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
"""Regression tests for the command lifecycle of the Zenoh bridge.

Covers the stuck-robot bugs fixed in the race-condition audit:
- every failure path of _execute_command publishes a failure completion
- a re-issued in-flight command adopts the new ID without re-execution
- the completion watchdog force-completes a silent task (error_code=-5)
- a wrong command_id binding is undone after persistent mismatches
- completion publishing is idempotent and never wipes a newer task's state
"""
# ruff: noqa: SLF001

import asyncio
import logging
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock

import grpc
from kachaka_api.generated import kachaka_api_pb2 as pb2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from connect_openrmf_by_zenoh import CommandCompletion  # noqa: E402
from connect_openrmf_by_zenoh import KachakaApiClientByZenoh  # noqa: E402
from connect_openrmf_by_zenoh import MapState  # noqa: E402
from connect_openrmf_by_zenoh import Pose  # noqa: E402


class FakeRpcError(grpc.RpcError):
    """Minimal RpcError stand-in with the attributes the bridge reads."""

    def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.DEADLINE_EXCEEDED

    def details(self) -> str:
        return 'Deadline Exceeded'


def make_node(client_methods: list = []) -> KachakaApiClientByZenoh:
    """Build a KachakaApiClientByZenoh without running __init__.

    Args:
        client_methods: Method names the fake kachaka_client exposes
            (hasattr() returns False for anything else).

    Returns:
        A node instance with mocked gRPC client and Zenoh publishers.
    """
    node = object.__new__(KachakaApiClientByZenoh)
    node.logger = logging.getLogger('test_command_lifecycle')
    node.robot_name = 'test_robot'
    node.method_mapping = {}
    node.map_name_mapping = {}
    node.reverse_map_name_mapping = {}
    node.last_pose = Pose.zero()
    node.map_state = MapState.initial()
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
    node.retry_on_error_types = []
    node._command_lock = threading.RLock()
    node._client_lock = threading.RLock()
    node._dispatch_lock = threading.Lock()
    node.dispatching = False
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
    node.kachaka_client = MagicMock(spec=client_methods)
    # Issue #34 Step 2 state: near-distance short-circuit, split timeouts,
    # command matching, external returnHome tracking, unified state seq.
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
    node._recently_own_return_home_id = None
    node._recently_own_return_home_until = None
    node._state_seq = 0
    return node


def published_payloads(node: KachakaApiClientByZenoh) -> list:
    """Return all payloads published on the completion publisher."""
    return [
        call.args[1] for call in node._publish_to_zenoh.call_args_list if call.args[0] is node.command_is_completed_pub
    ]


def published_state_payloads(node: KachakaApiClientByZenoh) -> list:
    """Return all payloads published on the unified robots/*/state publisher."""
    return [call.args[1] for call in node._publish_to_zenoh.call_args_list if call.args[0] is node.state_pub]


def stub_start_command_response(command_id: Optional[str] = None, success: bool = True, error_code: int = 0) -> object:
    """Build a pb2.StartCommandResponse for mocking kachaka_client.stub.StartCommand.

    A real protobuf message (not a dict/SimpleNamespace) is required so
    _to_dict()'s module-name check routes it through MessageToDict, exactly
    as it would for the genuine gRPC stub response.
    """
    response = pb2.StartCommandResponse()
    response.result.success = success
    response.result.error_code = error_code
    if command_id is not None:
        response.command_id = command_id
    return response


# ---------------------------------------------------------------------------
# A-2: every failure path publishes a failure completion
# ---------------------------------------------------------------------------


def test_malformed_command_publishes_failure_completion() -> None:
    """A command missing method/args still gets a failure completion for its ID."""
    node = make_node()
    node._execute_command({'id': 'cmd-1'})
    assert published_payloads(node) == [{'id': 'cmd-1', 'is_completed': True, 'success': False}]
    assert node.last_command_result == CommandCompletion('cmd-1', True, False, -1)


def test_unknown_method_publishes_failure_completion() -> None:
    """A command whose method does not exist on the client fails loudly."""
    node = make_node()
    node._execute_command({'id': 'cmd-2', 'method': 'bogus_method', 'args': {}})
    assert published_payloads(node) == [{'id': 'cmd-2', 'is_completed': True, 'success': False}]
    # The failed task must not stay active and block the next command
    assert node.task_id is None


# ---------------------------------------------------------------------------
# Re-issue handling: in-flight adoption and supersede
# ---------------------------------------------------------------------------


def test_reissued_inflight_command_adopts_new_id() -> None:
    """A re-issue with identical method/args adopts the new ID, no re-execution."""
    node = make_node()
    node.task_id = 'cmd-old'
    node.last_command = {'id': 'cmd-old', 'method': 'move_to_pose', 'args': {'x': 1.0, 'y': 2.0, 'yaw': 0.0}}
    node.last_progress_at = time.monotonic() - 100.0

    node._execute_command({'id': 'cmd-new', 'method': 'move_to_pose', 'args': {'x': 1.0, 'y': 2.0, 'yaw': 0.0}})

    assert node.task_id == 'cmd-new'
    assert node.last_command['id'] == 'cmd-new'
    # No completion was published and nothing was re-executed
    assert published_payloads(node) == []
    # Adoption counts as progress so the watchdog does not fire right away
    assert time.monotonic() - node.last_progress_at < 1.0


