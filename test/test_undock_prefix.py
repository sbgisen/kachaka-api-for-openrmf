#!/usr/bin/env python3

# Copyright (c) 2026 SoftBank Corp.
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
"""Tests for the undock prefix before the first move off a dock (Issue #51).

Covers the two-phase (undock -> original move) command, the conditions that
must suppress or refuse the prefix, the phase-owned command_id retention, the
undock-specific timeouts, and the rule that only the final phase may report
success to RMF.

The default configuration (undock.enabled: false) must leave the existing
move_to_pose path byte-for-byte unchanged, including issuing no extra gRPC
calls; test_disabled_by_default_* covers that.
"""
# ruff: noqa: SLF001

import asyncio
import logging
import math
from pathlib import Path
import sys
import threading
import time
from typing import List, Optional, Tuple
from unittest.mock import MagicMock

from kachaka_api.generated import kachaka_api_pb2 as pb2
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from connect_openrmf_by_zenoh import KachakaApiClientByZenoh  # noqa: E402
from connect_openrmf_by_zenoh import MapState  # noqa: E402
from connect_openrmf_by_zenoh import Pose  # noqa: E402
from connect_openrmf_by_zenoh import UndockPhase  # noqa: E402

CHARGING = pb2.PowerSupplyStatus.Value('POWER_SUPPLY_STATUS_CHARGING')
DISCHARGING = pb2.PowerSupplyStatus.Value('POWER_SUPPLY_STATUS_DISCHARGING')


def make_node(client_methods: Optional[list] = None) -> KachakaApiClientByZenoh:
    """Build a KachakaApiClientByZenoh without running __init__.

    Args:
        client_methods: Method names the fake kachaka_client exposes
            (hasattr() returns False for anything else).

    Returns:
        A node instance with mocked gRPC client and Zenoh publishers, with
        the undock feature enabled and the config.yaml defaults applied.
    """
    node = object.__new__(KachakaApiClientByZenoh)
    node.logger = logging.getLogger('test_undock_prefix')
    node.robot_name = 'test_robot'
    node.method_mapping = {}
    node.map_name_mapping = {}
    node.reverse_map_name_mapping = {}
    node.last_pose = Pose.zero()
    node.map_state = MapState.initial().with_telemetry_map_name('27F')
    node.task_id = None
    node.last_command = None
    node.last_command_result = None
    node.last_command_id = None
    node.current_command_id = None
    node.is_async_command = False
    node.saw_running = False
    node.async_command_started_at = None
    node.retry_count = 0
    node.retry_enabled = False
    node.max_navigation_retries = 0
    node.retry_interval = 0.0
    node.retry_on_error_types = ['Error']
    node._command_lock = threading.RLock()
    node._client_lock = threading.RLock()
    node._dispatch_lock = threading.Lock()
    node.dispatching = False
    node._pending_dispatch_snapshot_id = None
    node.last_progress_at = None
    node._ignored_result_count = 0
    node._first_grpc_failure_time = None
    node.command_completion_timeout = 180.0
    node.command_max_retries = 1
    node.running_state_wait = 5.0
    node.grpc_status_check_timeout = 0.1
    node.grpc_telemetry_timeout = 0.1
    node.grpc_stuck_threshold = 30.0
    node.command_is_completed_pub = MagicMock()
    node.map_name_pub = MagicMock()
    node.pose_pub = MagicMock()
    node.state_pub = MagicMock()
    node._publish_to_zenoh = MagicMock(return_value=True)
    node.kachaka_client = MagicMock(spec=client_methods if client_methods is not None else ['move_to_pose', 'stub'])
    node.noop_enabled = False
    node.noop_distance_tolerance = 0.15
    node.noop_yaw_tolerance = 0.10
    node.progress_distance_delta = 0.02
    node.progress_yaw_delta = 0.02
    node.command_match_distance_tolerance = 0.75
    node.command_match_yaw_tolerance = 0.35
    node.command_start_timeout = 15.0
    node.motion_progress_timeout = 30.0
    node.command_dispatched_at = None
    node._motion_progress_pose = None
    node._motion_progress_at = None
    node.expected_kachaka_method = None
    node.command_target_map_name = None
    node.command_target_pose = None
    node.external_control_active = False
    node._external_command_id = None
    node.own_return_home_retention = 5.0
    node._recently_completed_own_command_id = None
    node._recently_completed_own_command_until = None
    node._state_seq = 0
    # Issue #51 undock state, at the config.yaml defaults but enabled.
    node.undock_enabled = True
    node.undock_distance_m = 0.5
    node.undock_dock_radius_m = 0.5
    node.undock_other_dock_vicinity_m = 0.5
    node.undock_lane_tolerance_m = 0.3
    node.undock_start_timeout = 15.0
    node.undock_progress_timeout = 30.0
    node.undock_phase_timeout = 45.0
    node.undock_chargers = []
    node.undock_lanes = []
    node._undock = None
    node.grpc_connection_check = MagicMock(return_value=True)
    return node


