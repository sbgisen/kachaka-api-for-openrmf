#!/usr/bin/env pipenv-shebang
# -*- encoding: utf-8 -*-

# Copyright (c) 2024 SoftBank Corp.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import asyncio
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional, Union

from google._upb._message import RepeatedCompositeContainer
from google.protobuf.json_format import MessageToDict
from grpc import RpcError
from grpc import StatusCode
from kachaka_api.generated import kachaka_api_pb2 as pb2
from kachaka_client_with_keepalive import KachakaApiClientWithKeepalive
import yaml
import zenoh


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    theta: float

    @classmethod
    def zero(cls) -> 'Pose':
        return cls(0.0, 0.0, 0.0)

    def as_list(self) -> List[float]:
        return [self.x, self.y, self.theta]


@dataclass(frozen=True)
class CommandCompletion:
    """Internal command completion state.

    as_payload() publishes {id, is_completed, success, reason} to Zenoh.
    error_code is kept on the instance for the Kachaka-side retry logic
    but is NOT published — see as_payload(). reason IS published (it is an
    optional field existing consumers can ignore, see Issue #34 Plan §5.2).

    error_code values used internally (not transmitted):
        0   : success
        -1  : unknown / format error
        -2  : map name mismatch (reason='map_mismatch')
        -3  : superseded by a new command
        -4  : gRPC channel persistently stuck (Deadline Exceeded > grpc_stuck threshold)
        -5  : completion watchdog expired (no RUNNING/completion observed in time)
        -6  : no RUNNING observed within command_start timeout (reason='start_timeout')
        -7  : no motion progress within motion_progress timeout (reason='progress_timeout')
        -8  : preempted by an externally-triggered returnHome (reason='external_preempted')
        -9  : rejected while external control is active (reason='external_busy')
        -10 : StartCommand succeeded but returned no command_id, so ownership
              could not be captured; fail closed instead of binding by
              command type/target (reason='missing_command_id')
        -11 : undock endpoint rejected (too close to another charge_station,
              or too far from every configured lane;
              reason='undock_endpoint_rejected')
        -12 : the undock phase exceeded its total budget
              (reason='undock_phase_timeout')
    """

    task_id: str
    is_completed: bool
    success: Optional[bool]
    error_code: Optional[int]
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError('task_id must not be empty')
        if not self.is_completed and (self.success is not None or self.error_code is not None or
                                      self.reason is not None):
            raise ValueError('in-progress completion must not have success, error_code or reason')

    def as_payload(self) -> Dict[str, Any]:
        """Return the payload for Zenoh publishing.

        error_code is internal state for retry logic and not published.
        """
        payload: Dict[str, Any] = {
            'id': self.task_id,
            'is_completed': self.is_completed,
        }
        if self.is_completed and self.success is not None:
            payload['success'] = self.success
        if self.is_completed and self.reason is not None:
            payload['reason'] = self.reason
        return payload


@dataclass(frozen=True)
class MapState:
    telemetry_map_name: str

    def __post_init__(self) -> None:
        if not self.telemetry_map_name:
            raise ValueError('telemetry_map_name must not be empty')

    @classmethod
    def initial(cls) -> 'MapState':
        return cls(telemetry_map_name='unknown')

    def with_telemetry_map_name(self, map_name: str) -> 'MapState':
        return MapState(telemetry_map_name=map_name)


@dataclass
class UndockPhase:
    """State of a two-phase undock-then-navigate command (Issue #51).

    A single RMF task ID drives two Kachaka commands: phase 1 leaves the
    dock, phase 2 performs the originally requested move (or is skipped when
    the original target sits on the dock the robot just left). Both phases'
    Kachaka command IDs are held here so _is_own_command_id() keeps
    recognizing phase 1's command as ours across the phase boundary, instead
    of betting that boundary on the own_return_home_retention grace period.

    Transitions: UNDOCKING -> NAVIGATING | SKIP_ORIGINAL -> FINAL. Only FINAL
    may report success to RMF (see _publish_command_completion).
    """

    UNDOCKING = 'UNDOCKING'
    NAVIGATING = 'NAVIGATING'
    SKIP_ORIGINAL = 'SKIP_ORIGINAL'
    FINAL = 'FINAL'

    task_id: str
    original_command: Dict[str, Any]
    skip_original: bool
    started_at: float
    phase: str = UNDOCKING
    phase1_command_id: Optional[str] = None
    phase2_command_id: Optional[str] = None