def test_different_command_supersedes_inflight_task() -> None:
    """A re-issue with different args completes the old task as superseded (-3)."""
    node = make_node(client_methods=['speak'])
    node._execute_sync_method = MagicMock(return_value=None)
    node.task_id = 'cmd-old'
    node.last_command = {'id': 'cmd-old', 'method': 'speak', 'args': {'text': 'a'}}

    node._execute_command({'id': 'cmd-new', 'method': 'speak', 'args': {'text': 'b'}})

    assert published_payloads(node) == [{'id': 'cmd-old', 'is_completed': True, 'success': False}]
    # After the supersede completion, the new task starts with a clean slate
    assert node.last_command_result is None
    assert node.task_id == 'cmd-new'
    node._execute_sync_method.assert_called_once_with('speak', {'text': 'b'}, task_id='cmd-new')


# ---------------------------------------------------------------------------
# A-7: completion watchdog
# ---------------------------------------------------------------------------


def test_watchdog_forces_completion_for_silent_task() -> None:
    """A task with no observed progress is force-completed with error_code=-5."""
    node = make_node()
    node.task_id = 'cmd-stuck'
    node.command_completion_timeout = 0.05
    node.last_progress_at = time.monotonic() - 1.0

    asyncio.run(node.publish_result())

    assert published_payloads(node) == [{'id': 'cmd-stuck', 'is_completed': True, 'success': False}]
    assert node.last_command_result == CommandCompletion('cmd-stuck', True, False, -5)
    assert node.task_id is None


def test_watchdog_does_not_fire_while_progressing() -> None:
    """The watchdog stays quiet while progress is recent and dispatch is running."""
    node = make_node()
    node.task_id = 'cmd-live'
    node.command_completion_timeout = 180.0
    node.last_progress_at = time.monotonic()
    node.dispatching = True

    asyncio.run(node.publish_result())

    # dispatching=True short-circuits polling; nothing is published
    assert published_payloads(node) == []
    assert node.task_id == 'cmd-live'


# ---------------------------------------------------------------------------
# A-3: wrong command_id binding is undone after persistent mismatches
# ---------------------------------------------------------------------------


def test_persistent_mismatch_unbinds_command_id() -> None:
    """MAX_IGNORED_MISMATCHES consecutive mismatches undo the binding."""
    node = make_node()
    node.task_id = 'cmd-3'
    node.current_command_id = 'grpc-old'

    for _ in range(KachakaApiClientByZenoh.MAX_IGNORED_MISMATCHES - 1):
        node._note_ignored_mismatch('command state', 'grpc-new')
        assert node.current_command_id == 'grpc-old'

    node._note_ignored_mismatch('command state', 'grpc-new')
    assert node.current_command_id is None
    assert node._ignored_result_count == 0


# ---------------------------------------------------------------------------
# Completion idempotency and task isolation
# ---------------------------------------------------------------------------


def test_duplicate_completion_is_skipped() -> None:
    """A second completion for the same task ID is not published again."""
    node = make_node()
    node.last_command_result = CommandCompletion('cmd-4', True, False, -1)

    assert node._publish_command_completion(success=False, error_code=-1, task_id='cmd-4') is True
    assert published_payloads(node) == []


def test_stale_completion_does_not_reset_active_task() -> None:
    """A late completion for an old task never wipes the new task's state."""
    node = make_node()
    node.task_id = 'cmd-active'
    node.last_progress_at = time.monotonic()

    assert node._publish_command_completion(success=False, error_code=-1, task_id='cmd-finished') is True

    assert published_payloads(node) == [{'id': 'cmd-finished', 'is_completed': True, 'success': False}]
    assert node.task_id == 'cmd-active'
    assert node.last_progress_at is not None


# ---------------------------------------------------------------------------
# Issue #34 Plan §7.1: near-distance short-circuit
# ---------------------------------------------------------------------------


def test_move_to_pose_short_circuits_within_noop_tolerance() -> None:
    """A 14.9cm move within yaw tolerance succeeds once without calling Kachaka."""
    node = make_node(client_methods=['move_to_pose'])
    node._execute_sync_method = MagicMock()
    node.last_pose = Pose(0.0, 0.0, 0.0)
    node.map_state = MapState.initial().with_telemetry_map_name('8F')
    node._fetch_robot_map_name = MagicMock(return_value='8F')

    node._execute_command({
        'id': 'cmd-noop',
        'method': 'move_to_pose',
        'args': {
            'x': 0.149,
            'y': 0.0,
            'yaw': 0.0,
            'map_name': '8F'
        },
    })

    node._execute_sync_method.assert_not_called()
    # The shortcut must always re-query the floor, even though the cache already
    # agreed with the requested map_name (Issue #34 Plan §7.1 concern (a)).
    node._fetch_robot_map_name.assert_called_once()
    assert published_payloads(node) == [{'id': 'cmd-noop', 'is_completed': True, 'success': True}]
    assert node.task_id is None


def test_move_to_pose_short_circuits_across_pi_seam() -> None:
    """Yaw tolerance is checked after normalizing across the +/-pi wraparound."""
    node = make_node(client_methods=['move_to_pose'])
    node._execute_sync_method = MagicMock()
    node.last_pose = Pose(0.0, 0.0, 3.10)
    node.map_state = MapState.initial().with_telemetry_map_name('8F')
    node._fetch_robot_map_name = MagicMock(return_value='8F')

    node._execute_command({
        'id': 'cmd-noop-wrap',
        'method': 'move_to_pose',
        'args': {
            'x': 0.0,
            'y': 0.0,
            'yaw': -3.10,
            'map_name': '8F'
        },
    })

    node._execute_sync_method.assert_not_called()
    assert published_payloads(node) == [{'id': 'cmd-noop-wrap', 'is_completed': True, 'success': True}]