def published_payloads(node: KachakaApiClientByZenoh) -> list:
    """Return all payloads published on the completion publisher."""
    return [
        call.args[1] for call in node._publish_to_zenoh.call_args_list if call.args[0] is node.command_is_completed_pub
    ]


def locations_response(poses: List[Tuple[float, float, float]]) -> object:
    """Build a GetLocationsResponse listing the given poses as chargers."""
    response = pb2.GetLocationsResponse()
    for index, (x, y, theta) in enumerate(poses):
        location = response.locations.add()
        location.id = f'charger-{index}'
        location.name = f'charger-{index}'
        location.type = pb2.LocationType.Value('LOCATION_TYPE_CHARGER')
        location.pose.x = x
        location.pose.y = y
        location.pose.theta = theta
    return response


def battery_response(status: int) -> object:
    """Build a GetBatteryInfoResponse with the given power supply status."""
    response = pb2.GetBatteryInfoResponse()
    response.remaining_percentage = 80.0
    response.power_supply_status = status
    return response


def pose_response(x: float, y: float, theta: float) -> object:
    """Build a GetRobotPoseResponse for the given pose."""
    response = pb2.GetRobotPoseResponse()
    response.pose.x = x
    response.pose.y = y
    response.pose.theta = theta
    return response


def start_command_response(command_id: str, success: bool = True) -> object:
    """Build a StartCommandResponse carrying a command_id."""
    response = pb2.StartCommandResponse()
    response.result.success = success
    response.command_id = command_id
    return response


def arm_robot(
    node: KachakaApiClientByZenoh,
    pose: Tuple[float, float, float] = (0.0, 0.0, 0.0),
    chargers: Optional[List[Tuple[float, float, float]]] = None,
    status: int = CHARGING,
    command_ids: Optional[List[str]] = None,
) -> None:
    """Wire the fake gRPC stub for an on-the-dock robot.

    Args:
        node: The node to arm.
        pose: The robot pose reported by GetRobotPose.
        chargers: Charger poses reported by GetLocations.
        status: The power supply status reported by GetBatteryInfo.
        command_ids: command_ids handed out by successive StartCommand calls.
    """
    node.kachaka_client.stub.GetBatteryInfo = MagicMock(return_value=battery_response(status))
    node.kachaka_client.stub.GetLocations = MagicMock(
        return_value=locations_response(chargers if chargers is not None else [(0.0, 0.0, 0.0)]))
    node.kachaka_client.stub.GetRobotPose = MagicMock(return_value=pose_response(*pose))
    node.kachaka_client.stub.StartCommand = MagicMock(
        side_effect=[start_command_response(cid) for cid in (command_ids or ['grpc-phase1', 'grpc-phase2'])])


def move_command(task_id: str = 'task-1', x: float = 5.0, y: float = 0.0) -> dict:
    """Build an RMF move_to_pose command on map 27F."""
    return {'id': task_id, 'method': 'move_to_pose', 'args': {'x': x, 'y': y, 'yaw': 0.0, 'map_name': '27F'}}


def complete_active_command(node: KachakaApiClientByZenoh,
                            command_id: str,
                            success: bool = True,
                            error_code: int = 0) -> None:
    """Feed publish_result() a completion for command_id and run one poll."""
    node.saw_running = True
    node._get_command_state_response = MagicMock(return_value={
        'commandId': command_id,
        'state': 'COMMAND_STATE_UNKNOWN'
    })
    node._get_last_command_result_response = MagicMock(
        return_value={
            'commandId': command_id,
            'result': {
                'success': success,
                'errorCode': error_code
            },
            'command': {
                'moveToPoseCommand': {}
            },
        })
    asyncio.run(node.publish_result())


