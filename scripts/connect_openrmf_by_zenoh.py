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

    # Consecutive command_id mismatches tolerated before undoing a binding
    # (one mismatch is observed roughly once per main loop iteration).
    MAX_IGNORED_MISMATCHES = 5

    # Maps the internal (post method_mapping) Kachaka method name to the
    # protobuf Command oneof field MessageToDict() produces, used by
    # _validate_command_match() to confirm a result actually belongs to the
    # command type that was dispatched (Issue #34 Plan §7.3).
    COMMAND_TYPE_FIELD = {
        'move_to_pose': 'moveToPoseCommand',
        'return_home': 'returnHomeCommand',
    }

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
        self.command_check_interval = intervals.get('command_check', 4.0)

        # Load navigation tolerance settings (Issue #34 Plan §7.1-§7.3)
        self.noop_distance_tolerance = float(navigation.get('noop_distance_tolerance', 0.15))
        self.noop_yaw_tolerance = float(navigation.get('noop_yaw_tolerance', 0.10))
        self.progress_distance_delta = float(navigation.get('progress_distance_delta', 0.02))
        self.progress_yaw_delta = float(navigation.get('progress_yaw_delta', 0.02))
        self.command_match_distance_tolerance = float(navigation.get('command_match_distance_tolerance', 0.75))
        self.command_match_yaw_tolerance = float(navigation.get('command_match_yaw_tolerance', 0.35))
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
        # True while an externally-triggered returnHome (not issued by this
        # bridge) is observed running on the robot (Issue #34 Plan §7.4).
        self.external_control_active = False
        self._external_command_id: Optional[str] = None
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
        """Extract and store command_id for async commands if available."""
        if not isinstance(response, dict):
            return
        command_id = response.get('commandId')
        if command_id:
            self.current_command_id = command_id
            self.logger.debug(f'Captured command_id {command_id} for {method_name}')
        else:
            self.logger.warning(f'Async command {method_name} did not return commandId; awaiting state update')

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
        """Track command_id mismatches and undo a wrong binding when persistent.

        Binding the wrong command_id (e.g. the previous command's RUNNING state
        observed right after cancel_all) would otherwise make every later
        state/result be ignored and the completion never reach RMF.
        """
        self._ignored_result_count += 1
        if self._ignored_result_count >= self.MAX_IGNORED_MISMATCHES:
            self._log_warning(f'{source} command_id {observed_id} mismatched bound '
                              f'{self.current_command_id} {self._ignored_result_count} consecutive times; '
                              'unbinding to allow re-binding')
            self.current_command_id = None
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
        return (time.monotonic() - self.command_dispatched_at) >= self.command_start_timeout

    def _motion_progress_timeout_expired(self) -> bool:
        """Return True when the pose has not moved within motion_progress_timeout while RUNNING."""
        if self._motion_progress_at is None:
            return False
        return (time.monotonic() - self._motion_progress_at) >= self.motion_progress_timeout

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
        if not isinstance(command_dict, dict) or expected_field not in command_dict:
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

    def _is_own_return_home(self, observed_command_id: Optional[str]) -> bool:
        """Return True when a RUNNING returnHomeCommand belongs to our own dispatched dock.

        RMF issues return_home via the 'dock' RMF method (mapped to Kachaka's
        return_home). Before the command_id has been bound (early in the
        dispatch), the observed id cannot yet be compared, so any RUNNING
        return_home while our own dock task is in flight is treated as ours
        (Issue #34 Plan §7.4).
        """
        if self.task_id is None or self.last_command is None:
            return False
        if self.last_command.get('method') != 'dock':
            return False
        return self.current_command_id is None or self.current_command_id == observed_command_id

    def _handle_external_return_home_started(self, command_id: Optional[str]) -> None:
        """Preempt any active RMF task and mark external control as active."""
        preempted_task_id = self.task_id
        if preempted_task_id is not None:
            self._log_error_msg(f'External returnHome (command_id={command_id}) observed; '
                                f'preempting active RMF task {preempted_task_id}')
            self._publish_command_completion(success=False,
                                             error_code=-8,
                                             task_id=preempted_task_id,
                                             reason='external_preempted')
        else:
            self._log_info(f'External returnHome (command_id={command_id}) observed while idle')
        self.external_control_active = True
        self._external_command_id = command_id

    def _handle_external_return_home_ended(self) -> None:
        """Mark external control as no longer active."""
        self._log_info(f'External returnHome (command_id={self._external_command_id}) ended')
        self.external_control_active = False
        self._external_command_id = None

    async def monitor_external_control(self) -> None:
        """Detect Kachaka commands not issued by this bridge (Issue #34 Plan §7.4).

        Runs every main loop iteration regardless of whether an RMF task is
        active, so an externally-triggered returnHome (e.g. from the Kachaka
        app or a low-battery auto-return) is observed even while idle.
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
            is_return_home = isinstance(command_dict, dict) and 'returnHomeCommand' in command_dict
            is_running = self._is_running_state(state_res.get('state'))
            observed_id = state_res.get('commandId')

            is_external_now = (is_return_home and is_running and observed_id is not None and
                               not self._is_own_return_home(observed_id))

            if is_external_now and not self.external_control_active:
                self._handle_external_return_home_started(observed_id)
            elif not is_external_now and self.external_control_active:
                self._handle_external_return_home_ended()

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

    async def _handle_command_retry(self, task_id: str, error_code: int) -> bool:
        """Handle retry logic for failed commands.

        Args:
            task_id: The task ID that produced the failed result
            error_code: The error code from the failed command

        Returns:
            True if retry was initiated (caller should return early), False otherwise
        """
        should_retry = await self._should_retry_command(error_code)
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

                if self.dispatching:
                    # A dispatch is running outside the lock; polling now could
                    # pair the previous command's result with the new task.
                    return

                state_res = self._get_command_state_response()
                self.logger.debug(f'GetCommandState response: {state_res}')
                command_id = state_res.get('commandId')
                state_value = state_res.get('state')

                if self.is_async_command and not self.saw_running and self._command_start_timeout_expired():
                    self._handle_async_timeout(error_code=-6, reason='start_timeout')
                    return

                if self.is_async_command:
                    if command_id:
                        if self.current_command_id is None and self._is_running_state(state_value):
                            self.current_command_id = command_id
                            self._ignored_result_count = 0
                            self.logger.debug(f'Bound command_id {command_id} to task {self.task_id}')
                        elif self.current_command_id and command_id != self.current_command_id:
                            self._note_ignored_mismatch('command state', command_id)
                            return
                    else:
                        self.logger.debug(
                            'Command state missing commandId for task %s (state=%s)',
                            self.task_id,
                            state_value,
                        )

                if self._is_running_state(state_value):
                    self.saw_running = True
                    self.last_progress_at = time.monotonic()
                    self._ignored_result_count = 0
                    self._check_motion_progress()
                    if self._motion_progress_timeout_expired():
                        self._handle_async_timeout(error_code=-7, reason='progress_timeout')
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

                if self.is_async_command and not self.saw_running:
                    if self.current_command_id is None and command_id and self._running_state_wait_expired():
                        self.current_command_id = command_id
                        self._log_warning(f'RUNNING state was not observed within {self.running_state_wait}s; '
                                          f'falling back to command_id={command_id} for task {self.task_id}')
                    elif self.current_command_id is None:
                        self.logger.debug('Async command: waiting to see RUNNING state first')
                        return

                last_result = self._get_last_command_result_response()
                # Both GetCommandState and GetLastCommandResult succeeded; reset stuck timer.
                self._first_grpc_failure_time = None
                self.logger.debug(f'GetLastCommandResult response: {last_result}')
                result_command_id = last_result.get('commandId')

                if self.is_async_command:
                    if result_command_id:
                        if self.current_command_id is None and self.saw_running:
                            self.current_command_id = result_command_id
                            self._ignored_result_count = 0
                            self.logger.debug(f'Bound result command_id {result_command_id} to task {self.task_id}')
                        elif self.current_command_id and result_command_id != self.current_command_id:
                            self._note_ignored_mismatch('result', result_command_id)
                            return
                    else:
                        self.logger.debug(f'Last command result missing commandId for task {self.task_id}')

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
                    else:
                        self._log_warning(f'Command {self.task_id} failed with error code {error_code}')
                        retry_task_id = self.task_id
                        retry_error_code = error_code

            if retry_task_id is not None and retry_error_code is not None:
                if await self._handle_command_retry(retry_task_id, retry_error_code):
                    return  # Retry initiated, don't publish yet

            with self._command_lock:
                if retry_task_id is not None and self.task_id != retry_task_id:
                    return

                # Publish and reset
                self.last_command_result = result
                if self._publish_to_zenoh(self.command_is_completed_pub, result.as_payload()) and result.is_completed:
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

    async def _should_retry_command(self, error_code: int) -> bool:
        """Determine if a command should be retried based on its error code.

        Args:
            error_code (int): The error code from the failed command

        Returns:
            bool: True if the command should be retried, False otherwise
        """
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

    def _prepare_async_command_args(self,
                                    args: Dict[str, Any],
                                    defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Prepare arguments for async StartCommand methods.

        Sets wait_for_completion=False by default and updates is_async_command flag.

        Args:
            args: Original command arguments
            defaults: Additional default values to set if not present

        Returns:
            Prepared arguments dict
        """
        prepared = args.copy() if args else {}
        if defaults:
            for key, value in defaults.items():
                if key not in prepared:
                    prepared[key] = value
        if 'wait_for_completion' not in prepared:
            prepared['wait_for_completion'] = False
        self.is_async_command = not prepared.get('wait_for_completion', True)
        return prepared

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

    def _execute_command(self, command: Dict[str, Any], expected_task_id: Optional[str] = None) -> None:
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
                    self.is_async_command = False
                    self.saw_running = False
                    self.async_command_started_at = None
                    self.last_progress_at = time.monotonic()
                    self.command_dispatched_at = time.monotonic()
                    self._motion_progress_pose = None
                    self._motion_progress_at = None
                    self.expected_kachaka_method = None
                    self.command_target_map_name = None
                    self.command_target_pose = None
                    self._ignored_result_count = 0
                    if not is_retry:
                        self.retry_count = 0
                    self.dispatching = True

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
                        if (target_x is not None and target_y is not None and self._is_near_target(
                                target_x,
                                target_y,
                                target_yaw,
                                distance_tolerance=self.noop_distance_tolerance,
                                yaw_tolerance=self.noop_yaw_tolerance,
                        )):
                            self._log_info(
                                f'Target within noop tolerance (distance<={self.noop_distance_tolerance}m, '
                                f'yaw<={self.noop_yaw_tolerance}rad); reporting success without calling Kachaka')
                            self._publish_command_completion(success=True, error_code=0, task_id=new_task_id)
                            return
                        self.expected_kachaka_method = 'move_to_pose'
                        self.command_target_map_name = map_name
                        self.command_target_pose = Pose(target_x or 0.0, target_y or 0.0, target_yaw or 0.0)
                        args = self._prepare_async_command_args(args, {'cancel_all': True})
                        self.logger.debug(f'Current pose: {self.last_pose.as_list()}')
                        self.logger.debug(f'Target pose: x={args.get("x")}, y={args.get("y")}, yaw={args.get("yaw")}')
                        response = self._execute_sync_method(method_name, args, task_id=new_task_id)
                        if self.is_async_command:
                            self._update_current_command_id(response, method_name)
                    elif method_name == 'return_home':
                        self.expected_kachaka_method = 'return_home'
                        args = self._prepare_async_command_args(command['args'])
                        response = self._execute_sync_method(method_name, args, task_id=new_task_id)
                        if self.is_async_command:
                            self._update_current_command_id(response, method_name)
                    else:
                        self.logger.debug(f'{method_name} args: {command["args"]}')
                        self._execute_sync_method(method_name, command['args'], task_id=new_task_id)
                finally:
                    with self._command_lock:
                        self.dispatching = False
        except (json.JSONDecodeError, ValueError, AttributeError) as e:
            self._log_error_msg(f'Invalid command: {str(e)}')
            if new_task_id:
                self._publish_command_completion(success=False, error_code=-1, task_id=new_task_id)
        except (ConnectionError, RpcError, Exception) as e:
            self._log_error('Unexpected', f'executing command {method_name}', e)
            if new_task_id:
                self._publish_command_completion(success=False, error_code=-1, task_id=new_task_id)

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
                    self.async_command_started_at = time.monotonic()
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