def test_move_to_pose_does_not_short_circuit_without_map_name() -> None:
    """A missing map_name never short-circuits, even at zero distance (Plan §7.1)."""
    node = make_node(client_methods=['move_to_pose'])
    node._execute_async_stub_dispatch = MagicMock(return_value={'commandId': 'grpc-nomap'})
    node.last_pose = Pose(0.0, 0.0, 0.0)
    node._fetch_robot_map_name = MagicMock(return_value='8F')

    node._execute_command({
        'id': 'cmd-nomap',
        'method': 'move_to_pose',
        'args': {
            'x': 0.0,
            'y': 0.0,
            'yaw': 0.0,
            'map_name': None
        },
    })

    node._fetch_robot_map_name.assert_not_called()
    node._execute_async_stub_dispatch.assert_called_once()
    assert published_payloads(node) == []


def test_move_to_pose_short_circuit_requires_fresh_floor_match() -> None:
    """A stale cache match is not enough: a fresh floor mismatch dispatches instead of short-circuiting."""
    node = make_node(client_methods=['move_to_pose'])
    node._execute_async_stub_dispatch = MagicMock(return_value={'commandId': 'grpc-stale'})
    node.last_pose = Pose(0.0, 0.0, 0.0)
    # Cache says 8F (matches the request) but the robot has actually switched to 9F
    # since the last telemetry read -- exactly the switch_map race in concern (a).
    node.map_state = MapState.initial().with_telemetry_map_name('8F')
    node._fetch_robot_map_name = MagicMock(return_value='9F')

    node._execute_command({
        'id': 'cmd-stale-floor',
        'method': 'move_to_pose',
        'args': {
            'x': 0.0,
            'y': 0.0,
            'yaw': 0.0,
            'map_name': '8F'
        },
    })

    node._fetch_robot_map_name.assert_called_once()
    node._execute_async_stub_dispatch.assert_called_once()
    assert published_payloads(node) == []


def test_move_to_pose_short_circuits_at_exact_boundary() -> None:
    """Exactly 0.15m / 0.10rad (the inclusive boundary) still short-circuits."""
    node = make_node(client_methods=['move_to_pose'])
    node._execute_async_stub_dispatch = MagicMock()
    node.last_pose = Pose(0.0, 0.0, 0.0)
    node.map_state = MapState.initial().with_telemetry_map_name('8F')
    node._fetch_robot_map_name = MagicMock(return_value='8F')

    node._execute_command({
        'id': 'cmd-boundary',
        'method': 'move_to_pose',
        'args': {
            'x': 0.15,
            'y': 0.0,
            'yaw': 0.10,
            'map_name': '8F'
        },
    })

    node._execute_async_stub_dispatch.assert_not_called()
    assert published_payloads(node) == [{'id': 'cmd-boundary', 'is_completed': True, 'success': True}]


def test_move_to_pose_dispatches_when_only_yaw_exceeds_tolerance() -> None:
    """Distance within tolerance but yaw beyond it dispatches a normal command."""
    node = make_node(client_methods=['move_to_pose'])
    node._execute_async_stub_dispatch = MagicMock(return_value={'commandId': 'grpc-yaw'})
    node.last_pose = Pose(0.0, 0.0, 0.0)
    node.map_state = MapState.initial().with_telemetry_map_name('8F')
    node._fetch_robot_map_name = MagicMock(return_value='8F')

    node._execute_command({
        'id': 'cmd-yaw-out',
        'method': 'move_to_pose',
        'args': {
            'x': 0.0,
            'y': 0.0,
            'yaw': 0.11,
            'map_name': '8F'
        },
    })

    # yaw alone exceeds noop_yaw_tolerance (0.10rad); the shortcut's cheap
    # near-target check must reject it before any fresh floor re-query.
    node._fetch_robot_map_name.assert_not_called()
    node._execute_async_stub_dispatch.assert_called_once()
    assert published_payloads(node) == []


def test_move_to_pose_dispatches_when_outside_noop_tolerance() -> None:
    """A move beyond the noop tolerance is dispatched to Kachaka as normal."""
    node = make_node(client_methods=['move_to_pose'])
    node._execute_async_stub_dispatch = MagicMock(return_value={'commandId': 'grpc-move'})
    node.last_pose = Pose(0.0, 0.0, 0.0)
    node.map_state = MapState.initial().with_telemetry_map_name('8F')

    node._execute_command({
        'id': 'cmd-move',
        'method': 'move_to_pose',
        'args': {
            'x': 1.0,
            'y': 0.0,
            'yaw': 0.0,
            'map_name': '8F'
        },
    })

    node._execute_async_stub_dispatch.assert_called_once()
    assert node._execute_async_stub_dispatch.call_args.kwargs['task_id'] == 'cmd-move'
    assert node.expected_kachaka_method == 'move_to_pose'
    assert node.command_target_map_name == '8F'
    assert node.command_target_pose == Pose(1.0, 0.0, 0.0)
    # The noop short-circuit must not have published a completion itself;
    # _execute_async_stub_dispatch (mocked here) owns publishing for the dispatch path.
    assert published_payloads(node) == []


# ---------------------------------------------------------------------------
# Issue #34 Plan §7.2: split command_start / motion_progress timeouts
# ---------------------------------------------------------------------------


def test_start_timeout_fires_before_running_observed() -> None:
    """No RUNNING within command_start_timeout cancels and fails once with start_timeout."""
    node = make_node(client_methods=['cancel_command'])
    node.task_id = 'cmd-start'
    node.is_async_command = True
    node.saw_running = False
    node.command_dispatched_at = time.monotonic() - 100.0
    node.command_start_timeout = 0.05
    node._get_command_state_response = MagicMock(return_value={'commandId': None, 'state': 'COMMAND_STATE_PENDING'})

    asyncio.run(node.publish_result())

    node.kachaka_client.cancel_command.assert_called_once()
    assert published_payloads(node) == [{
        'id': 'cmd-start',
        'is_completed': True,
        'success': False,
        'reason': 'start_timeout',
    }]
    assert node.task_id is None