# ---------------------------------------------------------------------------
# Two-phase success
# ---------------------------------------------------------------------------


def test_two_phase_undock_then_original_move_publishes_one_completion() -> None:
    """A docked robot leaves the dock first, then moves to the original target."""
    node = make_node()
    arm_robot(node)

    node._execute_command(move_command())

    # Phase 1: the departure move is 0.5m straight ahead of the body yaw,
    # with the yaw unchanged (no rotate-in-place on the contacts).
    assert node._undock is not None
    assert node._undock.phase == UndockPhase.UNDOCKING
    assert node._undock.phase1_command_id == 'grpc-phase1'
    assert node.current_command_id == 'grpc-phase1'
    assert math.isclose(node.command_target_pose.x, 0.5)
    assert math.isclose(node.command_target_pose.y, 0.0)
    assert math.isclose(node.command_target_pose.theta, 0.0)
    # Nothing is reported to RMF yet: the task is still running.
    assert published_payloads(node) == []

    # Phase 1 completes -> phase 2 is dispatched, still no completion.
    node.last_pose = Pose(0.5, 0.0, 0.0)
    complete_active_command(node, 'grpc-phase1')

    assert published_payloads(node) == []
    assert node._undock.phase == UndockPhase.NAVIGATING
    assert node._undock.phase2_command_id == 'grpc-phase2'
    assert node.task_id == 'task-1'
    assert node.command_target_pose == Pose(5.0, 0.0, 0.0)

    # Phase 2 completes -> exactly one success completion for the RMF task.
    node.last_pose = Pose(5.0, 0.0, 0.0)
    complete_active_command(node, 'grpc-phase2')

    assert published_payloads(node) == [{'id': 'task-1', 'is_completed': True, 'success': True}]
    assert node.task_id is None
    assert node._undock is None


def test_original_target_on_the_dock_left_is_skipped_after_undock() -> None:
    """The stuck-on-dock case: phase 1 succeeds and the original target is not re-approached."""
    node = make_node()
    arm_robot(node)

    # The RMF target is the dock vertex itself (within the dock radius).
    node._execute_command(move_command(x=0.1, y=0.0))

    assert node._undock.skip_original is True

    node.last_pose = Pose(0.5, 0.0, 0.0)
    complete_active_command(node, 'grpc-phase1')

    assert published_payloads(node) == [{'id': 'task-1', 'is_completed': True, 'success': True}]
    # Only phase 1 was ever dispatched; the robot never drove back onto the dock.
    assert node.kachaka_client.stub.StartCommand.call_count == 1
    assert node.task_id is None


# ---------------------------------------------------------------------------
# Conditions that suppress the prefix
# ---------------------------------------------------------------------------


def test_no_undock_when_battery_is_discharging() -> None:
    """A robot near a dock but not charging is not on the contacts; no prefix."""
    node = make_node()
    arm_robot(node, status=DISCHARGING)

    node._execute_command(move_command())

    assert node._undock is None
    assert node.command_target_pose == Pose(5.0, 0.0, 0.0)
    assert node.current_command_id == 'grpc-phase1'


def test_no_undock_when_battery_status_rpc_fails() -> None:
    """An unreadable battery status falls back to the plain move, not to a failure."""
    node = make_node()
    arm_robot(node)
    node.kachaka_client.stub.GetBatteryInfo = MagicMock(side_effect=RuntimeError('boom'))

    node._execute_command(move_command())

    assert node._undock is None
    assert published_payloads(node) == []
    assert node.command_target_pose == Pose(5.0, 0.0, 0.0)


def test_no_undock_when_robot_is_away_from_every_charger() -> None:
    """Charging status alone does not imply being on a dock."""
    node = make_node()
    arm_robot(node, pose=(9.0, 9.0, 0.0))

    node._execute_command(move_command())

    assert node._undock is None
    assert node.command_target_pose == Pose(5.0, 0.0, 0.0)


def test_map_mismatch_is_rejected_before_the_undock_decision() -> None:
    """The map guard runs first: a wrong-floor command never reaches the dock check."""
    node = make_node()
    arm_robot(node)
    node.map_state = MapState.initial().with_telemetry_map_name('29F')
    node._fetch_robot_map_name = MagicMock(return_value='29F')

    node._execute_command(move_command())

    assert node._undock is None
    assert node.kachaka_client.stub.GetBatteryInfo.call_count == 0
    assert published_payloads(node) == [{
        'id': 'task-1',
        'is_completed': True,
        'success': False,
        'reason': 'map_mismatch',
    }]