class KachakaApiClientByZenoh:
    """A client for the Kachaka API that publishes data to Zenoh.

    This class connects to a Kachaka API server and a Zenoh router,
    and provides methods to publish the robot's pose, current map name,
    and command state to Zenoh topics. It also subscribes to a command
    topic to receive and execute commands.
    """

    session: zenoh.Session
    pose_pub: zenoh.Publisher
    battery_pub: zenoh.Publisher
    map_name_pub: zenoh.Publisher
    command_is_completed_pub: zenoh.Publisher
    status_queryable: zenoh.Queryable
    command_querier: zenoh.Querier
    kachaka_client: Any  # kachaka_api.KachakaApiClient
    method_mapping: Dict[str, str]
    map_name_mapping: Dict[str, str]
    reverse_map_name_mapping: Dict[str, str]
    zenoh_config: Optional[str]
    robot_name: str
    task_id: Optional[str]
    last_command: Optional[Dict[str, Any]]
    last_command_result: Optional[CommandCompletion]
    last_command_id: Optional[str]
    current_command_id: Optional[str]
    is_async_command: bool
    saw_running: bool
    async_command_started_at: Optional[float]
    command_check_interval: float
    logger: logging.Logger
    retry_enabled: bool
    max_navigation_retries: int
    retry_interval: float
    retry_on_error_types: List[str]
    retry_count: int
    state_pub: zenoh.Publisher
    noop_enabled: bool
    noop_distance_tolerance: float
    noop_yaw_tolerance: float
    progress_distance_delta: float
    progress_yaw_delta: float
    command_match_distance_tolerance: float
    command_match_yaw_tolerance: float
    command_start_timeout: float
    motion_progress_timeout: float
    command_dispatched_at: Optional[float]
    expected_kachaka_method: Optional[str]
    command_target_map_name: Optional[str]
    command_target_pose: Optional[Pose]
    external_control_active: bool
    own_return_home_retention: float

    # Undock prefix settings (Issue #51). Defined at class level so an
    # instance built without __init__ (tests, tooling) has the feature off
    # and behaves exactly as before rather than raising AttributeError.
    undock_enabled = False
    undock_distance_m = 0.5
    undock_dock_radius_m = 0.5
    undock_other_dock_vicinity_m = 0.5
    undock_lane_tolerance_m = 0.3
    undock_start_timeout = 15.0
    undock_progress_timeout = 30.0
    undock_phase_timeout = 45.0
    undock_chargers: List[Dict[str, Any]] = ()
    undock_lanes: List[Dict[str, Any]] = ()
    # Active two-phase undock command, or None when no undock is in flight.
    _undock: Optional[UndockPhase] = None

    # Allowed range for undock.distance_m: 1.0m would overshoot the 0.95m
    # spacing between two adjacent docks, and anything at or below
    # noop_distance_tolerance would "succeed" without the robot moving
    # (Issue #51 defaults section).
    UNDOCK_MIN_DISTANCE_M = 0.3
    UNDOCK_MAX_DISTANCE_M = 0.6

    # Consecutive command_id mismatches logged before re-warning (own binding
    # is never undone by a mismatch -- see _note_ignored_mismatch).
    MAX_IGNORED_MISMATCHES = 5

    # Maps the internal (post method_mapping) Kachaka method name to the
    # protobuf Command oneof field MessageToDict() produces, used by
    # _validate_command_match() to confirm a result actually belongs to the
    # command type that was dispatched (Issue #34 Plan §7.3).
    COMMAND_TYPE_FIELD = {
        'move_to_pose': 'moveToPoseCommand',
        'return_home': 'returnHomeCommand',
    }

    # Kachaka command types known to occupy the robot's movement/command
    # slot; used only to log a clear diagnostic when a RUNNING command's type
    # is not one of these. Detection itself never gates on this set: a
    # non-own RUNNING command of any (including unrecognized) type is
    # treated as external, since the real Kachaka app's Return Home button
    # was observed on hardware to issue moveToLocationCommand rather than
    # returnHomeCommand (ISS34-034), and any future/unknown command type must
    # default to busy rather than being silently ignored (Issue #34 Plan §5).
    KNOWN_MOVEMENT_COMMAND_FIELDS = frozenset({
        'returnHomeCommand',
        'moveToLocationCommand',
        'moveToPoseCommand',
    })

    def __init__(
        self,
        zenoh_router: str,
        kachaka_access_point: Optional[str] = None,
        robot_name: str = 'kachaka',
        config_file: str = 'config.yaml',
        log_level: str = 'INFO',
    ) -> None:
        """Construct method.

        Args:
            zenoh_router (str): The address of the Zenoh router to connect to,
                in the format "ip:port".
            kachaka_access_point (str, optional): The URL of the Kachaka API server.
                Can be None when running internally on Kachaka.
            robot_name (str): The name of the robot, used in Zenoh topic names.
                Defaults to 'kachaka'.
            config_file (str): The name of the configuration file to load.
                Defaults to 'config.yaml'.
            log_level (str): The logging level. Defaults to 'INFO'.
                Valid values: DEBUG, INFO, WARNING, ERROR, CRITICAL.
        """
        self.log_level = getattr(logging, log_level.upper(), logging.INFO)
        file_path = Path(__file__).resolve().parent
        config_path = file_path / '..' / 'config' if (file_path / '..' / 'config').exists() else file_path / 'config'
        with open(config_path / config_file, 'r') as f:
            config = yaml.safe_load(f)
        self.method_mapping = config.get('method_mapping', {})
        self.map_name_mapping = config.get('map_name_mapping', {})
        self.reverse_map_name_mapping = {v: k for k, v in self.map_name_mapping.items()}
        self.zenoh_config = config.get('zenoh_config', None)

        # Load timing and connection settings
        timeouts = config.get('timeouts', {})
        intervals = config.get('intervals', {})
        connection = config.get('connection', {})
        navigation_retry = config.get('navigation_retry', {})
        navigation = config.get('navigation', {})

        self.command_query_timeout = timeouts.get('command_query', 2.0)
        self.grpc_connection_sleep = timeouts.get('grpc_connection', 5)
        self.running_state_wait = timeouts.get('running_state_wait', 5.0)
        self.grpc_telemetry_timeout = timeouts.get('grpc_telemetry', 5.0)
        self.grpc_status_check_timeout = timeouts.get('grpc_status_check', 5.0)
        self.grpc_stuck_threshold = float(timeouts.get('grpc_stuck', 30.0))
        self.command_completion_timeout = float(timeouts.get('command_completion', 180.0))
        self.command_start_timeout = float(timeouts.get('command_start', 15.0))
        self.motion_progress_timeout = float(timeouts.get('motion_progress', 30.0))
        self.own_return_home_retention = float(timeouts.get('own_return_home_retention', 5.0))
        self.command_check_interval = intervals.get('command_check', 4.0)

        # Load navigation tolerance settings (Issue #34 Plan §7.1-§7.3)
        self.noop_enabled = bool(navigation.get('noop_enabled', False))
        self.noop_distance_tolerance = float(navigation.get('noop_distance_tolerance', 0.15))
        self.noop_yaw_tolerance = float(navigation.get('noop_yaw_tolerance', 0.10))
        self.progress_distance_delta = float(navigation.get('progress_distance_delta', 0.02))
        self.progress_yaw_delta = float(navigation.get('progress_yaw_delta', 0.02))
        self.command_match_distance_tolerance = float(navigation.get('command_match_distance_tolerance', 0.75))
        self.command_match_yaw_tolerance = float(navigation.get('command_match_yaw_tolerance', 0.35))
        self._load_undock_config(config.get('undock', {}))
        self.main_loop_sleep = intervals.get('main_loop', 1)
        self.max_retries = connection.get('max_retries', 20)
        self.command_max_retries = connection.get('command_max_retries', 2)
        self.max_consecutive_errors = connection.get('max_consecutive_errors', 10)

        # Load navigation retry settings
        self.retry_enabled = navigation_retry.get('enabled', True)
        self.max_navigation_retries = navigation_retry.get('max_retries', 3)
        self.retry_interval = navigation_retry.get('retry_interval', 2.0)
        self.retry_on_error_types = navigation_retry.get('retry_on_error_types', ['Error'])
        self.kachaka_access_point = kachaka_access_point
        self.kachaka_client = (KachakaApiClientWithKeepalive(kachaka_access_point)
                               if kachaka_access_point else KachakaApiClientWithKeepalive())
        self.robot_name = robot_name
        self.task_id = None
        logging.basicConfig(
            level=self.log_level,
            format='%(asctime)s - %(levelname)s - %(message)s',
            filename='kachaka_api.log',
        )
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(self.log_level)

        # Initialize Zenoh session and publishers in constructor
        self.session = zenoh.open(self._get_zenoh_config(zenoh_router))
        self.pose_pub = self.session.declare_publisher(f'robots/{self.robot_name}/pose')
        self.battery_pub = self.session.declare_publisher(f'robots/{self.robot_name}/battery')
        self.map_name_pub = self.session.declare_publisher(f'robots/{self.robot_name}/map_name')
        self.command_is_completed_pub = self.session.declare_publisher(
            f'robots/{self.robot_name}/command_is_completed')
        self.state_pub = self.session.declare_publisher(f'robots/{self.robot_name}/state')

        # Initialize queryable for request-reply pattern
        self.status_queryable = self.session.declare_queryable(f'robots/{self.robot_name}/status',
                                                               self._status_query_handler)

        # Initialize querier for fetching commands from fleet adapter
        self.command_querier = self.session.declare_querier(
            f'robots/{self.robot_name}/command',
            target=zenoh.QueryTarget.ALL,
            timeout=self.command_query_timeout,
        )

        self.logger.info(f'Initialized KachakaApiClientByZenoh for robot {robot_name}')
        self.last_pose = Pose.zero()
        self.last_battery = 100.0
        self.map_state = MapState.initial()
        self.last_command = None
        self.last_command_result = None
        self.last_command_id = None
        self.current_command_id = None
        self.is_async_command = False
        self.saw_running = False
        self.async_command_started_at = None
        self.retry_count = 0
        self._command_lock = threading.RLock()
        self._client_lock = threading.RLock()
        self._first_grpc_failure_time: Optional[float] = None
        # Serializes command dispatch so heavy gRPC work runs outside _command_lock.
        self._dispatch_lock = threading.Lock()
        # True while a command dispatch is running outside _command_lock.
        # publish_result skips polling then, so it can never pair the previous
        # command's result with the task being dispatched.
        self.dispatching = False
        # commandId of a RUNNING command monitor_external_control() observed
        # while dispatching=True deferred judgment (Issue #34 Plan §5
        # "remember snapshot.commandId for reconciliation"): reconciled once
        # the dispatch settles, so a genuinely external command that both
        # starts and ends entirely within the dispatch window is still
        # detected instead of silently missed (Codex re-review ISS34-037
        # non-blocking finding).
        self._pending_dispatch_snapshot_id: Optional[str] = None
        # Monotonic time of last observed progress (accept or RUNNING) for the
        # active task; drives the completion watchdog.
        self.last_progress_at: Optional[float] = None
        # Consecutive polls whose state/result command_id mismatched the bound
        # current_command_id; used to detect and undo a wrong binding.
        self._ignored_result_count = 0
        # Monotonic dispatch time of the active async command; drives the
        # command_start timeout (RUNNING must be observed before it expires).
        self.command_dispatched_at: Optional[float] = None
        # Pose baseline and timestamp for the motion_progress timeout: reset
        # whenever the robot's position/yaw moves beyond the configured delta
        # while RUNNING, so a stalled-in-place RUNNING command still times out.
        self._motion_progress_pose: Optional[Pose] = None
        self._motion_progress_at: Optional[float] = None
        # Expected Kachaka command type/target for the active async command,
        # used to confirm a success result actually matches what was
        # dispatched (Issue #34 Plan §7.3).
        self.expected_kachaka_method: Optional[str] = None
        self.command_target_map_name: Optional[str] = None
        self.command_target_pose: Optional[Pose] = None
        # True while a RUNNING Kachaka command not issued by this bridge is
        # observed on the robot (Issue #34 Plan §5/§7.4, generalized from
        # returnHome-only to any command type -- ISS34-034/ISS34-035).
        self.external_control_active = False
        self._external_command_id: Optional[str] = None
        # The command_id and expiry of the own async command (any method,
        # not only dock/return_home) that most recently completed; lets
        # _is_own_command_id() still recognize a briefly-lingering RUNNING
        # command as ours after task_id has already been reset (Issue #34
        # Plan §5, generalized from the return_home-only grace period,
        # Codex review concern (b)).
        self._recently_completed_own_command_id: Optional[str] = None
        self._recently_completed_own_command_until: Optional[float] = None
        # Active two-phase undock command (Issue #51), or None.
        self._undock: Optional[UndockPhase] = None
        # Monotonically increasing sequence number for the unified state
        # payload (Issue #34 Plan §5.1).
        self._state_seq = 0

    def _get_zenoh_config(self, zenoh_router: str) -> zenoh.Config:
        """Get Zenoh configuration with the provided router.

        Args:
            zenoh_router (str): The address of the Zenoh router to connect to,
                in the format "ip:port".

        Returns:
            zenoh.Config: A Zenoh configuration object.
        """
        conf = zenoh.Config.from_file(self.zenoh_config) if self.zenoh_config is not None else zenoh.Config()
        conf.insert_json5('connect/endpoints', json.dumps([f'tcp/{zenoh_router}']))
        return conf

    async def run_method(self, method_name: str, args: Optional[Dict[str, Any]] = None) -> Any:  # noqa: ANN401
        """Run a KachakaApiClient method with the provided arguments.

        Args:
            method_name (str): The name of the method to run.
            args (dict, optional): The arguments to pass to the method.
                Defaults to None.

        Returns:
            Any: The result of the method call, converted to a dictionary
                or list if it is a protobuf message.

        Raises:
            ConnectionError: If gRPC connection check fails after max retries
            RpcError: If any other gRPC error occurs
        """
        if not self.grpc_connection_check():
            error_msg = f'Failed to connect to Kachaka API server after max retries for method {method_name}'
            self.logger.error(error_msg)
            raise ConnectionError(error_msg)

        args = args or {}
        try:
            method = getattr(self.kachaka_client, method_name)
            response = self._to_dict(method(**args))
            return response
        except RpcError as e:
            self.logger.error(f'RPC error in {method_name}: {e.details()}')
            raise

    def _log_info(self, message: str) -> None:
        """Log info message and print to console."""
        self.logger.info(message)
        print(message)

    def _log_warning(self, message: str) -> None:
        """Log warning message and print to console."""
        self.logger.warning(message)
        print(message)

    def _log_error_msg(self, message: str) -> None:
        """Log error message and print to console."""
        self.logger.error(message)
        print(message)

    def _log_error(self, error_type: str, method_name: str, error: Exception) -> None:
        """Log an error with consistent formatting.

        Args:
            error_type (str): Type of error (Connection, RPC, etc.)
            method_name (str): Name of the method where the error occurred
            error (Exception): The exception object
        """
        error_msg = f'{error_type} error during {method_name}: {str(error)}'
        if isinstance(error, RpcError):
            error_msg = f'{error_type} error during {method_name}: {error.details()}'
        self._log_error_msg(error_msg)

    def _publish_to_zenoh(self, publisher: zenoh.Publisher, data: Union[Dict, List, str, int, float, bool]) -> bool:
        """Publish data to a Zenoh topic with consistent encoding.

        Args:
            publisher: The Zenoh publisher to use
            data: The data to publish (will be JSON-encoded)
        """
        try:
            publisher.put(json.dumps(data).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
        except Exception as e:
            self._log_error_msg(f'Failed to publish data to Zenoh: {str(e)}')
            return False
        return True

    def _get_command_state_response(self) -> Dict[str, Any]:
        """Fetch the latest command state including command_id."""
        try:
            response = self.kachaka_client.stub.GetCommandState(pb2.GetRequest(),
                                                                timeout=self.grpc_status_check_timeout)
            return MessageToDict(response)
        except RpcError as e:
            self._log_error('RPC', 'get_command_state', e)
            raise

    def _get_last_command_result_response(self) -> Dict[str, Any]:
        """Fetch the most recent command result including command_id."""
        try:
            response = self.kachaka_client.stub.GetLastCommandResult(pb2.GetRequest(),
                                                                     timeout=self.grpc_status_check_timeout)
            return MessageToDict(response)
        except RpcError as e:
            self._log_error('RPC', 'get_last_command_result', e)
            raise

    def _is_running_state(self, state_value: Union[str, int, None]) -> bool:
        """Return True if the provided state represents RUNNING."""
        if isinstance(state_value, str):
            return state_value.upper() == 'COMMAND_STATE_RUNNING'
        if isinstance(state_value, int):
            return state_value == pb2.CommandState.Value('COMMAND_STATE_RUNNING')
        return False

    def _update_current_command_id(self, response: Optional[Dict[str, Any]], method_name: str) -> None:
        """Bind current_command_id from a StartCommand response known to carry a non-empty commandId.

        Callers must already have fail-closed a missing/empty commandId
        (Codex re-review ISS34-037 blocking-1); this only performs the bind.
        """
        command_id = response.get('commandId') if isinstance(response, dict) else None
        self.current_command_id = command_id
        self.logger.debug(f'Captured command_id {command_id} for {method_name}')

    def _running_state_wait_expired(self) -> bool:
        """Return True when waiting for RUNNING has exceeded the configured timeout."""
        if self.async_command_started_at is None:
            return False
        return (time.monotonic() - self.async_command_started_at) >= self.running_state_wait

    def _completion_watchdog_expired(self) -> bool:
        """Return True when the active task has shown no progress for too long.

        Progress means command acceptance or an observed RUNNING state. This
        is the last line of defense: whatever path loses a completion (wrong
        command_id binding, lost result, robot silence), RMF is eventually
        unblocked with a failure completion instead of waiting forever.
        """
        if self.last_progress_at is None:
            return False
        return (time.monotonic() - self.last_progress_at) >= self.command_completion_timeout

    def _note_ignored_mismatch(self, source: str, observed_id: Optional[str]) -> None:
        """Log a persistent command_id mismatch without ever undoing the own binding.

        Never clears current_command_id. StartCommand's response binds our
        own command_id synchronously at dispatch (_execute_async_stub_dispatch),
        so once bound it is authoritative for the life of the task; a
        mismatch here just means Kachaka is reporting a different command's
        state/result, not evidence that our own binding was wrong. Unbinding
        used to let a persistent external command_id whose type/target
        happened to match the dispatched task rebind onto this task once
        _ignored_result_count reached MAX_IGNORED_MISMATCHES -- a same-type,
        same-ish-target external command could then be silently attributed
        to this task's completion (ISS34-035 gap analysis). A genuinely
        external command is instead handled by monitor_external_control(),
        which preempts this task outright rather than waiting for repeated
        mismatches (Issue #34 Plan §5).
        """
        self._ignored_result_count += 1
        if self._ignored_result_count >= self.MAX_IGNORED_MISMATCHES:
            self._log_warning(f'{source} command_id {observed_id} mismatched bound '
                              f'{self.current_command_id} {self._ignored_result_count} consecutive times; '
                              'still ignoring (own binding is never undone by a mismatch)')
            self._ignored_result_count = 0
        else:
            self.logger.debug(
                'Ignoring %s for command_id %s (expecting %s)',
                source,
                observed_id,
                self.current_command_id,
            )

    @staticmethod
    def _normalize_angle(angle: float) -> float:
        """Normalize an angle (radians) to (-pi, pi], wrapping across the +/-pi seam."""
        return math.atan2(math.sin(angle), math.cos(angle))

    def _is_near_target(
        self,
        target_x: float,
        target_y: float,
        target_yaw: float,
        distance_tolerance: float,
        yaw_tolerance: float,
    ) -> bool:
        """Return True when the last known pose is within tolerance of the target.

        Used both for the near-distance short-circuit (§7.1, noop_* tolerances)
        and to confirm a Kachaka success actually reached the RMF-requested
        target (§7.3, command_match_* tolerances).
        """
        distance = math.hypot(target_x - self.last_pose.x, target_y - self.last_pose.y)
        yaw_diff = abs(self._normalize_angle(target_yaw - self.last_pose.theta))
        return distance <= distance_tolerance and yaw_diff <= yaw_tolerance

    def _fresh_floor_matches(self, requested_map_name: str) -> bool:
        """Re-fetch the robot's current floor via gRPC and compare with requested_map_name.

        Gates the near-distance no-op shortcut (Issue #34 Plan §7.1, Codex
        review ISS34-006 concern (a)): the shortcut must never rely on cached
        telemetry, because a switch_map racing the last telemetry read would
        otherwise let a stale floor/pose combination short-circuit to
        success. Always re-queries, even when the cache already agrees with
        requested_map_name.
        """
        try:
            current_map_name = self._fetch_robot_map_name(self.grpc_status_check_timeout)
        except RpcError as e:
            self._log_error('RPC', '_fresh_floor_matches', e)
            return False
        self.map_state = self.map_state.with_telemetry_map_name(current_map_name)
        self._publish_to_zenoh(self.map_name_pub, current_map_name)
        return current_map_name == requested_map_name

    def _observed_command_type_matches_expected(self, command_dict: Optional[Dict[str, Any]]) -> bool:
        """Return True when an observed Kachaka command payload's type matches expected_kachaka_method.

        Used by _validate_command_match() to confirm a completed command's
        type actually matches what was dispatched before its success is
        reported to RMF (Issue #34 Plan §7.3). Ownership (which command_id
        this task's polling should follow) is decided solely by command_id
        equality against the id captured at dispatch (Codex re-review
        ISS34-037 blocking-1) -- command type/target are never used to bind
        an unconfirmed command_id.
        """
        expected_field = self.COMMAND_TYPE_FIELD.get(self.expected_kachaka_method or '')
        if expected_field is None:
            return True
        return isinstance(command_dict, dict) and expected_field in command_dict

    def _record_motion_progress_baseline(self) -> None:
        """Reset the motion_progress baseline to the current pose and time."""
        self._motion_progress_pose = self.last_pose
        self._motion_progress_at = time.monotonic()

    def _check_motion_progress(self) -> None:
        """Advance the motion_progress baseline if the pose moved beyond the configured delta.

        Called every cycle the active command is RUNNING. Only a real
        position/yaw change counts as progress; observing RUNNING alone does
        not (Issue #34 Plan §7.2).
        """
        if self._motion_progress_pose is None:
            self._record_motion_progress_baseline()
            return
        if not self._is_near_target(
                self._motion_progress_pose.x,
                self._motion_progress_pose.y,
                self._motion_progress_pose.theta,
                distance_tolerance=self.progress_distance_delta,
                yaw_tolerance=self.progress_yaw_delta,
        ):
            self._record_motion_progress_baseline()

    def _command_start_timeout_expired(self) -> bool:
        """Return True when RUNNING has not been observed within command_start_timeout."""
        if self.command_dispatched_at is None:
            return False
        return (time.monotonic() - self.command_dispatched_at) >= self._active_start_timeout()

    def _motion_progress_timeout_expired(self) -> bool:
        """Return True when the pose has not moved within motion_progress_timeout while RUNNING."""
        if self._motion_progress_at is None:
            return False
        return (time.monotonic() - self._motion_progress_at) >= self._active_progress_timeout()

    def _attempt_cancel_active_command(self) -> None:
        """Best-effort cancel of the active Kachaka command on a start/progress timeout."""
        try:
            if hasattr(self.kachaka_client, 'cancel_command'):
                self.kachaka_client.cancel_command()
        except Exception as e:
            self._log_error('Unexpected', '_attempt_cancel_active_command', e)

    def _handle_async_timeout(self, error_code: int, reason: str) -> None:
        """Cancel and fail-once the active async command on a start/progress timeout."""
        task_id = self.task_id
        if not task_id:
            return
        self._log_error_msg(f'{reason} for task {task_id}; attempting cancel and failing once')
        self._attempt_cancel_active_command()
        self._publish_command_completion(success=False, error_code=error_code, task_id=task_id, reason=reason)

    def _validate_command_match(self, last_result: Dict[str, Any]) -> tuple:
        """Confirm a Kachaka success result matches the command that was dispatched.

        A matching command_id is not sufficient: the Kachaka command type,
        current floor, and (when applicable) target position/yaw must also
        match, so a same-ID-but-different-command success is never reported
        as the active RMF instruction's success (Issue #34 Plan §7.3).

        Returns:
            (matched, reason): matched is False when the result should not be
                treated as success; reason is the Plan §5.2 reason string to
                publish, or None when no listed reason applies.
        """
        expected_field = self.COMMAND_TYPE_FIELD.get(self.expected_kachaka_method or '')
        if expected_field is None:
            return True, None

        command_dict = last_result.get('command')
        if not self._observed_command_type_matches_expected(command_dict):
            self._log_warning(
                f'Command type mismatch for task {self.task_id}: expected {expected_field}, got {command_dict!r}')
            return False, None

        if (self.command_target_map_name is not None and
                self.command_target_map_name != self.map_state.telemetry_map_name):
            self._log_warning(f'Floor mismatch at completion for task {self.task_id}: expected '
                              f'{self.command_target_map_name}, robot is on {self.map_state.telemetry_map_name}')
            return False, 'map_mismatch'

        if self.command_target_pose is not None and not self._is_near_target(
                self.command_target_pose.x,
                self.command_target_pose.y,
                self.command_target_pose.theta,
                distance_tolerance=self.command_match_distance_tolerance,
                yaw_tolerance=self.command_match_yaw_tolerance,
        ):
            self._log_warning(f'Target position mismatch at completion for task {self.task_id}: '
                              f'target={self.command_target_pose.as_list()}, last_pose={self.last_pose.as_list()}')
            return False, None

        return True, None

    def _load_undock_config(self, undock: Dict[str, Any]) -> None:
        """Load the undock prefix settings (Issue #51).

        Args:
            undock (Dict[str, Any]): The ``undock`` section of config.yaml.
        """
        timeouts = undock.get('timeouts', {}) or {}
        self.undock_enabled = bool(undock.get('enabled', False))
        distance = float(undock.get('distance_m', 0.5))
        clamped = min(max(distance, self.UNDOCK_MIN_DISTANCE_M), self.UNDOCK_MAX_DISTANCE_M)
        if clamped != distance:
            self.logger.warning('undock.distance_m %s is outside [%s, %s]; using %s', distance,
                                self.UNDOCK_MIN_DISTANCE_M, self.UNDOCK_MAX_DISTANCE_M, clamped)
        self.undock_distance_m = clamped
        self.undock_dock_radius_m = float(undock.get('dock_radius_m', 0.5))
        self.undock_other_dock_vicinity_m = float(undock.get('other_dock_vicinity_m', 0.5))
        self.undock_lane_tolerance_m = float(undock.get('lane_tolerance_m', 0.3))
        self.undock_start_timeout = float(timeouts.get('start', 15.0))
        self.undock_progress_timeout = float(timeouts.get('progress', 30.0))
        self.undock_phase_timeout = float(timeouts.get('phase_total', 45.0))
        self.undock_chargers = list(undock.get('chargers', []) or [])
        self.undock_lanes = list(undock.get('lanes', []) or [])

    def _is_undock_phase1_active(self) -> bool:
        """Return True while the undock (phase 1) move of the active task is in flight."""
        undock = self._undock
        return (undock is not None and undock.phase == UndockPhase.UNDOCKING and undock.task_id == self.task_id)

    def _active_start_timeout(self) -> float:
        """Return the command_start timeout that applies to the in-flight command."""
        return self.undock_start_timeout if self._is_undock_phase1_active() else self.command_start_timeout

    def _active_progress_timeout(self) -> float:
        """Return the motion_progress timeout that applies to the in-flight command."""
        return self.undock_progress_timeout if self._is_undock_phase1_active() else self.motion_progress_timeout

    def _timeout_reason(self, reason: str) -> str:
        """Prefix a timeout reason with ``undock_`` while the undock phase owns the command.

        Undock reasons are excluded from the Kachaka-side navigation retry
        (see _should_retry_command): retrying re-runs last_command as a
        whole, which would repeat the departure move up to max_retries times
        when an obstacle blocks it (Issue #51).
        """
        return f'undock_{reason}' if self._is_undock_phase1_active() else reason

    def _undock_phase_timeout_expired(self) -> bool:
        """Return True when the undock phase has exceeded its total budget."""
        undock = self._undock
        if undock is None or undock.phase != UndockPhase.UNDOCKING:
            return False
        return (time.monotonic() - undock.started_at) >= self.undock_phase_timeout

    @staticmethod
    def _point_segment_distance(x: float, y: float, start: List[float], end: List[float]) -> float:
        """Return the distance from (x, y) to the segment start-end."""
        sx, sy = float(start[0]), float(start[1])
        ex, ey = float(end[0]), float(end[1])
        dx, dy = ex - sx, ey - sy
        length_sq = dx * dx + dy * dy
        if length_sq == 0.0:
            return math.hypot(x - sx, y - sy)
        ratio = max(0.0, min(1.0, ((x - sx) * dx + (y - sy) * dy) / length_sq))
        return math.hypot(x - (sx + ratio * dx), y - (sy + ratio * dy))

    def _configured_chargers(self, map_name: Optional[str]) -> List[Dict[str, Any]]:
        """Return the configured charge_station entries for a map."""
        return [c for c in self.undock_chargers if map_name is None or c.get('map_name') == map_name]

    def _fetch_charger_poses(self, map_name: Optional[str]) -> List[Pose]:
        """Return the charge_station poses, preferring the robot's own location list.

        get_locations() is the first choice; the configured poses are used
        only when the RPC fails or reports no charger (Issue #51). Kachaka
        locations carry no map name -- they belong to the map the robot is
        currently on, which the caller has already verified against the
        requested map.

        Args:
            map_name (str, optional): The RMF-side map name of the command,
                used to select the configured fallback entries.

        Returns:
            List[Pose]: The charger poses, or an empty list when none are known.
        """
        try:
            response = self.kachaka_client.stub.GetLocations(pb2.GetRequest(), timeout=self.grpc_status_check_timeout)
            charger_type = pb2.LocationType.Value('LOCATION_TYPE_CHARGER')
            poses = [
                Pose(loc.pose.x, loc.pose.y, loc.pose.theta) for loc in response.locations if loc.type == charger_type
            ]
            if poses:
                return poses
            self._log_warning('get_locations() reported no LOCATION_TYPE_CHARGER; falling back to configured poses')
        except Exception as e:
            self._log_error('Unexpected', '_fetch_charger_poses', e)

        fallback = []
        for entry in self._configured_chargers(map_name):
            pose = entry.get('pose') or []
            if len(pose) >= 2:
                fallback.append(Pose(float(pose[0]), float(pose[1]), float(pose[2]) if len(pose) > 2 else 0.0))
        return fallback

    def _charger_override(self, charger: Pose, map_name: Optional[str]) -> Dict[str, Any]:
        """Return the configured entry nearest to a charger pose, or an empty dict."""
        best: Dict[str, Any] = {}
        best_distance = self.undock_dock_radius_m
        for entry in self._configured_chargers(map_name):
            pose = entry.get('pose') or []
            if len(pose) < 2:
                continue
            distance = math.hypot(charger.x - float(pose[0]), charger.y - float(pose[1]))
            if distance <= best_distance:
                best, best_distance = entry, distance
        return best

    def _fetch_battery_status(self) -> Optional[int]:
        """Return the robot's power supply status enum, or None when it cannot be read."""
        try:
            response = self.kachaka_client.stub.GetBatteryInfo(pb2.GetRequest(),
                                                               timeout=self.grpc_status_check_timeout)
            return response.power_supply_status
        except Exception as e:
            self._log_error('Unexpected', '_fetch_battery_status', e)
            return None

    def _fetch_current_pose(self) -> Optional[Pose]:
        """Return a freshly queried robot pose, or None when it cannot be read.

        The undock move is aimed straight ahead of the *actual* body yaw
        read immediately before dispatch, never at a lane heading: aiming at
        the exit-lane heading is what produced the rotate-in-place command
        the robot would not execute (Issue #51).
        """
        try:
            response = self.kachaka_client.stub.GetRobotPose(pb2.GetRequest(), timeout=self.grpc_telemetry_timeout)
            pose = Pose(response.pose.x, response.pose.y, response.pose.theta)
            self.last_pose = pose
            return pose
        except Exception as e:
            self._log_error('Unexpected', '_fetch_current_pose', e)
            return None

    def _evaluate_undock(
        self,
        map_name: Optional[str],
        target_x: Optional[float],
        target_y: Optional[float],
    ) -> tuple:
        """Decide whether a move_to_pose must be prefixed with an undock move (Issue #51).

        Runs after the map guard and before the near-target no-op shortcut,
        so a micro-rotation on the dock can never be short-circuited to
        success while the robot is still on the charging contacts.

        The robot counts as docked only when the battery status is not
        DISCHARGING *and* it is within the dock radius of a charge_station:
        either signal alone is ambiguous (a robot can idle next to a dock, and
        UNSPECIFIED/NOT_CHARGING both occur on the contacts).

        Any inability to judge (status RPC failure, no known charger pose,
        unreadable pose) skips the prefix and dispatches the original command
        unchanged, so a telemetry hiccup never turns into a failed task.

        Args:
            map_name (str, optional): The RMF-side map name of the command.
            target_x (float, optional): The requested target x.
            target_y (float, optional): The requested target y.

        Returns:
            tuple: ``('none', None)`` to dispatch the original command
                unchanged, ``('reject', reason)`` to fail the command without
                moving, or ``('prefix', (args, skip_original))`` with the
                move_to_pose arguments of the departure move.
        """
        if not self.undock_enabled:
            return 'none', None
        if map_name is None or target_x is None or target_y is None:
            self.logger.debug('Undock: command lacks map_name/target; skipping undock evaluation')
            return 'none', None

        status = self._fetch_battery_status()
        if status is None:
            self._log_warning('Undock: battery status unavailable; dispatching without undock prefix')
            return 'none', None
        if status == pb2.PowerSupplyStatus.Value('POWER_SUPPLY_STATUS_DISCHARGING'):
            return 'none', None

        pose = self._fetch_current_pose()
        if pose is None:
            self._log_warning('Undock: robot pose unavailable; dispatching without undock prefix')
            return 'none', None

        chargers = self._fetch_charger_poses(map_name)
        if not chargers:
            self._log_warning('Undock: no charge_station pose available; dispatching without undock prefix')
            return 'none', None

        departure = min(chargers, key=lambda c: math.hypot(pose.x - c.x, pose.y - c.y))
        override = self._charger_override(departure, map_name)
        dock_radius = float(override.get('dock_radius_m', self.undock_dock_radius_m))
        if math.hypot(pose.x - departure.x, pose.y - departure.y) > dock_radius:
            self.logger.debug('Undock: not charging on a dock (status=%s, nearest charger beyond %.2fm)', status,
                              dock_radius)
            return 'none', None

        distance = float(override.get('distance_m', self.undock_distance_m))
        endpoint_x = pose.x + distance * math.cos(pose.theta)
        endpoint_y = pose.y + distance * math.sin(pose.theta)

        for other in chargers:
            if other is departure:
                continue
            if math.hypot(endpoint_x - other.x, endpoint_y - other.y) <= self.undock_other_dock_vicinity_m:
                self._log_warning(f'Undock endpoint ({endpoint_x:.2f}, {endpoint_y:.2f}) is within '
                                  f'{self.undock_other_dock_vicinity_m}m of another charge_station; refusing undock')
                return 'reject', 'undock_endpoint_rejected'

        lanes = [lane for lane in self.undock_lanes if lane.get('map_name') in (None, map_name)]
        if lanes:
            lane_distance = min(
                self._point_segment_distance(endpoint_x, endpoint_y, lane.get('start', [0.0, 0.0]),
                                             lane.get('end', [0.0, 0.0])) for lane in lanes)
            if lane_distance > self.undock_lane_tolerance_m:
                self._log_warning(f'Undock endpoint ({endpoint_x:.2f}, {endpoint_y:.2f}) is {lane_distance:.2f}m '
                                  f'from the nearest lane (> {self.undock_lane_tolerance_m}m); refusing undock')
                return 'reject', 'undock_endpoint_rejected'

        # The original target sitting on the dock we are leaving is the core
        # stuck-on-dock case: navigating back to it would re-enter the
        # contacts and undock again on the next command (Issue #51).
        skip_original = math.hypot(target_x - departure.x, target_y - departure.y) <= dock_radius
        args = {'x': endpoint_x, 'y': endpoint_y, 'yaw': pose.theta}
        self._log_info(f'Undock prefix: departing dock at ({departure.x:.2f}, {departure.y:.2f}) to '
                       f'({endpoint_x:.2f}, {endpoint_y:.2f}) keeping yaw {pose.theta:.3f}; '
                       f'skip_original={skip_original}')
        return 'prefix', (args, skip_original)

    async def _advance_undock_phase(self, task_id: str) -> None:
        """Move a task from a completed undock (phase 1) to its final phase.

        Called from publish_result() once phase 1 reported success, outside
        _command_lock: the phase 2 dispatch takes _dispatch_lock and would
        deadlock against a concurrent _execute_command if it ran while
        holding the command lock.

        Args:
            task_id (str): The RMF task ID whose undock phase completed.
        """
        with self._command_lock:
            undock = self._undock
            if undock is None or undock.task_id != task_id or undock.phase != UndockPhase.UNDOCKING:
                return
            undock.phase = UndockPhase.SKIP_ORIGINAL if undock.skip_original else UndockPhase.NAVIGATING
            skip_original = undock.skip_original
            original_command = undock.original_command
            self.last_progress_at = time.monotonic()

        if skip_original:
            self._log_info(f'Undock succeeded and the original target is on the dock just left; '
                           f'completing task {task_id} without re-approaching it')
            with self._command_lock:
                if self._undock is not None and self._undock.task_id == task_id:
                    self._undock.phase = UndockPhase.FINAL
            self._publish_command_completion(success=True, error_code=0, task_id=task_id)
            return

        self._log_info(f'Undock succeeded; dispatching the original target for task {task_id}')
        self._execute_command(original_command, expected_task_id=task_id, skip_undock=True)
        with self._command_lock:
            if self._undock is not None and self._undock.task_id == task_id:
                self._undock.phase2_command_id = self.current_command_id

    def _is_own_command_id(self, observed_command_id: Optional[str]) -> bool:
        """Return True when a RUNNING command's id belongs to this bridge's own dispatch.

        Ownership is decided purely by command_id equality against the
        bounded own-id set -- the currently bound dispatch (any method:
        move_to_pose/return_home dispatch captures command_id synchronously
        via stub.StartCommand(), see _execute_async_stub_dispatch) or a
        just-completed/just-superseded own command still within its
        retention grace period. Command type/target are never consulted
        here (Issue #34 Plan §5, generalized from a return_home-only,
        dock-method-gated check to any RMF-dispatched command -- ISS34-034
        found the real Kachaka app's Return Home button issues
        moveToLocationCommand, not returnHomeCommand, so a type-specific
        check misses it entirely).

        An unbound current_command_id is therefore treated as not ours --
        never as plausibly ours within a grace window -- so a genuinely
        external command racing our own dispatch (still in flight, not yet
        bound) is recognized as external instead of being assumed ours
        (Codex re-review ISS34-010 blocking-3). A command that completed
        just before this poll is still recognized via the short-lived
        _recently_completed_own_command_* grace period (concern (b)), so
        the trailing RUNNING state Kachaka can report right after
        completion or cancellation/supersession is not mistaken for
        external control.
        """
        if observed_command_id is None:
            return False
        if self.current_command_id is not None and observed_command_id == self.current_command_id:
            return True
        undock = self._undock
        if undock is not None and observed_command_id in (undock.phase1_command_id, undock.phase2_command_id):
            # Phase-owned retention (Issue #51): phase 1's id stays ours until
            # the undock context is cleared, so the gap between phase 1's
            # completion and phase 2's bind never depends on
            # own_return_home_retention being longer than the phase-boundary
            # round trip.
            return True
        if (self._recently_completed_own_command_id is not None and
                observed_command_id == self._recently_completed_own_command_id and
                self._recently_completed_own_command_until is not None and
                time.monotonic() < self._recently_completed_own_command_until):
            return True
        return False

    def _handle_external_command_started(self, command_id: Optional[str]) -> None:
        """Preempt any active RMF task and mark external control as active."""
        preempted_task_id = self.task_id
        if preempted_task_id is not None:
            self._log_error_msg(
                f'External command (command_id={command_id}) observed; preempting active RMF task {preempted_task_id}')
            self._publish_command_completion(success=False,
                                             error_code=-8,
                                             task_id=preempted_task_id,
                                             reason='external_preempted')
        else:
            self._log_info(f'External command (command_id={command_id}) observed while idle')
        self.external_control_active = True
        self._external_command_id = command_id

    def _handle_external_command_ended(self) -> None:
        """Mark external control as no longer active."""
        self._log_info(f'External command (command_id={self._external_command_id}) ended')
        self.external_control_active = False
        self._external_command_id = None

    def _reconcile_pending_dispatch_snapshot(self) -> None:
        """Reconcile a RUNNING command_id observed while a dispatch was in flight.

        Must be called holding self._command_lock, once a dispatch has fully
        settled (dispatching just flipped back to False) and StartCommand's
        response has already been fail-closed or bound. monitor_external_control()
        defers judgment while dispatching=True and instead remembers the last
        RUNNING command_id it saw (Issue #34 Plan §5 "remember
        snapshot.commandId for reconciliation"); check that id against
        ownership now, using the same rule monitor_external_control() itself
        uses, so a transient external command that started and ended entirely
        within the dispatch window (and would therefore be invisible to any
        later live poll) is still detected rather than silently dropped
        (Codex re-review ISS34-037 non-blocking finding).

        Callers must only invoke this after a dispatch that goes through
        _execute_async_stub_dispatch() (move_to_pose/return_home): that is
        the only path that can ever bind current_command_id, which
        _is_own_command_id() needs to tell "our own in-flight command" apart
        from a genuinely external one. A sync dispatch (switch_map, etc.)
        never binds an id at all, so this would always misclassify its own
        command as external (Codex re-review ISS34-040 blocking-B).
        """
        snapshot_id = self._pending_dispatch_snapshot_id
        self._pending_dispatch_snapshot_id = None
        if snapshot_id is None or self._is_own_command_id(snapshot_id):
            return
        if not self.external_control_active:
            self._handle_external_command_started(snapshot_id)
        elif snapshot_id != self._external_command_id:
            self._external_command_id = snapshot_id

    async def monitor_external_control(self) -> None:
        """Detect Kachaka commands not issued by this bridge (Issue #34 Plan §5).

        Runs every main loop iteration regardless of whether an RMF task is
        active, so an externally-triggered command (e.g. the Kachaka app's
        Return Home button, a low-battery auto-return, or a direct API call)
        is observed even while idle. Detection is generalized from a
        returnHomeCommand-only check to any RUNNING command whose command_id
        is not in the bounded own-id set (see _is_own_command_id):
        moveToLocationCommand/moveToPoseCommand are common examples, and an
        unrecognized command type still defaults to busy rather than being
        silently ignored (ISS34-034 found the real Return Home button issues
        moveToLocationCommand, which the previous returnHomeCommand-only
        check never caught).
        """
        method_name = 'monitor_external_control'
        try:
            state_res = self._get_command_state_response()
        except RpcError:
            return  # Already logged by _get_command_state_response.
        except Exception as e:
            self._log_error('Unexpected', method_name, e)
            return

        with self._command_lock:
            command_dict = state_res.get('command')
            is_running = self._is_running_state(state_res.get('state'))
            observed_id = state_res.get('commandId')

            if self.dispatching:
                # A dispatch is in flight and has not yet synchronously bound
                # its own command_id (_execute_async_stub_dispatch runs the
                # gRPC call outside _command_lock while dispatching=True).
                # Judging ownership now could misclassify our own
                # not-yet-bound dispatch as external; defer to the next poll,
                # by which point the dispatch has either bound
                # current_command_id or failed and reset task_id (Issue #34
                # Plan §5 dispatch-pending hold). Remember this snapshot so
                # _reconcile_pending_dispatch_snapshot() can still catch a
                # genuinely external command once the dispatch settles, even
                # if it has already ended by the next live poll (Codex
                # re-review ISS34-037 non-blocking finding).
                if is_running and observed_id is not None:
                    self._pending_dispatch_snapshot_id = observed_id
                return

            is_external_now = is_running and observed_id is not None and not self._is_own_command_id(observed_id)

            if is_external_now:
                if not isinstance(command_dict, dict) or not any(field in command_dict
                                                                 for field in self.KNOWN_MOVEMENT_COMMAND_FIELDS):
                    self.logger.warning(
                        'Unrecognized RUNNING command type %r (command_id=%s); defaulting to busy',
                        command_dict,
                        observed_id,
                    )
                if not self.external_control_active:
                    self._handle_external_command_started(observed_id)
                elif observed_id != self._external_command_id:
                    self._log_info(f'External command switched from {self._external_command_id} to {observed_id}; '
                                   'external_control_active remains True')
                    self._external_command_id = observed_id
            elif self.external_control_active:
                self._handle_external_command_ended()

    async def publish_pose(self) -> None:
        """Publish the current robot pose to Zenoh.

        Gets the robot's current pose from Kachaka API via gRPC stub with timeout
        and publishes it to Zenoh. On timeout, publishes cached value to keep
        the telemetry loop running.
        """
        method_name = 'publish_pose'
        try:
            response = self.kachaka_client.stub.GetRobotPose(pb2.GetRequest(), timeout=self.grpc_telemetry_timeout)
            pose = Pose(response.pose.x, response.pose.y, response.pose.theta)
            self.last_pose = pose
            self._publish_to_zenoh(self.pose_pub, pose.as_list())
        except RpcError as e:
            if e.code() == StatusCode.DEADLINE_EXCEEDED:
                self.logger.warning(f'Timeout in {method_name}, publishing cached value')
                self._publish_to_zenoh(self.pose_pub, self.last_pose.as_list())
            else:
                self._log_error('RPC', method_name, e)
                self._publish_to_zenoh(self.pose_pub, self.last_pose.as_list())
        except Exception as e:
            self._log_error('Unexpected', method_name, e)

    async def publish_battery(self) -> None:
        """Publish the current robot battery to Zenoh.

        Gets battery information from Kachaka API via gRPC stub with timeout.
        On timeout, publishes cached value to keep the telemetry loop running.
        """
        method_name = 'publish_battery'
        try:
            response = self.kachaka_client.stub.GetBatteryInfo(pb2.GetRequest(), timeout=self.grpc_telemetry_timeout)
            battery = response.remaining_percentage / 100.0
            self.last_battery = battery
            self._publish_to_zenoh(self.battery_pub, battery)
        except RpcError as e:
            if e.code() == StatusCode.DEADLINE_EXCEEDED:
                self.logger.warning(f'Timeout in {method_name}, publishing cached value')
                self._publish_to_zenoh(self.battery_pub, self.last_battery)
            else:
                self._log_error('RPC', method_name, e)
                self._publish_to_zenoh(self.battery_pub, self.last_battery)
        except Exception as e:
            self._log_error('Unexpected', method_name, e)

    def _fetch_robot_map_name(self, timeout: float) -> str:
        """Fetch the robot's actual current map name via gRPC.

        Queries GetMapList and GetCurrentMapId on the robot and applies the
        reverse name mapping so the returned value is the RMF-side map name.
        This is the single source of truth for the robot's current map.

        Args:
            timeout (float): The timeout in seconds for each gRPC call.

        Returns:
            str: The RMF-side name of the map the robot is currently on.

        Raises:
            RpcError: If either gRPC call fails or times out.
        """
        map_list_response = self.kachaka_client.stub.GetMapList(pb2.GetRequest(), timeout=timeout)
        map_id_response = self.kachaka_client.stub.GetCurrentMapId(pb2.GetRequest(), timeout=timeout)

        search_id = map_id_response.id
        kachaka_map_name = next(
            (entry.name for entry in map_list_response.map_list_entries if entry.id == search_id),
            'L1',
        )
        return self.reverse_map_name_mapping.get(kachaka_map_name, kachaka_map_name)

    async def publish_map_name(self) -> None:
        """Publish the current map name to Zenoh.

        Gets the current map name from Kachaka via gRPC with timeout and
        publishes it to Zenoh. On timeout, publishes cached value to keep
        the telemetry loop running.
        """
        method_name = 'publish_map_name'
        try:
            map_name = self._fetch_robot_map_name(self.grpc_telemetry_timeout)
            self.map_state = self.map_state.with_telemetry_map_name(map_name)
            self._publish_to_zenoh(self.map_name_pub, map_name)
        except RpcError as e:
            if e.code() == StatusCode.DEADLINE_EXCEEDED:
                self.logger.warning(f'Timeout in {method_name}, publishing cached value')
            else:
                self._log_error('RPC', method_name, e)
            self._publish_to_zenoh(self.map_name_pub, self.map_state.telemetry_map_name)
        except Exception as e:
            self._log_error('Unexpected', method_name, e)

    async def publish_state(self) -> None:
        """Publish the unified robot state payload to Zenoh (Issue #34 Plan §5.1).

        Kachaka has no API that returns floor and position atomically, so this
        reads floor -> pose -> floor and only publishes when both floor reads
        agree; a switch_map racing this read is caught by the mismatch and the
        sample is discarded rather than published with a stale floor/pose
        combination. On any gRPC error the sample is discarded outright: an
        old pose must never be resent under a new timestamp.
        """
        method_name = 'publish_state'
        try:
            map_name_before = self._fetch_robot_map_name(self.grpc_telemetry_timeout)
            pose_response = self.kachaka_client.stub.GetRobotPose(pb2.GetRequest(),
                                                                  timeout=self.grpc_telemetry_timeout)
            pose = Pose(pose_response.pose.x, pose_response.pose.y, pose_response.pose.theta)
            map_name_after = self._fetch_robot_map_name(self.grpc_telemetry_timeout)

            if map_name_before != map_name_after:
                self.logger.info(
                    'Discarding state sample: map changed during read (%s -> %s), likely switch_map in progress',
                    map_name_before,
                    map_name_after,
                )
                return

            with self._command_lock:
                self._state_seq += 1
                seq = self._state_seq
                external_control_active = self.external_control_active

            state_payload = {
                'schema_version': 1,
                'seq': seq,
                'stamp': time.time(),
                'map_name': map_name_after,
                'pose': pose.as_list(),
                'external_control_active': external_control_active,
            }
            self._publish_to_zenoh(self.state_pub, state_payload)
            self.logger.debug(f'Published state seq={seq} map_name={map_name_after}')
        except RpcError as e:
            if e.code() == StatusCode.DEADLINE_EXCEEDED:
                self.logger.warning(f'Timeout in {method_name}; discarding sample (no stale resend)')
            else:
                self._log_error('RPC', method_name, e)
        except Exception as e:
            self._log_error('Unexpected', method_name, e)

    async def _handle_command_retry(self, task_id: str, error_code: int, reason: Optional[str] = None) -> bool:
        """Handle retry logic for failed commands.

        Args:
            task_id: The task ID that produced the failed result
            error_code: The error code from the failed command
            reason: The completion reason, when one applies. ``undock_*``
                reasons are never retried (see _should_retry_command).

        Returns:
            True if retry was initiated (caller should return early), False otherwise
        """
        should_retry = await self._should_retry_command(error_code, reason)
        if not should_retry:
            self._log_info(f'Error code {error_code} is not retriable')
            return False

        with self._command_lock:
            if self.task_id != task_id:
                self.logger.debug('Skip retry for stale task %s (active task: %s)', task_id, self.task_id)
                return False
            if not self.retry_enabled or self.retry_count >= self.max_navigation_retries:
                if self.retry_count >= self.max_navigation_retries:
                    self._log_error_msg(f'Max retries ({self.max_navigation_retries}) exceeded for command {task_id}')
                return False
            if not self.last_command:
                return False

            self.retry_count += 1
            retry_count = self.retry_count
            command_to_retry = self.last_command.copy()

        self._log_info(f'Retrying command (attempt {retry_count}/{self.max_navigation_retries})')

        await asyncio.sleep(self.retry_interval)
        with self._command_lock:
            if self.task_id != task_id:
                self.logger.debug('Cancel retry for stale task %s (active task: %s)', task_id, self.task_id)
                return False

        # expected_task_id closes the window where a new command is accepted
        # between the check above and the dispatch below.
        self._execute_command(command_to_retry, expected_task_id=task_id)
        return True

    async def publish_result(self) -> None:
        """Publish command completion status to Zenoh.

        Monitors active async commands and publishes their completion status.
        Uses command_id to ensure state/result pairs belong to the active command.
        """
        method_name = 'publish_result'
        retry_task_id: Optional[str] = None
        retry_error_code: Optional[int] = None
        retry_reason: Optional[str] = None
        undock_advance_task_id: Optional[str] = None
        try:
            with self._command_lock:
                if not self.task_id:
                    # Keep publishing last result for Pub/Sub reliability.
                    # Fleet adapter's ID check ensures stale completions are ignored.
                    if self.last_command_result:
                        self._publish_to_zenoh(
                            self.command_is_completed_pub,
                            self.last_command_result.as_payload(),
                        )
                    return

                if self._completion_watchdog_expired():
                    self._log_error_msg(
                        f'No progress for task {self.task_id} within {self.command_completion_timeout}s; '
                        'forcing completion (internal error_code=-5) to unblock RMF')
                    self._publish_command_completion(success=False, error_code=-5)
                    return

                if self._undock_phase_timeout_expired():
                    self._log_error_msg(f'Undock phase for task {self.task_id} exceeded '
                                        f'{self.undock_phase_timeout}s; failing the task')
                    self._handle_async_timeout(error_code=-12, reason='undock_phase_timeout')
                    return

                if self.dispatching:
                    # A dispatch is running outside the lock; polling now could
                    # pair the previous command's result with the new task.
                    return

                state_res = self._get_command_state_response()
                self.logger.debug(f'GetCommandState response: {state_res}')
                command_id = state_res.get('commandId')
                state_value = state_res.get('state')

                if self.is_async_command and not self.saw_running and self._command_start_timeout_expired():
                    self._handle_async_timeout(error_code=-6, reason=self._timeout_reason('start_timeout'))
                    return

                if self.is_async_command:
                    if self.current_command_id is None:
                        # Ownership is bound exclusively by StartCommand's response
                        # command_id, captured synchronously at dispatch
                        # (_execute_async_stub_dispatch). A fail-closed dispatch
                        # (Issue #34 Plan §7.3/§7.4, Codex re-review ISS34-037
                        # blocking-1) never leaves this unbound while a task is
                        # active, so reaching here means the invariant was
                        # violated upstream; never treat an unconfirmed RUNNING
                        # command as progress on our task in that case (matching
                        # command type/target used to be accepted as an ownership
                        # proof here, but a same-type/target external command
                        # racing an ID-less dispatch could poison it).
                        self.logger.debug(
                            'Task %s has no bound command_id; ignoring observed command_id %s',
                            self.task_id,
                            command_id,
                        )
                        return
                if self._is_running_state(state_value):
                    if self.is_async_command and command_id != self.current_command_id:
                        # A required exact match, not just a mismatch check
                        # when both sides are non-empty: an empty/missing
                        # commandId here must never be treated as "still
                        # ours". Kachaka API 3.14.4.0's own client wrapper
                        # requires result.command_id == response.command_id
                        # while waiting for completion, so a genuinely
                        # RUNNING own command is expected to keep reporting
                        # a non-empty id (Codex re-review ISS34-040
                        # blocking-A). This check only gates RUNNING being
                        # accepted as our own progress; a non-RUNNING state
                        # (e.g. Kachaka's observed post-completion
                        # COMMAND_STATE_PENDING/id-less transition, Codex
                        # re-review ISS34-042) must fall through to
                        # GetLastCommandResult below instead of returning
                        # here, since completion ownership is judged by the
                        # result commandId match, not the state one.
                        self._note_ignored_mismatch('command state', command_id)
                        return
                    self.saw_running = True
                    self.last_progress_at = time.monotonic()
                    self._ignored_result_count = 0
                    self._check_motion_progress()
                    if self._motion_progress_timeout_expired():
                        self._handle_async_timeout(error_code=-7, reason=self._timeout_reason('progress_timeout'))
                        return
                    result = CommandCompletion(
                        task_id=self.task_id,
                        is_completed=False,
                        success=None,
                        error_code=None,
                    )
                    self.last_command_result = result
                    self._publish_to_zenoh(self.command_is_completed_pub, result.as_payload())
                    return

                if self.is_async_command and not self.saw_running and not self._running_state_wait_expired():
                    self.logger.debug('Async command: waiting to see RUNNING state first')
                    return

                last_result = self._get_last_command_result_response()
                # Both GetCommandState and GetLastCommandResult succeeded; reset stuck timer.
                self._first_grpc_failure_time = None
                self.logger.debug(f'GetLastCommandResult response: {last_result}')
                result_command_id = last_result.get('commandId')

                if self.is_async_command and result_command_id != self.current_command_id:
                    # Required exact match, same as the state-side check
                    # above: an empty/missing commandId on the result must
                    # never be accepted as a completion for our bound
                    # command (Codex re-review ISS34-040 blocking-A).
                    self._note_ignored_mismatch('result', result_command_id)
                    return

                cmd_result = last_result.get('result')
                if not isinstance(cmd_result, dict):
                    self._log_error_msg(f'Unexpected last command result format: {last_result}')
                    result = CommandCompletion(
                        task_id=self.task_id,
                        is_completed=True,
                        success=False,
                        error_code=-1,
                    )
                else:
                    success = cmd_result.get('success', False)
                    error_code = cmd_result.get('errorCode', 0)
                    reason = None
                    if success and self.is_async_command:
                        matched, mismatch_reason = self._validate_command_match(last_result)
                        if not matched:
                            success = False
                            error_code = -2 if mismatch_reason == 'map_mismatch' else -1
                            reason = mismatch_reason
                    if not success and reason is None and self._is_undock_phase1_active():
                        # Marks the failure as belonging to the departure move
                        # so it is never re-run by the navigation retry
                        # (Issue #51): a retry re-executes last_command as a
                        # whole and would repeat the undock up to max_retries
                        # times against the same obstacle.
                        reason = 'undock_failed'
                    result = CommandCompletion(
                        task_id=self.task_id,
                        is_completed=True,
                        success=success,
                        error_code=error_code,
                        reason=reason,
                    )

                    if success:
                        self._log_info(f'Command {self.task_id} succeeded')
                        self.retry_count = 0
                        undock = self._undock
                        if undock is not None and undock.task_id == self.task_id:
                            if undock.phase == UndockPhase.UNDOCKING:
                                # Phase 1 done: the task is not finished, so no
                                # completion is published. The original move is
                                # dispatched (or skipped) outside the lock.
                                undock_advance_task_id = self.task_id
                                self.last_progress_at = time.monotonic()
                            else:
                                undock.phase = UndockPhase.FINAL
                    else:
                        self._log_warning(f'Command {self.task_id} failed with error code {error_code}')
                        retry_task_id = self.task_id
                        retry_error_code = error_code
                        retry_reason = reason

            if undock_advance_task_id is not None:
                await self._advance_undock_phase(undock_advance_task_id)
                return

            if retry_task_id is not None and retry_error_code is not None:
                if await self._handle_command_retry(retry_task_id, retry_error_code, retry_reason):
                    return  # Retry initiated, don't publish yet

            with self._command_lock:
                if retry_task_id is not None and self.task_id != retry_task_id:
                    return

                # Publish and reset
                self.last_command_result = result
                if self._publish_to_zenoh(self.command_is_completed_pub, result.as_payload()) and result.is_completed:
                    self._record_recently_completed_own_command()
                    self._reset_async_command_state()

        except RpcError as e:
            code = e.code()
            is_deadline = code == StatusCode.DEADLINE_EXCEEDED
            is_unavailable = code == StatusCode.UNAVAILABLE
            if is_deadline:
                self.logger.warning(f'Timeout in {method_name}, publishing cached value')
            elif is_unavailable:
                self.logger.warning(f'UNAVAILABLE in {method_name}, will reconstruct client')
            else:
                self._log_error('RPC', method_name, e)

            fail_recovery_needed = False
            immediate_reconnect_needed = False
            with self._command_lock:
                if is_unavailable:
                    immediate_reconnect_needed = True
                    self._first_grpc_failure_time = None
                elif is_deadline and self.task_id:
                    now = time.monotonic()
                    if self._first_grpc_failure_time is None:
                        self._first_grpc_failure_time = now
                    elif now - self._first_grpc_failure_time >= self.grpc_stuck_threshold:
                        stuck_task = self.task_id
                        self._log_error_msg(
                            f'gRPC stuck for {self.grpc_stuck_threshold}s on task {stuck_task}; '
                            f'publishing is_completed=True (internal error_code=-4) and reconstructing client')
                        # error_code=-4 is internal-only; CommandCompletion.as_payload() drops it.
                        # The signal that reaches RMF is is_completed=True, which unblocks the
                        # adapter from treating the task as still running.
                        fail_result = CommandCompletion(
                            task_id=stuck_task,
                            is_completed=True,
                            success=False,
                            error_code=-4,
                        )
                        self.last_command_result = fail_result
                        self._publish_to_zenoh(self.command_is_completed_pub, fail_result.as_payload())
                        self._reset_async_command_state()
                        self._first_grpc_failure_time = None
                        fail_recovery_needed = True

                if not (fail_recovery_needed or immediate_reconnect_needed) and self.last_command_result:
                    self._publish_to_zenoh(self.command_is_completed_pub, self.last_command_result.as_payload())

            if fail_recovery_needed or immediate_reconnect_needed:
                self._reconstruct_kachaka_client()
        except Exception as e:
            self._log_error('Unexpected', method_name, e)

    async def _should_retry_command(self, error_code: int, reason: Optional[str] = None) -> bool:
        """Determine if a command should be retried based on its error code.

        Args:
            error_code (int): The error code from the failed command
            reason (str, optional): The completion reason of the failure.

        Returns:
            bool: True if the command should be retried, False otherwise
        """
        if reason is not None and reason.startswith('undock_'):
            # The retry path re-executes last_command as a whole, which for a
            # failed departure move means undocking again -- up to max_retries
            # times against the same obstacle. The undock prefix is decided
            # freshly on the next RMF command instead (Issue #51).
            self.logger.info(f'Undock failure ({reason}); not retriable on Kachaka side')
            return False
        # Negative error codes are internal signals to RMF (replan); never retry on Kachaka side.
        if error_code < 0:
            self.logger.info(f'Internal error code {error_code}; not retriable on Kachaka side')
            return False
        try:
            # error_code=0 with success=False typically means the navigation was cancelled
            # (e.g., due to temporary obstacles from LiDAR noise). This should be retried.
            if error_code == 0:
                self.logger.info('error_code=0 with failure - navigation may have been cancelled, will retry')
                return True

            # Get error code details from Kachaka API
            error_code_dict = await self.run_method('get_robot_error_code')

            if not isinstance(error_code_dict, dict):
                self.logger.warning(f'Unexpected error code dict format: {error_code_dict}')
                # If we can't get error details, allow retry
                return True

            # Look up the error code
            error_info = error_code_dict.get(str(error_code))
            if not error_info:
                self.logger.warning(f'Error code {error_code} not found in error code dictionary')
                # Unknown error code, allow retry
                return True

            # Get the error type
            error_type = error_info.get('errorType', '')
            self._log_info(f'Error code {error_code}: type={error_type}, title={error_info.get("title", "")}')

            # Check if this error type should trigger a retry
            return error_type in self.retry_on_error_types

        except Exception as e:
            self.logger.error(f'Error checking if command should retry: {str(e)}')
            # On error, be conservative and allow retry
            return True

    def _to_dict(
        self, response: Union[dict, list, RepeatedCompositeContainer,
                              object]) -> Union[dict, list, RepeatedCompositeContainer]:
        """Convert a response object to a dictionary or list.

        Args:
            response (Union[dict, list, RepeatedCompositeContainer, object]): The response object to convert.

        Returns:
            Union[dict, list, RepeatedCompositeContainer]: The converted response object.
        """
        if response.__class__.__module__ == 'kachaka_api_pb2':
            return MessageToDict(response)
        if isinstance(response, (tuple, list, RepeatedCompositeContainer)):
            return [self._to_dict(item) for item in response]
        return response

    def _status_query_handler(self, query: zenoh.Query) -> None:
        """Handle queries for robot status.

        Args:
            query (zenoh.Query): The received query
        """
        try:
            status_data = {
                'robot_name': self.robot_name,
                'pose': self.last_pose.as_list(),
                'map_name': self.map_state.telemetry_map_name,
                'battery': self.last_battery,
                'command_is_completed': self.last_command_result.as_payload() if self.last_command_result else {},
            }
            reply_value = json.dumps(status_data).encode()
            query.reply(query.key_expr, reply_value, encoding=zenoh.Encoding.APPLICATION_JSON)
            self.logger.debug('Status query replied')
        except Exception as e:
            self.logger.error(f'Error handling status query: {str(e)}')

    async def periodic_command_check(self) -> None:
        """Periodically check for new commands from the command server using Query.

        This replaces the push-based subscriber model with a pull-based query model
        to improve reliability in case of temporary network disconnections.
        """
        self.logger.info('Starting periodic command check...')
        while True:
            try:
                # Query the command server for the latest command using Querier
                replies = self.command_querier.get()

                for reply in replies:
                    if reply.ok:
                        command = json.loads(reply.ok.payload.to_string())
                        command_id = command.get('id')

                        # Check if this is a new command
                        if command_id and command_id != self.last_command_id:
                            self._log_info(f'New command: {command["method"]} (ID: {command_id})')

                            # Update last command ID to prevent duplicate processing
                            self.last_command_id = command_id

                            # Process the command using unified logic
                            try:
                                self._execute_command(command)
                            except Exception as e:
                                self._log_error_msg(f'Error processing command: {str(e)}')
                        else:
                            self.logger.debug(f'Skipping duplicate command ID: {command_id}')
                    else:
                        error_payload = (reply.err.payload.to_string()
                                         if reply.err and reply.err.payload else 'Unknown error')
                        if error_payload == 'Timeout':
                            self.logger.debug('Query timeout (normal if no new commands)')
                        else:
                            self.logger.warning(f'Reply error: {error_payload}')

            except Exception as e:
                self.logger.debug(f'Command check error or no new commands: {e}')

            # Wait before next check
            await asyncio.sleep(self.command_check_interval)

    def _complete_superseded_task(self, new_task_id: Optional[str]) -> None:
        """Mark the current active task as superseded before accepting a new one."""
        if not self.task_id or self.task_id == new_task_id:
            return

        self._log_warning(f'Superseding active task {self.task_id} with new task {new_task_id}')
        # error_code=-3: task was replaced by a newer command before completion.
        self._publish_command_completion(success=False, error_code=-3)

    def _verify_map_for_navigation(self, requested_map_name: str, task_id: Optional[str] = None) -> bool:
        """Check that the robot is on the requested map before navigating.

        Compares against the telemetry map name, which is the same value
        reported to RMF, so the guard can never disagree with what RMF sees.
        On mismatch the robot is re-queried via gRPC before rejecting: the
        cached value can diverge from the robot when a switch_map result is
        lost or the map is changed outside this bridge (e.g. from the
        smartphone app), and without the re-query a stale cache would reject
        every navigation command until restart.

        Args:
            requested_map_name (str): The RMF-side map name of the navigation command.
            task_id (str, optional): The ID of the command being verified; the
                failure completion is published for this ID even if the active
                task changed meanwhile.

        Returns:
            bool: True if navigation may proceed. False if the robot is on a
                different map or its map could not be determined; a failure
                completion (error_code=-2) is published in that case.
        """
        if requested_map_name == self.map_state.telemetry_map_name:
            return True

        try:
            current_map_name = self._fetch_robot_map_name(self.grpc_status_check_timeout)
        except RpcError as e:
            self._log_error('RPC', '_verify_map_for_navigation', e)
            self._log_warning(
                f'Could not verify current map for navigation to {requested_map_name}. Rejecting navigation command.')
            self._publish_command_completion(success=False, error_code=-2, task_id=task_id, reason='map_mismatch')
            return False

        self.map_state = self.map_state.with_telemetry_map_name(current_map_name)
        self._publish_to_zenoh(self.map_name_pub, current_map_name)

        if requested_map_name == current_map_name:
            self._log_info(
                f'Cached map name was stale; robot is actually on {current_map_name}. Proceeding with navigation.')
            return True

        # Map name mismatch indicates RMF has incorrect floor information.
        # Reject the navigation command and return error to trigger replanning.
        self._log_warning(f'Map name mismatch: requested={requested_map_name}, '
                          f'current={current_map_name}. '
                          f'Rejecting navigation command to prevent navigation to wrong floor coordinates.')
        self._publish_command_completion(success=False, error_code=-2, task_id=task_id, reason='map_mismatch')
        return False

    def _execute_command(self,
                         command: Dict[str, Any],
                         expected_task_id: Optional[str] = None,
                         skip_undock: bool = False) -> None:
        """Unified command execution logic.

        Command state transitions happen under _command_lock, but the heavy
        gRPC work runs outside it (serialized by _dispatch_lock) so the
        telemetry/result loop never blocks on command execution. While the
        dispatch runs, ``dispatching`` is True and publish_result skips
        polling, which prevents pairing the previous command's result with
        the task being dispatched.

        Every failure path publishes a failure completion for the command's
        ID; a command must never be accepted and then fall silent, because
        the fleet adapter would wait for its completion forever (the robot
        side skips re-deliveries of the same ID as duplicates).

        Args:
            command: The command dict ({'id', 'method', 'args'}).
            expected_task_id: When set (retry path), abort the dispatch if the
                active task changed in the meantime.
            skip_undock: When True, do not evaluate the undock prefix and keep
                the active undock context. Used for the phase 2 dispatch of a
                command that has already left the dock (Issue #51); without it
                the reset below would drop the phase context and the freshly
                dispatched original move could be prefixed a second time.
        """
        method_name = 'execute_command'
        new_task_id: Optional[str] = None
        try:
            with self._dispatch_lock:
                with self._command_lock:
                    # Capture the ID before validation so even a malformed
                    # command gets a failure completion for the ID the
                    # adapter is waiting on.
                    new_task_id = command.get('id', None) if isinstance(command, dict) else None
                    if not isinstance(command, dict) or not all(k in command for k in ('method', 'args')):
                        raise ValueError('Invalid command structure')
                    if self.external_control_active:
                        self._log_warning(
                            f'External control active; rejecting RMF command {new_task_id} with external_busy')
                        self._publish_command_completion(success=False,
                                                         error_code=-9,
                                                         task_id=new_task_id,
                                                         reason='external_busy')
                        return
                    if expected_task_id is not None and self.task_id != expected_task_id:
                        self.logger.debug('Skip dispatch for stale task %s (active task: %s)', expected_task_id,
                                          self.task_id)
                        return
                    is_retry = new_task_id is not None and new_task_id == self.task_id

                    # Re-issue of the in-flight command (e.g. RMF replanning after a
                    # communication gap): adopt the new ID without re-executing, so
                    # the completion is reported with the ID the adapter waits for.
                    if (not is_retry and self.task_id and self.last_command and
                            command['method'] == self.last_command.get('method') and
                            command['args'] == self.last_command.get('args')):
                        self._log_info(f'Re-issued command matches in-flight task {self.task_id}; '
                                       f'adopting new ID {new_task_id} without re-execution')
                        # The undock context is keyed by task ID, so it has to
                        # follow the adopted ID. Left on the old one, phase 1's
                        # success would be published as the whole navigation's
                        # success and the original target would never be
                        # dispatched (PR #54 review).
                        if self._undock is not None and self._undock.task_id == self.task_id:
                            self._undock.task_id = new_task_id or ''
                            self._undock.original_command = command
                        self.task_id = new_task_id
                        self.last_command = command
                        self.last_progress_at = time.monotonic()
                        return

                    self._complete_superseded_task(new_task_id)
                    method_name = command['method']
                    method_name = self.method_mapping.get(method_name, method_name)
                    self.task_id = new_task_id

                    if not hasattr(self.kachaka_client, method_name):
                        raise AttributeError(f'Invalid method: {method_name}')

                    self._log_info(f'Executing command: {method_name} (ID: {self.task_id})')
                    self.last_command = command
                    self.last_command_result = None
                    self.current_command_id = None
                    if not skip_undock:
                        self._undock = None
                    self.is_async_command = False
                    self.saw_running = False
                    self.async_command_started_at = None
                    self.last_progress_at = time.monotonic()
                    # Set to the actual dispatch-success time in
                    # _execute_async_stub_dispatch/_execute_sync_method, not
                    # here: grpc_connection_check can stall before the real
                    # send, which would otherwise eat into the command_start
                    # budget before the command was even sent (Issue #34 Plan
                    # §7.2, Codex review ISS34-006 non-blocking finding).
                    self.command_dispatched_at = None
                    self._motion_progress_pose = None
                    self._motion_progress_at = None
                    self.expected_kachaka_method = None
                    self.command_target_map_name = None
                    self.command_target_pose = None
                    self._ignored_result_count = 0
                    if not is_retry:
                        self.retry_count = 0
                    self.dispatching = True
                    # Only move_to_pose/return_home go through
                    # _execute_async_stub_dispatch(), the sole path that can
                    # ever bind current_command_id from a StartCommand
                    # response. switch_map and other synchronous methods
                    # never capture an ownership id at all -- even for their
                    # own in-flight command -- so a RUNNING snapshot
                    # observed while one of them dispatches can never be
                    # confirmed as ours; reconciling it would always
                    # misclassify the sync command's own id as external
                    # (Codex re-review ISS34-040 blocking-B).
                    is_async_dispatch = method_name in ('move_to_pose', 'return_home')

                try:
                    # Heavy gRPC work: outside _command_lock, guarded by dispatching.
                    if method_name == 'switch_map':
                        self._execute_switch_map_sync(command['args'], new_task_id)
                    elif method_name == 'move_to_pose':
                        args = command['args'].copy()
                        map_name = args.pop('map_name', None)
                        if map_name is not None and not self._verify_map_for_navigation(map_name, new_task_id):
                            return
                        target_x = args.get('x')
                        target_y = args.get('y')
                        target_yaw = args.get('yaw', 0.0)
                        # Undock decision runs after the map guard and before
                        # the no-op shortcut, so a micro-rotation requested
                        # while the robot is on the charging contacts can
                        # never be short-circuited to success (Issue #51).
                        if not skip_undock:
                            decision, payload = self._evaluate_undock(map_name, target_x, target_y)
                            if decision == 'reject':
                                self._publish_command_completion(success=False,
                                                                 error_code=-11,
                                                                 task_id=new_task_id,
                                                                 reason=payload)
                                return
                            if decision == 'prefix':
                                undock_args, skip_original = payload
                                self._dispatch_undock_phase(command, undock_args, skip_original, map_name, new_task_id)
                                return
                        # map_name is required for the shortcut (a missing floor can
                        # never be confirmed) and the near-target check runs first
                        # since it is cheap; the fresh floor re-query only happens
                        # for candidates that are otherwise about to short-circuit
                        # (Issue #34 Plan §7.1, Codex review ISS34-006 concern (a)).
                        if (self.noop_enabled and map_name is not None and target_x is not None and
                                target_y is not None and self._is_near_target(
                                    target_x,
                                    target_y,
                                    target_yaw,
                                    distance_tolerance=self.noop_distance_tolerance,
                                    yaw_tolerance=self.noop_yaw_tolerance,
                                ) and self._fresh_floor_matches(map_name)):
                            self._log_info(
                                f'Target within noop tolerance (distance<={self.noop_distance_tolerance}m, '
                                f'yaw<={self.noop_yaw_tolerance}rad); reporting success without calling Kachaka')
                            self._publish_command_completion(success=True, error_code=0, task_id=new_task_id)
                            return
                        self.expected_kachaka_method = 'move_to_pose'
                        self.command_target_map_name = map_name
                        self.command_target_pose = Pose(target_x or 0.0, target_y or 0.0, target_yaw or 0.0)
                        self.logger.debug(f'Current pose: {self.last_pose.as_list()}')
                        self.logger.debug(f'Target pose: x={args.get("x")}, y={args.get("y")}, yaw={args.get("yaw")}')
                        self._execute_async_stub_dispatch(method_name, args, task_id=new_task_id)
                    elif method_name == 'return_home':
                        self.expected_kachaka_method = 'return_home'
                        self._execute_async_stub_dispatch(method_name, command['args'], task_id=new_task_id)
                    else:
                        self.logger.debug(f'{method_name} args: {command["args"]}')
                        self._execute_sync_method(method_name, command['args'], task_id=new_task_id)
                finally:
                    with self._command_lock:
                        self.dispatching = False
                        if is_async_dispatch:
                            self._reconcile_pending_dispatch_snapshot()
                        else:
                            # Discard rather than reconcile: a snapshot from
                            # a sync dispatch's window has no own id to
                            # compare against, so reconciling it here could
                            # only ever misclassify our own command, and
                            # carrying it over to a later, unrelated async
                            # dispatch's reconciliation would be equally
                            # wrong (Codex re-review ISS34-040 blocking-B).
                            self._pending_dispatch_snapshot_id = None
        except (json.JSONDecodeError, ValueError, AttributeError) as e:
            self._log_error_msg(f'Invalid command: {str(e)}')
            if new_task_id:
                self._publish_command_completion(success=False, error_code=-1, task_id=new_task_id)
        except (ConnectionError, RpcError, Exception) as e:
            self._log_error('Unexpected', f'executing command {method_name}', e)
            if new_task_id:
                self._publish_command_completion(success=False, error_code=-1, task_id=new_task_id)

    def _dispatch_undock_phase(
        self,
        command: Dict[str, Any],
        undock_args: Dict[str, Any],
        skip_original: bool,
        map_name: Optional[str],
        task_id: Optional[str],
    ) -> None:
        """Start the departure move of a two-phase undock command (Issue #51).

        The RMF task ID is unchanged: the adapter sees one command, and no
        completion is published until the final phase (see
        _publish_command_completion and _advance_undock_phase).

        Args:
            command (Dict[str, Any]): The original RMF command, replayed as
                phase 2 unless the original target is on the dock being left.
            undock_args (Dict[str, Any]): move_to_pose arguments of the
                departure move.
            skip_original (bool): True when the original target sits on the
                dock being left, so phase 2 is skipped.
            map_name (str, optional): The RMF-side map name of the command.
            task_id (str, optional): The RMF task ID of the command.
        """
        with self._command_lock:
            self._undock = UndockPhase(
                task_id=task_id or '',
                original_command=command,
                skip_original=skip_original,
                started_at=time.monotonic(),
            )
            self.expected_kachaka_method = 'move_to_pose'
            self.command_target_map_name = map_name
            self.command_target_pose = Pose(undock_args['x'], undock_args['y'], undock_args['yaw'])
        self._execute_async_stub_dispatch('move_to_pose', undock_args, task_id=task_id)
        with self._command_lock:
            if self._undock is not None and self._undock.task_id == task_id:
                self._undock.phase1_command_id = self.current_command_id

    def _execute_switch_map_sync(self, args: Dict[str, Any], task_id: Optional[str] = None) -> None:
        """Execute switch_map command synchronously.

        Args:
            args (dict): The arguments for the switch_map method, including:
                - map_name (str): The name of the map to switch to
                - pose (dict, optional): The initial pose on the new map
            task_id (str, optional): The ID of the command being executed.
                Completions are published for this ID so a watchdog-forced
                reset or a newly accepted task can never receive this
                command's result.
        """
        method_name = 'switch_map'
        try:
            if not self.grpc_connection_check(max_retries=self.command_max_retries):
                error_msg = f'Failed to connect to Kachaka API server for method {method_name}'
                self.logger.error(error_msg)
                raise ConnectionError(error_msg)

            map_name = self.map_name_mapping.get(args.get('map_name'), args.get('map_name'))
            map_list = self._to_dict(self.kachaka_client.get_map_list())
            map_id = next((item['id'] for item in map_list if item['name'] == map_name), None)

            if map_id is None:
                self._log_error_msg(f'Map {map_name} not found')
                self._publish_command_completion(success=False, error_code=-1, task_id=task_id)
                return

            current_map_id = self.kachaka_client.get_current_map_id()
            payload = {
                'map_id': map_id,
                'pose': args.get('pose', {
                    'x': 0.0,
                    'y': 0.0,
                    'theta': 0.0
                }),
            }

            # switch map only if the map is different from the current map id
            # because switch_map method takes long time to complete
            if map_id == current_map_id:
                rmf_map_name = args.get('map_name')
                self.map_state = self.map_state.with_telemetry_map_name(rmf_map_name)
                self._publish_to_zenoh(self.map_name_pub, rmf_map_name)
                self.logger.info('Nothing to do - already on target map')
                self._publish_command_completion(success=True, error_code=0, task_id=task_id)
            else:
                # Suppress automatic completion in _execute_sync_method so we can
                # publish map_name to Zenoh *before* notifying RMF of completion.
                # This prevents a race where RMF receives the completion, queries
                # the robot's map_name (still the old floor), and issues a
                # navigation command with stale floor coordinates.
                response = self._execute_sync_method('switch_map', payload, publish_completion=False, task_id=task_id)
                result_dict = self._extract_command_result(response)
                success = result_dict.get('success', False) if result_dict is not None else True
                if success:
                    rmf_map_name = args.get('map_name')
                    self.map_state = self.map_state.with_telemetry_map_name(rmf_map_name)
                    self._publish_to_zenoh(self.map_name_pub, rmf_map_name)
                    # Pose must precede completion: RMF reading (new map_name, stale pose) triggers "too far" replan.
                    target_pose = payload['pose']
                    new_pose = Pose(target_pose.get('x', 0.0), target_pose.get('y', 0.0),
                                    target_pose.get('theta', 0.0))
                    self.last_pose = new_pose
                    self._publish_to_zenoh(self.pose_pub, new_pose.as_list())
                    self.logger.info(
                        'Published map_name=%s and target pose to Zenoh after successful switch_map',
                        rmf_map_name,
                    )
                    self._publish_command_completion(success=True, error_code=0, task_id=task_id)
                else:
                    self._publish_command_completion(success=False, error_code=-1, task_id=task_id)
        except RpcError as e:
            self._log_error('RPC', method_name, e)
            if not self._recover_interrupted_switch_map(args, task_id):
                self._publish_command_completion(success=False, error_code=-1, task_id=task_id)
        except Exception as e:
            self._log_error('Unexpected', method_name, e)
            if not self._recover_interrupted_switch_map(args, task_id):
                self._publish_command_completion(success=False, error_code=-1, task_id=task_id)

    def _recover_interrupted_switch_map(self, args: Dict[str, Any], task_id: Optional[str] = None) -> bool:
        """Complete a failed-looking switch_map that actually took effect.

        A switch_map gRPC call can succeed on the robot while its response is
        lost (e.g. DEADLINE_EXCEEDED). Reporting failure in that case leaves
        the cached map name on the old floor while the robot is already on
        the new one, so every later move_to_pose would be rejected by the map
        guard. Re-query the robot and, when it is already on the target map,
        publish the fresh map_name and actual pose before completing the
        command as a success, mirroring the normal success path.

        Args:
            args (dict): The original switch_map arguments.
            task_id (str, optional): The ID of the command being recovered;
                the success completion is published for this ID.

        Returns:
            bool: True if the switch was confirmed on the robot and a success
                completion was published. False if the robot is not on the
                target map or could not be queried; the caller should publish
                the failure completion in that case.
        """
        rmf_map_name = args.get('map_name')
        if rmf_map_name is None:
            return False
        try:
            current_map_name = self._fetch_robot_map_name(self.grpc_status_check_timeout)
            if current_map_name != rmf_map_name:
                return False
            response = self.kachaka_client.stub.GetRobotPose(pb2.GetRequest(), timeout=self.grpc_status_check_timeout)
            pose = Pose(response.pose.x, response.pose.y, response.pose.theta)
        except Exception as e:
            self._log_error('Unexpected', '_recover_interrupted_switch_map', e)
            return False

        self._log_info(f'switch_map result was lost but robot is already on {rmf_map_name}; completing as success')
        self.map_state = self.map_state.with_telemetry_map_name(rmf_map_name)
        self._publish_to_zenoh(self.map_name_pub, rmf_map_name)
        # Pose must precede completion: RMF reading (new map_name, stale pose) triggers "too far" replan.
        self.last_pose = pose
        self._publish_to_zenoh(self.pose_pub, pose.as_list())
        self._publish_command_completion(success=True, error_code=0, task_id=task_id)
        return True

    @staticmethod
    def _extract_command_result(response: Any) -> Optional[Dict[str, Any]]:  # noqa: ANN401
        """Return the command Result dict from a method response, or None.

        The kachaka high-level client's start_command() unwraps
        StartCommandResponse and returns the bare Result message, so async
        commands (move_to_pose, return_home) and switch_map arrive as
        {'success': ..., 'errorCode': ...} with no nested 'result' key. The
        wrapped {'result': {...}} shape is also accepted for safety.

        MessageToDict drops zero-valued fields, so a plain success serializes
        to {'success': True} and a failure with a non-zero code to
        {'errorCode': N}. A bare Result with success=False and error_code=0
        therefore serializes to {} and is indistinguishable from a response
        that carries no Result at all; both yield None and callers treat None
        as success. (The wrapped {'result': {...}} branch instead returns the
        nested dict verbatim, so an empty {'result': {}} yields {} and is read
        as success=False.) This ambiguous bare case does not occur for the
        dispatch-time results handled here, where failures always carry a
        non-zero error code.
        """
        if not isinstance(response, dict):
            return None
        if isinstance(response.get('result'), dict):
            return response['result']
        if 'success' in response or 'errorCode' in response:
            return response
        return None

    def _build_stub_start_command_request(self, method_name: str, args: Dict[str, Any]) -> pb2.StartCommandRequest:
        """Build the StartCommandRequest for a stub-direct async dispatch.

        Mirrors the pb2.Command the installed kachaka_api library's
        move_to_pose()/return_home() wrappers build internally (see
        kachaka_api/base.py), so calling stub.StartCommand() with this
        request has the same effect on the robot as going through the
        wrapper -- except the response keeps command_id, which the wrapper
        discards (Issue #34 Plan §7.3/§7.4, Codex re-review ISS34-010
        blocking-3).
        """
        if method_name == 'move_to_pose':
            command = pb2.Command(move_to_pose_command=pb2.MoveToPoseCommand(
                x=args.get('x', 0.0),
                y=args.get('y', 0.0),
                yaw=args.get('yaw', 0.0),
            ))
        elif method_name == 'return_home':
            command = pb2.Command(return_home_command=pb2.ReturnHomeCommand())
        else:
            raise ValueError(f'Unsupported stub dispatch method: {method_name}')
        return pb2.StartCommandRequest(command=command, cancel_all=args.get('cancel_all', True))

    def _execute_async_stub_dispatch(
        self,
        method_name: str,
        args: Dict[str, Any],
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Dispatch move_to_pose/return_home via stub.StartCommand() directly.

        The installed kachaka_api library's move_to_pose()/return_home()
        wrappers call start_command(), which discards
        StartCommandResponse.command_id whenever wait_for_completion is
        False (the mode this bridge always uses): ``if not
        response.result.success or not wait_for_completion: return
        response.result``. Calling stub.StartCommand() directly instead
        keeps command_id, so bind (§7.3) and ownership (§7.4) judgments can
        key off ID equality from dispatch onward. This path replaces
        _execute_sync_method for move_to_pose/return_home only; every other
        method_mapping-driven method still goes through the generic wrapper
        dispatch there (Issue #34 Plan §7.3/§7.4, Codex re-review ISS34-010
        blocking-3).

        Args:
            method_name (str): 'move_to_pose' or 'return_home'.
            args (Dict[str, Any]): The command arguments (x/y/yaw for
                move_to_pose; cancel_all applies to both, defaulting to True).
            task_id (str, optional): The ID of the command being executed.
                Completions are published for this ID so they can never be
                attributed to a different (newer) task.

        Returns:
            Dict[str, Any]: {'result': {...}, 'commandId': ...} shaped like
            _to_dict(StartCommandResponse), so _update_current_command_id
            and the existing async-started bookkeeping apply unchanged.
        """
        self.is_async_command = True
        try:
            if not self.grpc_connection_check(max_retries=self.command_max_retries):
                error_msg = f'Failed to connect to Kachaka API server for method {method_name}'
                self.logger.error(error_msg)
                self._publish_command_completion(success=False, error_code=-1, task_id=task_id)
                raise ConnectionError(error_msg)

            request = self._build_stub_start_command_request(method_name, args)
            response_dict = self._to_dict(self.kachaka_client.stub.StartCommand(request))

            result_dict = response_dict.get('result', {}) if isinstance(response_dict, dict) else {}
            success = result_dict.get('success', False)
            error_code = result_dict.get('errorCode', 0)

            if success and not response_dict.get('commandId'):
                # Fail closed instead of leaving current_command_id unbound.
                # Kachaka API 3.14.4.0's StartCommandResponse normally
                # carries a non-empty command_id on success, and the
                # client wrapper itself relies on it; an empty id here is an
                # anomaly, not routine behavior. The previous design let
                # publish_result() bind ownership later by matching the
                # observed command's type/target instead, but a same-
                # type/target external command racing this dispatch could
                # bind onto it and have its success misreported as this
                # task's own (Codex re-review ISS34-037 blocking-1). Never
                # bind without a captured id; fail the task immediately so
                # the fleet adapter can retry/replan instead of waiting on
                # a command we can no longer distinguish from anyone else's.
                self._log_error_msg(
                    f'StartCommand for {method_name} succeeded but returned no command_id; failing closed')
                self._publish_command_completion(success=False,
                                                 error_code=-10,
                                                 task_id=task_id,
                                                 reason='missing_command_id')
            elif success:
                # Origin of the command_start timeout is dispatch success
                # (here), not command acceptance in _execute_command, so a
                # slow grpc_connection_check above does not eat into the
                # budget before the command was actually sent (Issue #34
                # Plan §7.2, Codex review ISS34-006 non-blocking finding).
                #
                # command_dispatched_at/async_command_started_at and
                # current_command_id are bound inside the same _command_lock
                # acquisition so monitor_external_control() (a different
                # thread) can never observe dispatched_at set while
                # current_command_id is still unbound -- that gap let a
                # racing own returnHome be misclassified as external and
                # preempted (Codex re-review ISS34-016 blocking-1).
                now = time.monotonic()
                with self._command_lock:
                    self.async_command_started_at = now
                    self.command_dispatched_at = now
                    self._update_current_command_id(response_dict, method_name)
                self.logger.info(f'Async command {method_name} started')
            else:
                self._log_warning(f'Command {method_name} failed with error_code={error_code}')
                self._publish_command_completion(success=False, error_code=error_code, task_id=task_id)

            return response_dict

        except RpcError as e:
            self.logger.error(f'RPC error in {method_name}: {e.details()}')
            self._publish_command_completion(success=False, error_code=-1, task_id=task_id)
            raise
        except Exception as e:
            self.logger.error(f'Unexpected error in {method_name}: {str(e)}')
            self._publish_command_completion(success=False, error_code=-1, task_id=task_id)
            raise

    def _execute_sync_method(
        self,
        method_name: str,
        args: Dict[str, Any],
        publish_completion: bool = True,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a method synchronously and optionally publish completion status.

        Args:
            method_name (str): The name of the method to execute
            args (Dict[str, Any]): The arguments for the method
            publish_completion (bool): Whether to automatically publish completion
                status. Set to False when the caller needs to perform additional
                state updates (e.g., publishing map_name) before notifying RMF.
            task_id (str, optional): The ID of the command being executed.
                Completions are published for this ID so they can never be
                attributed to a different (newer) task.

        Returns:
            Dict[str, Any]: The response from the method
        """
        try:
            if not self.grpc_connection_check(max_retries=self.command_max_retries):
                error_msg = f'Failed to connect to Kachaka API server for method {method_name}'
                self.logger.error(error_msg)
                if publish_completion:
                    self._publish_command_completion(success=False, error_code=-1, task_id=task_id)
                raise ConnectionError(error_msg)

            method = getattr(self.kachaka_client, method_name)
            response = self._to_dict(method(**args))

            result_dict = self._extract_command_result(response)
            if result_dict is not None:
                success = result_dict.get('success', False)
                error_code = result_dict.get('errorCode', 0)

                if self.is_async_command and success:
                    # Async command started, don't publish completion yet.
                    # publish_result polls GetCommandState/GetLastCommandResult
                    # and publishes completion after RUNNING state is seen.
                    now = time.monotonic()
                    self.async_command_started_at = now
                    # Origin of the command_start timeout is dispatch success
                    # (here), not command acceptance in _execute_command, so a
                    # slow grpc_connection_check above does not eat into the
                    # budget before the command was actually sent (Issue #34
                    # Plan §7.2, Codex review ISS34-006 non-blocking finding).
                    with self._command_lock:
                        self.command_dispatched_at = now
                    self.logger.info(f'Async command {method_name} started')
                elif success:
                    self.logger.info(f'Command {method_name} completed successfully')
                    if publish_completion:
                        self._publish_command_completion(success=True, error_code=0, task_id=task_id)
                else:
                    self._log_warning(f'Command {method_name} failed with error_code={error_code}')
                    if publish_completion:
                        self._publish_command_completion(success=False, error_code=error_code, task_id=task_id)
            else:
                # Response carries no success/result info; assume success.
                self.logger.info(f'Command {method_name} executed successfully')
                if publish_completion:
                    self._publish_command_completion(success=True, error_code=0, task_id=task_id)

            return response

        except RpcError as e:
            self.logger.error(f'RPC error in {method_name}: {e.details()}')
            self._publish_command_completion(success=False, error_code=-1, task_id=task_id)
            raise
        except Exception as e:
            self.logger.error(f'Unexpected error in {method_name}: {str(e)}')
            self._publish_command_completion(success=False, error_code=-1, task_id=task_id)
            raise

    def _record_recently_completed_own_command(self) -> None:
        """Remember a completing own command's command_id for the ownership grace period.

        Applies to any completing method (move_to_pose, dock/return_home,
        ...) that had a bound command_id, not only dock -- generalized from
        the previous dock-only check so a briefly-lingering RUNNING state
        after any own command's completion or cancellation/supersession is
        still recognized as ours (Issue #34 Plan §5, concern (b)). Must run
        on every completion path immediately before
        _reset_async_command_state() clears current_command_id, not only the
        explicit _publish_command_completion() path: publish_result()'s
        normal async-success completion calls _reset_async_command_state()
        directly and previously bypassed this recording entirely, so
        _is_own_command_id() had no record to recognize a briefly-lingering
        RUNNING command right after an ordinary success (Codex re-review
        ISS34-010 recommendation-2).
        """
        if self.current_command_id is not None:
            self._recently_completed_own_command_id = self.current_command_id
            self._recently_completed_own_command_until = time.monotonic() + self.own_return_home_retention

    def _reset_async_command_state(self) -> None:
        """Reset all async command tracking state.

        Called after command completion (success or failure) to prepare
        for the next command.
        """
        self.task_id = None
        self.retry_count = 0
        self.is_async_command = False
        self.saw_running = False
        self.current_command_id = None
        self.async_command_started_at = None
        self.last_progress_at = None
        self._ignored_result_count = 0
        self.command_dispatched_at = None
        self._motion_progress_pose = None
        self._motion_progress_at = None
        self.expected_kachaka_method = None
        self.command_target_map_name = None
        self.command_target_pose = None
        self._undock = None

    def _reconstruct_kachaka_client(self) -> bool:
        """Recreate the KachakaApiClient to recover from a dead gRPC channel.

        Closes the old channel explicitly before creating a new one to
        release resources promptly. In-flight RPCs hold the old client
        through their local references, so swapping the attribute is safe.
        """
        with self._client_lock:
            old_client = self.kachaka_client
            try:
                self._log_info('Reconstructing KachakaApiClient to recover gRPC channel')
                new_client = (KachakaApiClientWithKeepalive(self.kachaka_access_point)
                              if self.kachaka_access_point else KachakaApiClientWithKeepalive())
                self.kachaka_client = new_client
                self._log_info('KachakaApiClient reconstructed successfully')
                try:
                    if hasattr(old_client, 'close'):
                        old_client.close()
                except Exception as ce:
                    self.logger.warning(f'Failed to close old client channel: {ce}')
                return True
            except Exception as e:
                self._log_error('Unexpected', '_reconstruct_kachaka_client', e)
                return False

    def _publish_command_completion(
        self,
        success: bool,
        error_code: int,
        task_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """Publish command completion status to Zenoh.

        Idempotent per task: when a completion for the same task ID was
        already published (e.g. _execute_sync_method published a failure and
        the outer _execute_command handler tries again), the duplicate is
        skipped. Command tracking state is reset only when the published ID
        is still the active task, so a late completion from a stale dispatch
        can never wipe the state of a newly accepted task.

        Args:
            success (bool): Whether the command succeeded
            error_code (int): The error code (0 if success)
            task_id (str, optional): Explicit task ID to publish for.
                Defaults to the active task.
            reason (str, optional): Issue #34 Plan §5.2 completion reason
                (e.g. 'start_timeout', 'external_preempted'). Omitted from
                the payload when None.
        """
        with self._command_lock:
            target_task_id = task_id if task_id is not None else self.task_id
            if not target_task_id:
                return False

            prev = self.last_command_result
            if prev and prev.is_completed and prev.task_id == target_task_id:
                self.logger.debug(f'Completion for task {target_task_id} already published; skipping duplicate')
                return True

            undock = self._undock
            if undock is not None and undock.task_id == target_task_id and undock.phase != UndockPhase.FINAL:
                if success:
                    # Only the final phase may report the task as done: a
                    # success published while the departure move is still the
                    # active phase would tell RMF the whole navigation
                    # finished at the undock endpoint (Issue #51).
                    self._log_error_msg(f'Success completion for task {target_task_id} requested from non-final '
                                        f'undock phase {undock.phase}; not publishing')
                    return False
                # A failure ends the task outright, so the phase is finalized
                # here rather than dropped: withholding it would leave RMF
                # waiting for the 180s completion watchdog.
                undock.phase = UndockPhase.FINAL

            completion_result = CommandCompletion(
                task_id=target_task_id,
                is_completed=True,
                success=success,
                error_code=error_code,
                reason=reason,
            )

            self.last_command_result = completion_result
            self.logger.debug(f'Publishing command completion: {completion_result.as_payload()}')
            if not self._publish_to_zenoh(self.command_is_completed_pub, completion_result.as_payload()):
                self._log_warning(f'Failed to publish completion for task {target_task_id}; keeping state for retry')
                return False

            # Clear all async command tracking state after publishing
            if target_task_id == self.task_id:
                self._record_recently_completed_own_command()
                self._reset_async_command_state()
            return True

    def grpc_connection_check(self, max_retries: Optional[int] = None) -> bool:
        """Check if the gRPC connection to Kachaka API server is alive.

        Attempts to make a simple API call (get_robot_pose) to check if the
        connection is working. If the connection fails with UNAVAILABLE status,
        it will retry up to max_retries times with a sleep interval between retries.

        For other RPC errors, the error is re-raised as they indicate issues
        other than connection problems.

        Args:
            max_retries (int, optional): The maximum number of retries to check the connection.
                                       Uses config value if not specified.

        Returns:
            bool: True if the connection is alive, False if it could not be
                 established after max_retries

        Raises:
            RpcError: If an RPC error occurs that is not related to connection
                      availability (not StatusCode.UNAVAILABLE)
        """
        if max_retries is None:
            max_retries = self.max_retries
        sleep_time = self.grpc_connection_sleep
        retry_count = 0
        last_error = None

        for _ in range(max_retries):
            try:
                self.kachaka_client.get_robot_pose()
                if retry_count > 0:
                    self._log_info(f'gRPC connection restored after {retry_count} retries')
                return True
            except RpcError as e:
                retry_count += 1
                last_error = e
                if e.code() == StatusCode.UNAVAILABLE:
                    self._log_error_msg(f'gRPC connection error ({retry_count}/{max_retries}): {e.details()}')
                    time.sleep(sleep_time)
                else:
                    self._log_error_msg(f'Unexpected gRPC error: {e.details()} (code: {e.code()})')
                    raise e

        error_details = last_error.details() if last_error else 'Unknown error'
        self._log_error_msg(
            f'Failed to connect to gRPC server after {max_retries} attempts. Last error: {error_details}')
        return False


def main() -> None:
    """Run the main function to run the KachakaApiClientByZenoh.

    This function parses command-line arguments, creates an instance of
    KachakaApiClientByZenoh, subscribes to the command topic, and publishes
    the robot's pose, current map name, and command state to Zenoh in a loop.
    """
    parser = argparse.ArgumentParser(description='Kachaka API Client for OpenRMF via Zenoh')
    parser.add_argument(
        '--log-level',
        type=str,
        default=None,
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        help='Set the logging level (default: INFO, can also be set via LOG_LEVEL env var)',
    )
    args = parser.parse_args()

    zenoh_router_ap = os.getenv('ZENOH_ROUTER_ACCESS_POINT')
    kachaka_access_point = os.getenv('KACHAKA_ACCESS_POINT')
    robot_name = os.getenv('ROBOT_NAME', 'kachaka')
    config_file = os.getenv('CONFIG_FILE', 'config.yaml')
    # Priority: CLI argument > environment variable > default (INFO)
    log_level = args.log_level or os.getenv('LOG_LEVEL', 'INFO')

    if not zenoh_router_ap:
        raise ValueError('ZENOH_ROUTER_ACCESS_POINT must be set as an environment variable.')

    try:
        node = KachakaApiClientByZenoh(zenoh_router_ap, kachaka_access_point, robot_name, config_file, log_level)

        try:
            # Start the node with periodic command checking via Queryable pattern
            msg = 'Starting KachakaApiClientByZenoh with periodic command checking...'
            node.logger.info(msg)
            print(msg)

            consecutive_errors = 0
            max_consecutive_errors = node.max_consecutive_errors
            sleep_time = node.main_loop_sleep

            # Create a separate thread for command checking
            def run_command_check() -> None:
                asyncio.run(node.periodic_command_check())

            command_thread = threading.Thread(target=run_command_check, daemon=True)
            command_thread.start()

            while True:
                publish_fns = [
                    node.publish_pose,
                    node.publish_battery,
                    node.publish_map_name,
                    node.monitor_external_control,
                    node.publish_state,
                    node.publish_result,
                ]
                any_success = False
                for publish_fn in publish_fns:
                    try:
                        asyncio.run(publish_fn())
                        any_success = True
                    except Exception as e:
                        node.logger.error(f'Error in {publish_fn.__name__}: {e}')

                if any_success:
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    if consecutive_errors >= max_consecutive_errors:
                        msg = f'Too many consecutive full failures ({consecutive_errors}), exiting'
                        node.logger.error(msg)
                        print(msg)
                        raise RuntimeError(msg)

                time.sleep(sleep_time)

        except KeyboardInterrupt:
            print('Interrupted by user, cleaning up...')
        finally:
            # Clean up queryable and querier
            node.status_queryable.undeclare()
            node.command_querier.undeclare()
            # Clean up zenoh session
            node.session.delete(f'robots/{robot_name}/**')
            node.session.close()
            node.logger.info('Closed Zenoh session')
            node.logger.info('Exiting KachakaApiClientByZenoh')

    except Exception as e:
        print(f'Failed to initialize or run KachakaApiClientByZenoh: {str(e)}')
        logging.error(f'Failed to initialize or run KachakaApiClientByZenoh: {str(e)}')
        raise


if __name__ == '__main__':
    main()