def test_progress_timeout_fires_when_position_stalled_while_running() -> None:
    """No position/yaw change within motion_progress_timeout fails once with progress_timeout."""
    node = make_node(client_methods=['cancel_command'])
    node.task_id = 'cmd-progress'
    node.is_async_command = True
    node.saw_running = True
    node.current_command_id = 'grpc-p'
    node.last_pose = Pose(1.0, 2.0, 0.0)
    node._motion_progress_pose = Pose(1.0, 2.0, 0.0)
    node._motion_progress_at = time.monotonic() - 100.0
    node.motion_progress_timeout = 0.05
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'grpc-p',
        'state': 'COMMAND_STATE_RUNNING'
    })

    asyncio.run(node.publish_result())

    node.kachaka_client.cancel_command.assert_called_once()
    assert published_payloads(node) == [{
        'id': 'cmd-progress',
        'is_completed': True,
        'success': False,
        'reason': 'progress_timeout',
    }]


def test_progress_timeout_does_not_fire_when_position_advances() -> None:
    """A position change beyond the delta resets the motion_progress baseline."""
    node = make_node()
    node.task_id = 'cmd-moving'
    node.is_async_command = True
    node.saw_running = True
    node.current_command_id = 'grpc-m'
    node.last_pose = Pose(1.10, 2.0, 0.0)
    node._motion_progress_pose = Pose(1.0, 2.0, 0.0)
    node._motion_progress_at = time.monotonic() - 100.0
    node.motion_progress_timeout = 1.0
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'grpc-m',
        'state': 'COMMAND_STATE_RUNNING'
    })

    asyncio.run(node.publish_result())

    # No timeout fired: only the in-progress (is_completed=False) update was published.
    assert published_payloads(node) == [{'id': 'cmd-moving', 'is_completed': False}]
    assert node.task_id == 'cmd-moving'
    assert time.monotonic() - node._motion_progress_at < 1.0


# ---------------------------------------------------------------------------
# Issue #34 Plan §7.3: RMF/Kachaka command matching
# ---------------------------------------------------------------------------


def test_command_type_mismatch_is_not_reported_as_success() -> None:
    """A same-ID success whose command type differs from what was dispatched is rejected."""
    node = make_node()
    node.task_id = 'cmd-typed'
    node.is_async_command = True
    node.saw_running = True
    node.current_command_id = 'grpc-typed'
    node.expected_kachaka_method = 'move_to_pose'
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'grpc-typed',
        'state': 'COMMAND_STATE_UNKNOWN'
    })
    node._get_last_command_result_response = MagicMock(return_value={
        'commandId': 'grpc-typed',
        'result': {
            'success': True,
            'errorCode': 0
        },
        'command': {
            'returnHomeCommand': {}
        },
    })

    asyncio.run(node.publish_result())

    assert published_payloads(node) == [{'id': 'cmd-typed', 'is_completed': True, 'success': False}]


def test_command_floor_mismatch_at_completion_uses_map_mismatch_reason() -> None:
    """A same-ID, same-type success on the wrong floor is rejected as map_mismatch."""
    node = make_node()
    node.task_id = 'cmd-floor'
    node.is_async_command = True
    node.saw_running = True
    node.current_command_id = 'grpc-floor'
    node.expected_kachaka_method = 'move_to_pose'
    node.command_target_map_name = '8F'
    node.map_state = MapState.initial().with_telemetry_map_name('9F')
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'grpc-floor',
        'state': 'COMMAND_STATE_UNKNOWN'
    })
    node._get_last_command_result_response = MagicMock(return_value={
        'commandId': 'grpc-floor',
        'result': {
            'success': True,
            'errorCode': 0
        },
        'command': {
            'moveToPoseCommand': {}
        },
    })

    asyncio.run(node.publish_result())

    assert published_payloads(node) == [{
        'id': 'cmd-floor',
        'is_completed': True,
        'success': False,
        'reason': 'map_mismatch',
    }]


def test_matching_command_type_and_floor_reports_success() -> None:
    """A same-ID, same-type success on the correct floor is reported as success."""
    node = make_node()
    node.task_id = 'cmd-ok'
    node.is_async_command = True
    node.saw_running = True
    node.current_command_id = 'grpc-ok'
    node.expected_kachaka_method = 'move_to_pose'
    node.command_target_map_name = '8F'
    node.command_target_pose = Pose(1.0, 1.0, 0.0)
    node.last_pose = Pose(1.0, 1.0, 0.0)
    node.map_state = MapState.initial().with_telemetry_map_name('8F')
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'grpc-ok',
        'state': 'COMMAND_STATE_UNKNOWN'
    })
    node._get_last_command_result_response = MagicMock(return_value={
        'commandId': 'grpc-ok',
        'result': {
            'success': True,
            'errorCode': 0
        },
        'command': {
            'moveToPoseCommand': {}
        },
    })

    asyncio.run(node.publish_result())

    assert published_payloads(node) == [{'id': 'cmd-ok', 'is_completed': True, 'success': True}]