# ---------------------------------------------------------------------------
# Endpoint checks
# ---------------------------------------------------------------------------


def test_endpoint_inside_another_dock_vicinity_is_refused() -> None:
    """A departure that would end on the neighbouring dock is refused, not attempted."""
    node = make_node()
    arm_robot(node, chargers=[(0.0, 0.0, 0.0), (0.95, 0.0, math.pi)])

    node._execute_command(move_command())

    assert node._undock is None
    assert node.kachaka_client.stub.StartCommand.call_count == 0
    assert published_payloads(node) == [{
        'id': 'task-1',
        'is_completed': True,
        'success': False,
        'reason': 'undock_endpoint_rejected',
    }]


def test_endpoint_far_from_every_configured_lane_is_refused() -> None:
    """An endpoint off the lane network is refused (the bridge has no nav graph)."""
    node = make_node()
    arm_robot(node)
    node.undock_lanes = [{'map_name': '27F', 'start': [0.0, 3.0], 'end': [5.0, 3.0]}]

    node._execute_command(move_command())

    assert published_payloads(node) == [{
        'id': 'task-1',
        'is_completed': True,
        'success': False,
        'reason': 'undock_endpoint_rejected',
    }]


def test_endpoint_on_a_configured_lane_is_accepted() -> None:
    """An endpoint within lane_tolerance_m of a configured lane still undocks."""
    node = make_node()
    arm_robot(node)
    node.undock_lanes = [{'map_name': '27F', 'start': [0.0, 0.0], 'end': [5.0, 0.0]}]

    node._execute_command(move_command())

    assert node._undock is not None
    assert node._undock.phase == UndockPhase.UNDOCKING


# ---------------------------------------------------------------------------
# Timeouts (15s start / 30s progress / 45s phase total)
# ---------------------------------------------------------------------------


def active_undock_node(phase: str = UndockPhase.UNDOCKING, started_ago: float = 0.0) -> KachakaApiClientByZenoh:
    """Build a node with an in-flight undock phase for task-1."""
    node = make_node()
    node.task_id = 'task-1'
    node.is_async_command = True
    node.current_command_id = 'grpc-phase1'
    node.expected_kachaka_method = 'move_to_pose'
    node.command_target_map_name = '27F'
    node.command_target_pose = Pose(0.5, 0.0, 0.0)
    node.last_progress_at = time.monotonic()
    node.last_command = move_command()
    node._undock = UndockPhase(
        task_id='task-1',
        original_command=move_command(),
        skip_original=False,
        started_at=time.monotonic() - started_ago,
        phase=phase,
        phase1_command_id='grpc-phase1',
    )
    return node


def test_start_timeout_uses_the_undock_budget_while_phase1_is_in_flight() -> None:
    """Phase 1 must start within undock start timeout, not the global command_start one."""
    node = active_undock_node()
    node.command_start_timeout = 600.0
    node.undock_start_timeout = 15.0
    node.saw_running = False
    node.command_dispatched_at = time.monotonic() - 20.0
    node._get_command_state_response = MagicMock(return_value={'commandId': None, 'state': 'COMMAND_STATE_UNKNOWN'})

    asyncio.run(node.publish_result())

    assert published_payloads(node) == [{
        'id': 'task-1',
        'is_completed': True,
        'success': False,
        'reason': 'undock_start_timeout',
    }]


def test_progress_timeout_uses_the_undock_budget_while_phase1_is_in_flight() -> None:
    """A phase 1 stalled in place fails after undock progress timeout."""
    node = active_undock_node()
    node.motion_progress_timeout = 600.0
    node.undock_progress_timeout = 30.0
    node.saw_running = True
    node.command_dispatched_at = time.monotonic()
    node._motion_progress_pose = Pose.zero()
    node._motion_progress_at = time.monotonic() - 40.0
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'grpc-phase1',
        'state': 'COMMAND_STATE_RUNNING'
    })

    asyncio.run(node.publish_result())

    assert {
        'id': 'task-1',
        'is_completed': True,
        'success': False,
        'reason': 'undock_progress_timeout',
    } in published_payloads(node)