def test_final_pose_mismatch_is_not_reported_as_success() -> None:
    """A same-ID, same-type, same-floor success far from the target pose is rejected."""
    node = make_node()
    node.task_id = 'cmd-pose'
    node.is_async_command = True
    node.saw_running = True
    node.current_command_id = 'grpc-pose'
    node.expected_kachaka_method = 'move_to_pose'
    node.command_target_map_name = '8F'
    node.command_target_pose = Pose(10.0, 10.0, 0.0)
    node.last_pose = Pose(0.0, 0.0, 0.0)
    node.map_state = MapState.initial().with_telemetry_map_name('8F')
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'grpc-pose',
        'state': 'COMMAND_STATE_UNKNOWN'
    })
    node._get_last_command_result_response = MagicMock(return_value={
        'commandId': 'grpc-pose',
        'result': {
            'success': True,
            'errorCode': 0
        },
        'command': {
            'moveToPoseCommand': {}
        },
    })

    asyncio.run(node.publish_result())

    assert published_payloads(node) == [{'id': 'cmd-pose', 'is_completed': True, 'success': False}]


def test_wrong_type_running_command_is_not_bound_when_dispatch_lacked_command_id() -> None:
    """A StartCommand response without commandId must not let a wrong-type RUNNING command bind later.

    Reproduces Codex review ISS34-006's blocking finding: high-level
    StartCommand often returns a bare Result with no commandId, so
    current_command_id stays unbound after dispatch. Binding must still
    require the observed command's type to match what was dispatched.
    """
    node = make_node()
    node.task_id = 'cmd-nobind'
    node.is_async_command = True
    node.saw_running = False
    node.current_command_id = None  # StartCommand response carried no commandId
    node.expected_kachaka_method = 'move_to_pose'
    node.command_dispatched_at = time.monotonic()
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'unrelated-running-1',
        'state': 'COMMAND_STATE_RUNNING',
        'command': {
            'returnHomeCommand': {}
        },
    })

    asyncio.run(node.publish_result())

    assert node.current_command_id is None
    assert node.saw_running is False
    assert published_payloads(node) == []


def test_correct_type_running_command_binds_when_dispatch_lacked_command_id() -> None:
    """A matching-type RUNNING command still binds normally after a commandId-less dispatch."""
    node = make_node()
    node.task_id = 'cmd-bind-ok'
    node.is_async_command = True
    node.saw_running = False
    node.current_command_id = None
    node.expected_kachaka_method = 'move_to_pose'
    node.command_dispatched_at = time.monotonic()
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'grpc-bind-ok',
        'state': 'COMMAND_STATE_RUNNING',
        'command': {
            'moveToPoseCommand': {}
        },
    })

    asyncio.run(node.publish_result())

    assert node.current_command_id == 'grpc-bind-ok'
    assert node.saw_running is True
    assert published_payloads(node) == [{'id': 'cmd-bind-ok', 'is_completed': False}]


def test_same_type_running_command_with_mismatched_target_is_not_bound() -> None:
    """A same-type RUNNING move_to_pose toward a different target is not bound to our task.

    Reproduces Codex re-review ISS34-010 blocking-2: the previous bind
    condition checked only that the observed command's type matched
    (moveToPoseCommand), so a same-type command started by someone else (or
    a stale one) toward a different destination would still bind and its
    later success would be reported to RMF as our task's success. Binding
    must also confirm the observed command's own target matches what we
    dispatched (Issue #34 Plan §7.3).
    """
    node = make_node()
    node.task_id = 'cmd-ext-move'
    node.is_async_command = True
    node.saw_running = False
    node.current_command_id = None
    node.expected_kachaka_method = 'move_to_pose'
    node.command_dispatched_at = time.monotonic()
    node.command_target_pose = Pose(1.0, 1.0, 0.0)
    node._get_command_state_response = MagicMock(
        return_value={
            'commandId': 'external-move-1',
            'state': 'COMMAND_STATE_RUNNING',
            'command': {
                'moveToPoseCommand': {
                    'x': 5.0,
                    'y': 5.0,
                    'yaw': 0.0
                },
            },
        })

    asyncio.run(node.publish_result())

    assert node.current_command_id is None
    assert node.saw_running is False
    assert published_payloads(node) == []


def test_same_type_running_command_with_matching_target_still_binds() -> None:
    """A same-type RUNNING move_to_pose whose own target matches ours still binds normally."""
    node = make_node()
    node.task_id = 'cmd-own-move'
    node.is_async_command = True
    node.saw_running = False
    node.current_command_id = None
    node.expected_kachaka_method = 'move_to_pose'
    node.command_dispatched_at = time.monotonic()
    node.command_target_pose = Pose(1.0, 1.0, 0.0)
    node._get_command_state_response = MagicMock(
        return_value={
            'commandId': 'own-move-1',
            'state': 'COMMAND_STATE_RUNNING',
            'command': {
                'moveToPoseCommand': {
                    'x': 1.05,
                    'y': 0.98,
                    'yaw': 0.01
                },
            },
        })

    asyncio.run(node.publish_result())

    assert node.current_command_id == 'own-move-1'
    assert node.saw_running is True
    assert published_payloads(node) == [{'id': 'cmd-own-move', 'is_completed': False}]


# ---------------------------------------------------------------------------
# Issue #34 Plan §7.2: command_dispatched_at origin (Codex review non-blocking finding)
# ---------------------------------------------------------------------------


def test_command_dispatched_at_set_only_after_successful_dispatch() -> None:
    """command_dispatched_at is set at dispatch success, not before grpc_connection_check.

    move_to_pose/return_home now dispatch via stub.StartCommand() directly
    (_execute_async_stub_dispatch) instead of the high-level wrapper, so the
    mock target moved from kachaka_client.move_to_pose to
    kachaka_client.stub.StartCommand (Issue #34 Plan §7.3/§7.4, Codex
    re-review ISS34-010 blocking-3).
    """
    node = make_node(client_methods=['stub'])
    node.is_async_command = False
    node.command_dispatched_at = None
    node.grpc_connection_check = MagicMock(return_value=True)
    node.kachaka_client.stub.StartCommand = MagicMock(return_value=stub_start_command_response(
        command_id='grpc-dispatch'))

    node._execute_async_stub_dispatch('move_to_pose', {'x': 1.0, 'y': 0.0, 'yaw': 0.0}, task_id='cmd-dispatch')

    assert node.command_dispatched_at is not None
    assert time.monotonic() - node.command_dispatched_at < 1.0


def test_command_dispatched_at_stays_none_when_connection_check_fails() -> None:
    """A failed grpc_connection_check never sets command_dispatched_at."""
    node = make_node(client_methods=['stub'])
    node.is_async_command = False
    node.command_dispatched_at = None
    node.grpc_connection_check = MagicMock(return_value=False)

    try:
        node._execute_async_stub_dispatch('move_to_pose', {
            'x': 1.0,
            'y': 0.0,
            'yaw': 0.0
        },
                                          task_id='cmd-dispatch-fail')
    except ConnectionError:
        pass

    assert node.command_dispatched_at is None


# ---------------------------------------------------------------------------
# Issue #34 Plan §7.4: external returnHome
# ---------------------------------------------------------------------------


def test_external_return_home_preempts_active_rmf_task() -> None:
    """A returnHome not issued by this bridge preempts the active RMF task once."""
    node = make_node()
    node.task_id = 'cmd-active-move'
    node.last_command = {'id': 'cmd-active-move', 'method': 'move_to_pose', 'args': {}}
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'ext-return-home-1',
        'state': 'COMMAND_STATE_RUNNING',
        'command': {
            'returnHomeCommand': {}
        },
    })

    asyncio.run(node.monitor_external_control())

    assert published_payloads(node) == [{
        'id': 'cmd-active-move',
        'is_completed': True,
        'success': False,
        'reason': 'external_preempted',
    }]
    assert node.external_control_active is True
    assert node.task_id is None


def test_own_dock_return_home_is_not_treated_as_external() -> None:
    """RMF's own dispatched dock (return_home) is never classified as external.

    Stub-direct dispatch (_execute_async_stub_dispatch) captures command_id
    synchronously at dispatch time, so ownership is decided purely by ID
    equality from dispatch onward -- even within running_state_wait of the
    dispatch (Issue #34 Plan §7.3/§7.4, Codex re-review ISS34-010
    blocking-3). Goes through the real dispatch (_execute_command) and the
    real detection path (monitor_external_control), not manually-set state.
    """
    node = make_node(client_methods=['return_home', 'stub'])
    node.method_mapping = {'dock': 'return_home'}
    node.grpc_connection_check = MagicMock(return_value=True)
    node.kachaka_client.stub.StartCommand = MagicMock(return_value=stub_start_command_response(
        command_id='own-return-home-1'))

    node._execute_command({'id': 'cmd-dock', 'method': 'dock', 'args': {}})
    assert node.current_command_id == 'own-return-home-1'

    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'own-return-home-1',
        'state': 'COMMAND_STATE_RUNNING',
        'command': {
            'returnHomeCommand': {}
        },
    })

    asyncio.run(node.monitor_external_control())

    assert published_payloads(node) == []
    assert node.external_control_active is False
    assert node.task_id == 'cmd-dock'


def test_external_return_home_id_conflict_with_rmf_dock_is_not_reported_as_success() -> None:
    """An external returnHome racing our own dock dispatch is never confused for our dock's success.

    Reproduces Codex re-review ISS34-010 blocking-3: our own dock is
    dispatched and binds a command_id synchronously via stub-direct dispatch,
    but a different, externally-triggered returnHome is observed RUNNING
    under a different id -- well inside running_state_wait, the very window
    the previous fallback treated an unbound id as plausibly ours. The
    external command must still be recognized as external (preempting our
    task with external_preempted), not attributed to our dock as a success.
    """
    node = make_node(client_methods=['return_home', 'stub'])
    node.method_mapping = {'dock': 'return_home'}
    node.grpc_connection_check = MagicMock(return_value=True)
    node.kachaka_client.stub.StartCommand = MagicMock(return_value=stub_start_command_response(
        command_id='own-dock-id'))

    node._execute_command({'id': 'cmd-dock', 'method': 'dock', 'args': {}})
    assert node.current_command_id == 'own-dock-id'

    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'ext-return-home-2',
        'state': 'COMMAND_STATE_RUNNING',
        'command': {
            'returnHomeCommand': {}
        },
    })

    asyncio.run(node.monitor_external_control())

    assert published_payloads(node) == [{
        'id': 'cmd-dock',
        'is_completed': True,
        'success': False,
        'reason': 'external_preempted',
    }]
    assert node.external_control_active is True
    assert node.task_id is None

    # Even if the external returnHome later reports success, there is no
    # active task left to misattribute it to: publish_result has nothing to
    # poll for once task_id has been cleared by the preemption above.
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'ext-return-home-2',
        'state': 'COMMAND_STATE_SUCCEEDED'
    })
    asyncio.run(node.publish_result())
    assert node.last_command_result == CommandCompletion('cmd-dock', True, False, -8, 'external_preempted')