def test_phase_total_timeout_fails_the_task() -> None:
    """The whole undock phase is capped, independent of start/progress signals."""
    node = active_undock_node(started_ago=50.0)
    node.saw_running = True
    node.command_dispatched_at = time.monotonic()
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'grpc-phase1',
        'state': 'COMMAND_STATE_RUNNING'
    })

    asyncio.run(node.publish_result())

    assert published_payloads(node) == [{
        'id': 'task-1',
        'is_completed': True,
        'success': False,
        'reason': 'undock_phase_timeout',
    }]
    assert node.task_id is None


def test_timeouts_are_unchanged_once_the_undock_phase_is_over() -> None:
    """Phase 2 runs on the ordinary command budgets, not the undock ones."""
    node = active_undock_node(phase=UndockPhase.NAVIGATING)
    node.command_start_timeout = 15.0
    node.undock_start_timeout = 1.0
    node.motion_progress_timeout = 30.0
    node.undock_progress_timeout = 1.0

    assert node._active_start_timeout() == 15.0
    assert node._active_progress_timeout() == 30.0


# ---------------------------------------------------------------------------
# Failure handling: no retry, no early completion
# ---------------------------------------------------------------------------


def test_undock_failure_is_reported_once_and_never_retried() -> None:
    """An obstacle during phase 1 fails the task instead of undocking again."""
    node = active_undock_node()
    node.saw_running = True
    node.retry_enabled = True
    node.max_navigation_retries = 3
    node._execute_command = MagicMock()

    complete_active_command(node, 'grpc-phase1', success=False, error_code=0)

    assert published_payloads(node) == [{
        'id': 'task-1',
        'is_completed': True,
        'success': False,
        'reason': 'undock_failed',
    }]
    node._execute_command.assert_not_called()
    assert node.task_id is None


def test_should_retry_command_excludes_undock_reasons() -> None:
    """undock_* reasons are never retriable, even for otherwise retriable codes."""
    node = make_node()
    node.retry_enabled = True

    assert asyncio.run(node._should_retry_command(0, 'undock_failed')) is False
    assert asyncio.run(node._should_retry_command(0, 'undock_start_timeout')) is False
    # An ordinary cancelled navigation is still retriable.
    assert asyncio.run(node._should_retry_command(0, None)) is True


def test_success_completion_from_a_non_final_phase_is_not_published() -> None:
    """Only the final phase may tell RMF the task is done."""
    node = active_undock_node()

    assert node._publish_command_completion(success=True, error_code=0, task_id='task-1') is False
    assert published_payloads(node) == []
    assert node._undock.phase == UndockPhase.UNDOCKING


def test_failure_completion_from_a_non_final_phase_finalizes_and_publishes() -> None:
    """A failure ends the task now rather than waiting for the 180s watchdog."""
    node = active_undock_node()

    assert (node._publish_command_completion(success=False, error_code=-9, task_id='task-1', reason='external_busy')
            is True)
    assert published_payloads(node) == [{
        'id': 'task-1',
        'is_completed': True,
        'success': False,
        'reason': 'external_busy',
    }]


# ---------------------------------------------------------------------------
# Phase-owned command_id retention
# ---------------------------------------------------------------------------


def test_phase1_command_id_stays_own_across_the_phase_boundary() -> None:
    """Phase 1's id is ours until the phase context is cleared, with no grace-period bet."""
    node = active_undock_node(phase=UndockPhase.NAVIGATING)
    node._undock.phase2_command_id = 'grpc-phase2'
    node.current_command_id = 'grpc-phase2'
    # Expire the generic retention window: only phase ownership can save us here.
    node._recently_completed_own_command_id = None
    node._recently_completed_own_command_until = None

    assert node._is_own_command_id('grpc-phase1') is True
    assert node._is_own_command_id('grpc-phase2') is True
    assert node._is_own_command_id('someone-else') is False


def test_lingering_phase1_running_state_is_not_treated_as_external_control() -> None:
    """A RUNNING phase 1 command observed after the boundary must not preempt the task."""
    node = active_undock_node(phase=UndockPhase.NAVIGATING)
    node._undock.phase2_command_id = 'grpc-phase2'
    node.current_command_id = 'grpc-phase2'
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'grpc-phase1',
        'state': 'COMMAND_STATE_RUNNING',
        'command': {
            'moveToPoseCommand': {}
        },
    })

    asyncio.run(node.monitor_external_control())

    assert node.external_control_active is False
    assert published_payloads(node) == []