def test_external_return_home_when_own_dispatch_never_bound_id_is_detected_as_external() -> None:
    """A returnHome is treated as external when our own dock dispatch never bound a command_id.

    Defensive coverage for Issue #34 Plan §7.4 blocking-3: even if
    StartCommandResponse carried no command_id (should not normally happen
    once a stub-direct dispatch reports success), an unbound own
    command_id must never be treated as plausibly ours -- unlike the
    previous running_state_wait-window fallback, which assumed an unbound
    id was ours for the whole window.
    """
    node = make_node(client_methods=['return_home', 'stub'])
    node.method_mapping = {'dock': 'return_home'}
    node.grpc_connection_check = MagicMock(return_value=True)
    node.kachaka_client.stub.StartCommand = MagicMock(return_value=stub_start_command_response(command_id=None))

    node._execute_command({'id': 'cmd-dock-3', 'method': 'dock', 'args': {}})
    assert node.current_command_id is None

    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'ext-return-home-3',
        'state': 'COMMAND_STATE_RUNNING',
        'command': {
            'returnHomeCommand': {}
        },
    })

    asyncio.run(node.monitor_external_control())

    assert published_payloads(node) == [{
        'id': 'cmd-dock-3',
        'is_completed': True,
        'success': False,
        'reason': 'external_preempted',
    }]
    assert node.external_control_active is True


def test_start_command_success_and_id_bind_are_atomic_under_concurrent_monitor() -> None:
    """command_dispatched_at/current_command_id must never be observably split across threads.

    Reproduces Codex re-review ISS34-016 blocking-1: _execute_async_stub_dispatch
    previously set command_dispatched_at/async_command_started_at under
    _command_lock, but current_command_id was bound afterwards by the caller
    (_execute_command), outside any lock. A real monitor_external_control()
    call running on a different thread in that gap saw command_dispatched_at
    already set but current_command_id still unbound, misclassified our own
    dispatched dock (return_home) as an external returnHome, and preempted
    the in-flight task with external_preempted. The fix binds
    command_dispatched_at/async_command_started_at/current_command_id inside
    a single _command_lock acquisition in _execute_async_stub_dispatch, so
    monitor_external_control() can never observe the split state -- it
    either runs before dispatch starts or blocks on _command_lock until the
    whole bundle is bound.

    Drives command dispatch (_execute_command) and detection
    (monitor_external_control) on real threads through their real code
    paths; only _update_current_command_id is wrapped, purely to pin the
    interleaving at the exact point being fixed.
    """
    node = make_node(client_methods=['return_home', 'stub'])
    node.method_mapping = {'dock': 'return_home'}
    node.grpc_connection_check = MagicMock(return_value=True)
    node.kachaka_client.stub.StartCommand = MagicMock(return_value=stub_start_command_response(
        command_id='own-dock-race-1'))
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'own-dock-race-1',
        'state': 'COMMAND_STATE_RUNNING',
        'command': {
            'returnHomeCommand': {}
        },
    })

    reached_bind_point = threading.Event()
    proceed_to_bind = threading.Event()
    original_update = node._update_current_command_id

    def racing_update(response: Optional[dict], method_name: str) -> None:
        reached_bind_point.set()
        proceed_to_bind.wait(timeout=2.0)
        original_update(response, method_name)

    node._update_current_command_id = racing_update

    dispatch_thread = threading.Thread(
        target=node._execute_command,
        args=({
            'id': 'cmd-dock-race',
            'method': 'dock',
            'args': {}
        },),
    )
    dispatch_thread.start()
    assert reached_bind_point.wait(timeout=2.0), 'dispatch never reached the id-bind point'

    monitor_thread = threading.Thread(target=lambda: asyncio.run(node.monitor_external_control()))
    monitor_thread.start()
    # Grace period for the monitor to attempt _command_lock: enough for it to
    # either finish (pre-fix: lock is free, the bug reproduces almost
    # instantly) or still be blocked on it (post-fix: dispatch holds the
    # lock through the whole bind).
    time.sleep(0.1)
    proceed_to_bind.set()

    monitor_thread.join(timeout=2.0)
    dispatch_thread.join(timeout=2.0)
    assert not monitor_thread.is_alive()
    assert not dispatch_thread.is_alive()

    assert published_payloads(node) == []
    assert node.external_control_active is False
    assert node.task_id == 'cmd-dock-race'
    assert node.current_command_id == 'own-dock-race-1'


def test_recently_completed_dock_return_home_is_not_treated_as_external() -> None:
    """A dock that just completed is not mistaken for external control (concern (b)).

    Kachaka can still report a briefly-lingering RUNNING return_home right
    after our own dock completes; the short retention grace period recognizes
    it as ours so a following RMF move is not rejected with external_busy.
    """
    node = make_node(client_methods=['move_to_pose'])
    node.task_id = None  # dock already completed; task_id was reset
    node.last_command = {'id': 'cmd-dock-done', 'method': 'dock', 'args': {}}
    node._recently_own_return_home_id = 'own-return-home-2'
    node._recently_own_return_home_until = time.monotonic() + node.own_return_home_retention
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'own-return-home-2',
        'state': 'COMMAND_STATE_RUNNING',
        'command': {
            'returnHomeCommand': {}
        },
    })

    asyncio.run(node.monitor_external_control())

    assert node.external_control_active is False
    assert published_payloads(node) == []

    # A following RMF move must not be rejected with external_busy.
    node._execute_async_stub_dispatch = MagicMock(return_value={'commandId': 'grpc-next-move'})
    node._execute_command({'id': 'cmd-next-move', 'method': 'move_to_pose', 'args': {'x': 5.0, 'y': 5.0}})
    assert published_payloads(node) == []
    node._execute_async_stub_dispatch.assert_called_once()


def test_own_return_home_retention_recorded_on_dock_completion() -> None:
    """A successful dock completion records its command_id for the retention grace period."""
    node = make_node()
    node.task_id = 'cmd-dock-3'
    node.last_command = {'id': 'cmd-dock-3', 'method': 'dock', 'args': {}}
    node.current_command_id = 'own-return-home-3'

    assert node._publish_command_completion(success=True, error_code=0) is True

    assert node._recently_own_return_home_id == 'own-return-home-3'
    assert node._recently_own_return_home_until is not None
    assert node._recently_own_return_home_until > time.monotonic()
    assert node.task_id is None


def test_own_return_home_retention_recorded_via_publish_result_completion_path() -> None:
    """Own ID retention must also be recorded on the ordinary publish_result() success path.

    Reproduces Codex re-review ISS34-010 recommendation-2: the retention
    record was previously written only inside _publish_command_completion(),
    but a normal async dock success completes through publish_result()'s own
    publish-and-reset branch, which called _reset_async_command_state()
    directly and skipped the record entirely. Without it, a briefly
    lingering RUNNING return_home right after an ordinary dock success would
    be misclassified as external control (Issue #34 Plan §7.4 concern (b)).
    """
    node = make_node()
    node.task_id = 'cmd-dock-normal'
    node.last_command = {'id': 'cmd-dock-normal', 'method': 'dock', 'args': {}}
    node.is_async_command = True
    node.saw_running = True
    node.current_command_id = 'own-return-home-normal'
    node.expected_kachaka_method = 'return_home'
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'own-return-home-normal',
        'state': 'COMMAND_STATE_SUCCEEDED'
    })
    node._get_last_command_result_response = MagicMock(
        return_value={
            'commandId': 'own-return-home-normal',
            'result': {
                'success': True,
                'errorCode': 0
            },
            'command': {
                'returnHomeCommand': {}
            },
        })

    asyncio.run(node.publish_result())

    assert published_payloads(node) == [{'id': 'cmd-dock-normal', 'is_completed': True, 'success': True}]
    assert node.task_id is None
    assert node._recently_own_return_home_id == 'own-return-home-normal'
    assert node._recently_own_return_home_until is not None
    assert node._recently_own_return_home_until > time.monotonic()

    # A lingering RUNNING return_home right after must still be recognized as ours.
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'own-return-home-normal',
        'state': 'COMMAND_STATE_RUNNING',
        'command': {
            'returnHomeCommand': {}
        },
    })
    asyncio.run(node.monitor_external_control())
    assert node.external_control_active is False


def test_external_control_active_rejects_new_rmf_command_with_external_busy() -> None:
    """An RMF command arriving while external control is active is rejected, not sent to Kachaka."""
    node = make_node(client_methods=['move_to_pose'])
    node._execute_async_stub_dispatch = MagicMock()
    node.external_control_active = True

    node._execute_command({'id': 'cmd-during-external', 'method': 'move_to_pose', 'args': {'x': 5.0, 'y': 5.0}})

    node._execute_async_stub_dispatch.assert_not_called()
    assert published_payloads(node) == [{
        'id': 'cmd-during-external',
        'is_completed': True,
        'success': False,
        'reason': 'external_busy',
    }]
    assert node.task_id is None


def test_external_control_ends_when_return_home_no_longer_running() -> None:
    """external_control_active clears once the external returnHome is no longer RUNNING."""
    node = make_node()
    node.external_control_active = True
    node._external_command_id = 'ext-return-home-1'
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'ext-return-home-1',
        'state': 'COMMAND_STATE_SUCCEEDED'
    })

    asyncio.run(node.monitor_external_control())

    assert node.external_control_active is False
    assert node._external_command_id is None


# ---------------------------------------------------------------------------
# Issue #34 Plan §5.1: unified robots/{robot_name}/state payload
# ---------------------------------------------------------------------------


def test_publish_state_discards_sample_when_map_changes_during_read() -> None:
    """A floor change between the two floor reads (a racing switch_map) discards the sample."""
    node = make_node()
    node._fetch_robot_map_name = MagicMock(side_effect=['8F', '9F'])
    node.kachaka_client = MagicMock()
    node.kachaka_client.stub.GetRobotPose = MagicMock(return_value=SimpleNamespace(
        pose=SimpleNamespace(x=1.0, y=2.0, theta=0.5)))

    asyncio.run(node.publish_state())

    assert published_state_payloads(node) == []
    assert node._state_seq == 0


def test_publish_state_publishes_when_floor_reads_agree() -> None:
    """A consistent floor-before/floor-after read publishes a single seq'd state sample."""
    node = make_node()
    node._fetch_robot_map_name = MagicMock(side_effect=['8F', '8F'])
    node.kachaka_client = MagicMock()
    node.kachaka_client.stub.GetRobotPose = MagicMock(return_value=SimpleNamespace(
        pose=SimpleNamespace(x=1.0, y=2.0, theta=0.5)))

    asyncio.run(node.publish_state())

    payloads = published_state_payloads(node)
    assert len(payloads) == 1
    assert payloads[0]['schema_version'] == 1
    assert payloads[0]['seq'] == 1
    assert payloads[0]['map_name'] == '8F'
    assert payloads[0]['pose'] == [1.0, 2.0, 0.5]
    assert payloads[0]['external_control_active'] is False


def test_publish_state_discards_sample_on_grpc_error() -> None:
    """A gRPC failure discards the sample instead of resending a stale pose under a new timestamp."""
    node = make_node()
    node.last_pose = Pose(9.0, 9.0, 9.0)
    node._fetch_robot_map_name = MagicMock(side_effect=FakeRpcError())

    asyncio.run(node.publish_state())

    assert published_state_payloads(node) == []
    assert node._state_seq == 0