# ---------------------------------------------------------------------------
# Defaults: undock.enabled=false changes nothing
# ---------------------------------------------------------------------------


def test_disabled_by_default_dispatches_move_to_pose_unchanged() -> None:
    """With the feature off, move_to_pose behaves exactly as before."""
    node = make_node()
    node.undock_enabled = False
    arm_robot(node)

    node._execute_command(move_command())

    assert node._undock is None
    assert node.expected_kachaka_method == 'move_to_pose'
    assert node.command_target_pose == Pose(5.0, 0.0, 0.0)
    assert node.current_command_id == 'grpc-phase1'
    assert published_payloads(node) == []
    # No extra telemetry round trips are added to the dispatch path.
    assert node.kachaka_client.stub.GetBatteryInfo.call_count == 0
    assert node.kachaka_client.stub.GetLocations.call_count == 0


def test_disabled_by_default_keeps_the_ordinary_completion_path() -> None:
    """With the feature off, a plain move completes on the first success."""
    node = make_node()
    node.undock_enabled = False
    arm_robot(node)
    node._execute_command(move_command())

    node.last_pose = Pose(5.0, 0.0, 0.0)
    complete_active_command(node, 'grpc-phase1')

    assert published_payloads(node) == [{'id': 'task-1', 'is_completed': True, 'success': True}]
    assert node.task_id is None


def test_shipped_config_defaults() -> None:
    """config/config.yaml ships the feature disabled with the documented values."""
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / 'config' / 'config.yaml').read_text())
    node = make_node()
    node._load_undock_config(config['undock'])

    assert node.undock_enabled is False
    assert node.undock_distance_m == 0.5
    assert node.undock_dock_radius_m == 0.5
    assert node.undock_other_dock_vicinity_m == 0.5
    assert node.undock_lane_tolerance_m == 0.3
    assert node.undock_start_timeout == 15.0
    assert node.undock_progress_timeout == 30.0
    assert node.undock_phase_timeout == 45.0


def test_distance_is_clamped_to_the_supported_range() -> None:
    """A distance outside [0.3, 0.6] is clamped, never used as configured."""
    node = make_node()

    node._load_undock_config({'distance_m': 1.0})
    assert node.undock_distance_m == 0.6

    node._load_undock_config({'distance_m': 0.1})
    assert node.undock_distance_m == 0.3


def test_per_dock_distance_override_is_applied() -> None:
    """A configured charger entry can override distance_m for that dock."""
    node = make_node()
    node.undock_chargers = [{'name': 'dock_a', 'map_name': '27F', 'pose': [0.0, 0.0, 0.0], 'distance_m': 0.35}]
    arm_robot(node)

    node._execute_command(move_command())

    assert math.isclose(node.command_target_pose.x, 0.35)


def test_configured_charger_pose_is_used_when_get_locations_fails() -> None:
    """The configured pose is the fallback, not the first choice."""
    node = make_node()
    arm_robot(node)
    node.kachaka_client.stub.GetLocations = MagicMock(side_effect=RuntimeError('boom'))
    node.undock_chargers = [{'name': 'dock_a', 'map_name': '27F', 'pose': [0.0, 0.0, 0.0]}]

    node._execute_command(move_command())

    assert node._undock is not None
    assert math.isclose(node.command_target_pose.x, 0.5)


def test_reissued_command_carries_the_undock_context_to_the_new_id() -> None:
    """An ID adopted mid-undock must not orphan the phase context (PR #54 review).

    Left on the old ID, phase 1's success is published as the whole task's
    success and the original target is never dispatched.
    """
    node = active_undock_node()
    reissued = move_command(task_id='task-2')

    node._execute_command(reissued)

    assert node.task_id == 'task-2'
    assert node._undock is not None
    assert node._undock.task_id == 'task-2'
    assert node._undock.original_command['id'] == 'task-2'
    assert node._undock.phase == UndockPhase.UNDOCKING
    # The phase guard still owns the adopted ID, so no success leaks out.
    assert node._publish_command_completion(success=True, error_code=0, task_id='task-2') is False
    assert published_payloads(node) == []
