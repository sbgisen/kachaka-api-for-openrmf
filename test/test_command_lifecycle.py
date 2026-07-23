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
- a persistent command_id mismatch never undoes the own binding
- completion publishing is idempotent and never wipes a newer task's state

Also covers the Issue #34 Plan §5 generalization of external-command
detection (ISS34-036): any non-own RUNNING command (not only
returnHomeCommand) preempts an active RMF task or marks busy while idle,
ownership is decided purely by bounded command_id equality, and an
external id can never be rebound onto an own task's completion.
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
    node.kachaka_client = MagicMock(spec=client_methods)
    # Issue #34 Step 2 state: near-distance short-circuit, split timeouts,
    # command matching, external returnHome tracking, unified state seq.
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
# A-3: a persistent command_id mismatch never undoes the own binding
# ---------------------------------------------------------------------------


def test_persistent_mismatch_never_unbinds_command_id() -> None:
    """Repeated mismatches, even past MAX_IGNORED_MISMATCHES, never clear current_command_id.

    Reproduces the ISS34-035 gap analysis finding: unbinding on persistent
    mismatch used to let a same-type/target external command rebind onto
    this task once _ignored_result_count reached MAX_IGNORED_MISMATCHES.
    Own binding is decided once, synchronously, at dispatch (StartCommand's
    response) and must never be undone by later mismatched observations --
    a genuinely external command is instead handled by
    monitor_external_control(), which preempts the task outright.
    """
    node = make_node()
    node.task_id = 'cmd-3'
    node.current_command_id = 'grpc-old'

    for _ in range(KachakaApiClientByZenoh.MAX_IGNORED_MISMATCHES * 2):
        node._note_ignored_mismatch('command state', 'grpc-new')
        assert node.current_command_id == 'grpc-old'


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
    node.noop_enabled = True
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
    node.noop_enabled = True
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
    node.noop_enabled = True
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
    node.noop_enabled = True
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
    node.noop_enabled = True
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
    node.noop_enabled = True
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
    node.noop_enabled = True
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


def test_move_to_pose_dispatches_when_noop_disabled_by_default() -> None:
    """With noop_enabled left at its default (False), an in-tolerance move still dispatches normally."""
    node = make_node(client_methods=['move_to_pose'])
    node._execute_async_stub_dispatch = MagicMock(return_value={'commandId': 'grpc-noop-disabled'})
    node._is_near_target = MagicMock()
    node._fresh_floor_matches = MagicMock()
    node.last_pose = Pose(0.0, 0.0, 0.0)
    node.map_state = MapState.initial().with_telemetry_map_name('8F')

    node._execute_command({
        'id': 'cmd-noop-disabled',
        'method': 'move_to_pose',
        'args': {
            'x': 0.0,
            'y': 0.0,
            'yaw': 0.0,
            'map_name': '8F'
        },
    })

    # noop_enabled=False must short-circuit-evaluate the gate before
    # _is_near_target()/_fresh_floor_matches() are called at all.
    node._is_near_target.assert_not_called()
    node._fresh_floor_matches.assert_not_called()
    node._execute_async_stub_dispatch.assert_called_once()
    assert node._execute_async_stub_dispatch.call_args.kwargs['task_id'] == 'cmd-noop-disabled'
    assert node.expected_kachaka_method == 'move_to_pose'
    assert node.command_target_pose == Pose(0.0, 0.0, 0.0)
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


def test_bound_command_missing_state_command_id_is_not_treated_as_progress() -> None:
    """A RUNNING GetCommandState response missing commandId must not be accepted as our bound command.

    Codex re-review ISS34-040 blocking-A: publish_result() only rejected a
    *non-empty* mismatched commandId on the state poll; an empty/missing one
    slipped through unchecked and was treated as progress on the active
    task. Kachaka API 3.14.4.0's own client wrapper requires
    result.command_id == response.command_id while waiting for completion,
    so an empty id here must fail closed the same as any other mismatch,
    not be silently accepted.
    """
    node = make_node()
    node.task_id = 'cmd-bound'
    node.is_async_command = True
    node.saw_running = False
    node.current_command_id = 'own-id-1'
    node.expected_kachaka_method = 'move_to_pose'
    node.command_dispatched_at = time.monotonic()
    node._get_command_state_response = MagicMock(return_value={
        'commandId': None,
        'state': 'COMMAND_STATE_RUNNING',
        'command': {
            'moveToPoseCommand': {}
        },
    })

    asyncio.run(node.publish_result())

    assert node.saw_running is False
    assert published_payloads(node) == []


def test_bound_command_missing_result_command_id_does_not_report_success() -> None:
    """A GetLastCommandResult response missing commandId must never be accepted as our completion.

    Direct regression for Codex re-review ISS34-040 blocking-A: the
    previous code only rejected a *non-empty* mismatched result commandId
    and otherwise fell through to check type/map/target, so a stale or
    external result with no commandId at all -- but a matching type and
    target -- was reported as this task's own success (Codex's simulation
    reproduced exactly this: published=[{id: rmf-own, is_completed: true,
    success: true}]). Ownership requires an exact commandId match;
    empty/missing must fail closed like any other mismatch.
    """
    node = make_node()
    node.task_id = 'cmd-ok'
    node.is_async_command = True
    node.saw_running = True
    node.current_command_id = 'own-id-2'
    node.expected_kachaka_method = 'move_to_pose'
    node.command_target_map_name = '8F'
    node.command_target_pose = Pose(1.0, 1.0, 0.0)
    node.last_pose = Pose(1.0, 1.0, 0.0)
    node.map_state = MapState.initial().with_telemetry_map_name('8F')
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'own-id-2',
        'state': 'COMMAND_STATE_UNKNOWN'
    })
    node._get_last_command_result_response = MagicMock(return_value={
        'commandId': None,
        'result': {
            'success': True,
            'errorCode': 0
        },
        'command': {
            'moveToPoseCommand': {}
        },
    })

    asyncio.run(node.publish_result())

    assert published_payloads(node) == []


def test_sync_switch_map_own_id_snapshot_is_not_reconciled_as_external() -> None:
    """A RUNNING command observed during a synchronous switch_map dispatch is never reconciled.

    Reproduces Codex re-review ISS34-040 blocking-B: _reconcile_pending_dispatch_snapshot()
    used to run for every dispatch, including synchronous ones (switch_map)
    that never capture an ownership id at all. A monitor_external_control()
    poll during such a dispatch that observed the dispatch's own in-flight
    command_id would, once (mis)reconciled, treat that own id as external
    and set external_control_active=True right after the dispatch's own
    success was published -- incorrectly rejecting subsequent RMF commands
    with external_busy until the next live poll cleared it.

    Drives dispatch (_execute_command) and detection
    (monitor_external_control) on real threads: the mocked
    _execute_switch_map_sync pauses on an Event (mirroring the real gRPC
    work), which is the exact window where dispatching=True and no
    ownership id exists to compare a snapshot against.
    """
    node = make_node(client_methods=['switch_map'])
    node.method_mapping = {}

    reached_dispatch = threading.Event()
    proceed_with_dispatch = threading.Event()

    def racing_switch_map(_args: dict, _task_id: Optional[str]) -> None:
        reached_dispatch.set()
        proceed_with_dispatch.wait(timeout=2.0)
        node._publish_command_completion(success=True, error_code=0, task_id='cmd-switch-map')

    node._execute_switch_map_sync = MagicMock(side_effect=racing_switch_map)
    node._get_command_state_response = MagicMock(
        return_value={
            'commandId': 'own-switch-map-id',
            'state': 'COMMAND_STATE_RUNNING',
            'command': {
                'moveToLocationCommand': {
                    'targetLocationId': 'somewhere'
                }
            },
        })

    dispatch_thread = threading.Thread(
        target=node._execute_command,
        args=({
            'id': 'cmd-switch-map',
            'method': 'switch_map',
            'args': {}
        },),
    )
    dispatch_thread.start()
    assert reached_dispatch.wait(timeout=2.0), 'dispatch never reached switch_map execution'
    assert node.dispatching is True

    # monitor observes the dispatch's own in-flight command as RUNNING while
    # dispatching=True; it must defer, not judge ownership yet.
    asyncio.run(node.monitor_external_control())
    assert node.external_control_active is False
    assert node._pending_dispatch_snapshot_id == 'own-switch-map-id'

    proceed_with_dispatch.set()
    dispatch_thread.join(timeout=2.0)
    assert not dispatch_thread.is_alive()
    assert node.dispatching is False

    # The switch_map's own success was published, and reconciliation must
    # never have run for it: no bogus external detection.
    assert published_payloads(node) == [{'id': 'cmd-switch-map', 'is_completed': True, 'success': True}]
    assert node.external_control_active is False
    assert node._pending_dispatch_snapshot_id is None


def test_wrong_type_running_command_is_not_bound_when_dispatch_lacked_command_id() -> None:
    """publish_result() never binds current_command_id while it is unset, regardless of command type.

    Codex re-review ISS34-037 blocking-1 removed publish_result()'s
    type/target-based fallback bind entirely: ownership is captured solely
    by StartCommand's response command_id at dispatch
    (_execute_async_stub_dispatch), which now fails the task closed
    (reason='missing_command_id') instead of ever leaving current_command_id
    unbound while a task stays active. This state (current_command_id=None
    with is_async_command=True) should therefore never arise in practice; if
    it somehow does, an observed RUNNING command -- of any type -- must
    still never be treated as progress or bound to this task.
    """
    node = make_node()
    node.task_id = 'cmd-nobind'
    node.is_async_command = True
    node.saw_running = False
    node.current_command_id = None  # invariant violation: dispatch should have fail-closed instead
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


def test_correct_type_running_command_does_not_bind_when_dispatch_lacked_command_id() -> None:
    """A matching-type RUNNING command does not bind current_command_id via publish_result() either.

    Rewritten for the fail-closed design (Issue #34 Plan §7.3/§7.4, Codex
    re-review ISS34-037 blocking-1): the previous fallback used to bind on a
    type match alone once running_state_wait expired. That path is removed;
    _execute_async_stub_dispatch is now the only place current_command_id is
    ever set, and it fails the dispatch closed whenever StartCommand's
    response carries no command_id.
    """
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

    assert node.current_command_id is None
    assert node.saw_running is False
    assert published_payloads(node) == []


def test_same_type_running_command_with_mismatched_target_is_not_bound() -> None:
    """A same-type RUNNING move_to_pose toward a different target is not bound to our task.

    Type/target confirmation is no longer part of the ownership decision at
    all (Codex re-review ISS34-037 blocking-1 removed it): current_command_id
    stays unbound regardless of how closely the observed command matches.
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


def test_same_type_running_command_with_matching_target_does_not_bind_without_dispatch_time_id() -> None:
    """Even a perfectly type/target-matching RUNNING command is never bound by publish_result().

    Direct regression test for Codex re-review ISS34-037 blocking-1: the
    previous design (test name ended in "_still_binds") treated a matching
    command type and target as sufficient ownership proof and bound
    current_command_id here. That was exactly the gap a same-type/target
    external command racing an ID-less dispatch could exploit to have its
    own success reported as this task's success. Ownership is now captured
    exclusively at dispatch time (StartCommand's response command_id); this
    match must not bind anything.
    """
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

    assert node.current_command_id is None
    assert node.saw_running is False
    assert published_payloads(node) == []


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


def test_external_move_to_location_home_preempts_active_rmf_task() -> None:
    """A moveToLocationCommand(targetLocationId='home') not issued by this bridge preempts immediately.

    Reproduces the ISS34-034 hardware finding: the real Kachaka app's
    Return Home button issues moveToLocationCommand, not returnHomeCommand,
    so a returnHomeCommand-only check never observes it. Detection must
    treat any non-own RUNNING command as external regardless of type
    (Issue #34 Plan §5), so this is caught on the very first observation.
    """
    node = make_node()
    node.task_id = 'cmd-active-move'
    node.last_command = {'id': 'cmd-active-move', 'method': 'move_to_pose', 'args': {}}
    node._get_command_state_response = MagicMock(
        return_value={
            'commandId': 'ext-move-to-location-home',
            'state': 'COMMAND_STATE_RUNNING',
            'command': {
                'moveToLocationCommand': {
                    'targetLocationId': 'home'
                }
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


def test_external_move_to_location_non_home_target_preempts_active_rmf_task() -> None:
    """A moveToLocationCommand toward any targetLocationId (not just 'home') preempts immediately.

    Detection must never depend on the target value: any non-own RUNNING
    moveToLocationCommand is external regardless of destination.
    """
    node = make_node()
    node.task_id = 'cmd-active-move-2'
    node.last_command = {'id': 'cmd-active-move-2', 'method': 'move_to_pose', 'args': {}}
    node._get_command_state_response = MagicMock(
        return_value={
            'commandId': 'ext-move-to-location-other',
            'state': 'COMMAND_STATE_RUNNING',
            'command': {
                'moveToLocationCommand': {
                    'targetLocationId': 'kitchen'
                }
            },
        })

    asyncio.run(node.monitor_external_control())

    assert published_payloads(node) == [{
        'id': 'cmd-active-move-2',
        'is_completed': True,
        'success': False,
        'reason': 'external_preempted',
    }]
    assert node.external_control_active is True
    assert node.task_id is None


def test_external_unrecognized_command_type_defaults_to_busy() -> None:
    """A RUNNING command of a type not in KNOWN_MOVEMENT_COMMAND_FIELDS still defaults to busy.

    Detection never gates on the known-type set -- it exists only to
    control a diagnostic log message. Any non-own RUNNING command occupies
    the movement/command-processor slot (Issue #34 Plan §5).
    """
    node = make_node()
    node.task_id = 'cmd-active-move-3'
    node.last_command = {'id': 'cmd-active-move-3', 'method': 'move_to_pose', 'args': {}}
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'ext-unknown-type',
        'state': 'COMMAND_STATE_RUNNING',
        'command': {
            'someFutureCommand': {}
        },
    })

    asyncio.run(node.monitor_external_control())

    assert published_payloads(node) == [{
        'id': 'cmd-active-move-3',
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


def test_own_move_to_pose_is_not_treated_as_external() -> None:
    """RMF's own dispatched move_to_pose is never classified as external.

    Ownership is decided purely by command_id equality, the same as dock
    (Issue #34 Plan §5); this also demonstrates that the upstream requester
    (e.g. an RMF Web API task like kachaka-rmf-control's charge_station
    request) is irrelevant to the ownership judgement -- only whether the
    bridge itself dispatched and bound the command_id matters.
    """
    node = make_node(client_methods=['move_to_pose', 'stub'])
    node.grpc_connection_check = MagicMock(return_value=True)
    node.kachaka_client.stub.StartCommand = MagicMock(return_value=stub_start_command_response(
        command_id='own-move-to-pose-1'))

    node._execute_command({'id': 'cmd-move', 'method': 'move_to_pose', 'args': {'x': 1.0, 'y': 2.0, 'yaw': 0.0}})
    assert node.current_command_id == 'own-move-to-pose-1'

    node._get_command_state_response = MagicMock(
        return_value={
            'commandId': 'own-move-to-pose-1',
            'state': 'COMMAND_STATE_RUNNING',
            'command': {
                'moveToPoseCommand': {
                    'x': 1.0,
                    'y': 2.0,
                    'yaw': 0.0
                }
            },
        })

    asyncio.run(node.monitor_external_control())

    assert published_payloads(node) == []
    assert node.external_control_active is False
    assert node.task_id == 'cmd-move'


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


def test_missing_command_id_on_dispatch_fails_closed_instead_of_leaving_ownership_ambiguous() -> None:
    """A dock dispatch whose StartCommand response carries no command_id fails closed immediately.

    Rewritten for Codex re-review ISS34-037 blocking-1: the previous design
    (Issue #34 Plan §7.4 blocking-3) left current_command_id unbound and
    relied on downstream detection (monitor_external_control/publish_result)
    to never mistake a later RUNNING command for this task's own via a
    type/target match. That fallback has been removed entirely --
    _execute_async_stub_dispatch now fails the task closed
    (reason='missing_command_id') the moment StartCommand's success response
    carries no command_id, so ownership is never left ambiguous to begin
    with.
    """
    node = make_node(client_methods=['return_home', 'stub'])
    node.method_mapping = {'dock': 'return_home'}
    node.grpc_connection_check = MagicMock(return_value=True)
    node.kachaka_client.stub.StartCommand = MagicMock(return_value=stub_start_command_response(command_id=None))

    node._execute_command({'id': 'cmd-dock-3', 'method': 'dock', 'args': {}})

    assert node.current_command_id is None
    assert node.task_id is None
    assert published_payloads(node) == [{
        'id': 'cmd-dock-3',
        'is_completed': True,
        'success': False,
        'reason': 'missing_command_id',
    }]

    # A RUNNING command observed afterward is correctly treated as external
    # while idle (no RMF task left to preempt) -- cmd-dock-3 was already
    # failed closed and cannot be attributed to it.
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
        'reason': 'missing_command_id',
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


def test_dispatch_pending_snapshot_reconciled_as_external_even_after_it_ends() -> None:
    """A RUNNING command observed only during the dispatch window is still caught by reconciliation.

    Issue #34 Plan §5 "remember snapshot.commandId for reconciliation": while
    dispatching=True our own command_id is not yet bound
    (_execute_async_stub_dispatch's gRPC call runs outside _command_lock), so
    a concurrent monitor_external_control() poll must not judge ownership
    yet -- it could misclassify our own not-yet-bound dispatch as external.
    Deferring must not mean permanently missing a genuinely external
    command, though: the external command here has already ended (per the
    second GetCommandState response, taken after the dispatch settles), so
    only the remembered snapshot -- not a fresh live poll -- can still catch
    it (Codex re-review ISS34-037 non-blocking finding: the previous design
    discarded the dispatch-time snapshot outright).

    Drives command dispatch (_execute_command) and detection
    (monitor_external_control) on real threads through their real code
    paths: the mocked stub.StartCommand pauses on an Event outside
    _command_lock (mirroring the real gRPC call), which is the exact window
    where dispatching=True but current_command_id is still unbound.
    """
    node = make_node(client_methods=['return_home', 'stub'])
    node.method_mapping = {'dock': 'return_home'}
    node.grpc_connection_check = MagicMock(return_value=True)

    reached_grpc_call = threading.Event()
    proceed_with_grpc = threading.Event()

    def racing_start_command(_request: object) -> object:
        reached_grpc_call.set()
        proceed_with_grpc.wait(timeout=2.0)
        return stub_start_command_response(command_id='own-dock-race-2')

    node.kachaka_client.stub.StartCommand = MagicMock(side_effect=racing_start_command)
    # The external command is RUNNING only on the first (dispatch-time) poll;
    # every later live poll finds the robot idle, so detection can only come
    # from the remembered snapshot, never a fresh GetCommandState call.
    node._get_command_state_response = MagicMock(side_effect=[
        {
            'commandId': 'ext-during-dispatch',
            'state': 'COMMAND_STATE_RUNNING',
            'command': {
                'moveToLocationCommand': {
                    'targetLocationId': 'home'
                }
            },
        },
        {
            'commandId': None,
            'state': 'COMMAND_STATE_IDLE'
        },
    ])

    dispatch_thread = threading.Thread(
        target=node._execute_command,
        args=({
            'id': 'cmd-dock-race-2',
            'method': 'dock',
            'args': {}
        },),
    )
    dispatch_thread.start()
    assert reached_grpc_call.wait(timeout=2.0), 'dispatch never reached the gRPC call'
    assert node.dispatching is True

    asyncio.run(node.monitor_external_control())
    assert published_payloads(node) == []
    assert node.external_control_active is False
    assert node._pending_dispatch_snapshot_id == 'ext-during-dispatch'

    proceed_with_grpc.set()
    dispatch_thread.join(timeout=2.0)
    assert not dispatch_thread.is_alive()
    assert node.dispatching is False

    # Reconciliation ran synchronously inside _execute_command's finally
    # block (not a fresh poll) and caught the now-ended external command,
    # preempting the dispatch that raced it.
    assert published_payloads(node) == [{
        'id': 'cmd-dock-race-2',
        'is_completed': True,
        'success': False,
        'reason': 'external_preempted',
    }]
    assert node.external_control_active is True
    assert node._external_command_id == 'ext-during-dispatch'
    assert node.task_id is None
    assert node.current_command_id is None
    assert node._pending_dispatch_snapshot_id is None

    # A live poll now correctly reports idle, confirming the detection above
    # came from the remembered snapshot, not this (or any) fresh poll.
    asyncio.run(node.monitor_external_control())
    assert node.external_control_active is False


def test_missing_command_id_race_never_lets_publish_result_bind_external_success() -> None:
    """Blocking-1 regression (Codex ISS34-037): a same-target external command can never poison a failed dispatch.

    Reproduces the exact interleaving Codex's read-only simulation found,
    with real threads: (1) monitor_external_control() runs first and finds
    nothing external (idle robot) -- modeling "monitor completed" before the
    race; (2) a separate thread dispatches move_to_pose via _execute_command
    (command dispatch runs on its own thread in production, not the main
    loop) -- the mocked stub.StartCommand reports success but returns an
    empty command_id, the anomaly this fix must fail closed on; (3) once the
    dispatch thread has published its failure completion, a same-target
    external moveToPoseCommand appears RUNNING; (4) publish_result() must
    never bind current_command_id to it or report its eventual success as
    this (already-failed) task's own.

    Before this fix, step (2) would have left current_command_id unbound
    (only a warning logged) and the task still active; publish_result()'s
    type/target fallback bind (removed in Step 2/3 of this change) would
    then have bound the external command's id in step (4), and a later
    success for it would have been reported as cmd-race-missing-id's own
    success. Fail-closed dispatch (Step 3) means the task is already gone by
    step (4), so there is nothing left to poison in the first place.
    """
    node = make_node(client_methods=['move_to_pose', 'stub'])
    node.method_mapping = {}
    node.grpc_connection_check = MagicMock(return_value=True)
    node.map_state = MapState.initial().with_telemetry_map_name('L1')
    node.kachaka_client.stub.StartCommand = MagicMock(return_value=stub_start_command_response(command_id=None))

    # (1) monitor_external_control() runs first, on an idle robot.
    node._get_command_state_response = MagicMock(return_value={'commandId': None, 'state': 'COMMAND_STATE_IDLE'})
    asyncio.run(node.monitor_external_control())
    assert node.external_control_active is False

    # (2) a separate thread dispatches move_to_pose; StartCommand succeeds
    # but returns no command_id.
    dispatch_thread = threading.Thread(
        target=node._execute_command,
        args=({
            'id': 'cmd-race-missing-id',
            'method': 'move_to_pose',
            'args': {
                'x': 1.0,
                'y': 1.0,
                'yaw': 0.0,
                'map_name': 'L1'
            },
        },),
    )
    dispatch_thread.start()
    dispatch_thread.join(timeout=2.0)
    assert not dispatch_thread.is_alive()

    failure_payload = {
        'id': 'cmd-race-missing-id',
        'is_completed': True,
        'success': False,
        'reason': 'missing_command_id',
    }
    # Fail-closed: the task was completed as a failure, never left dangling
    # for a later poll to bind.
    assert published_payloads(node) == [failure_payload]
    assert node.task_id is None
    assert node.current_command_id is None

    # (3) a same-target external moveToPoseCommand now appears RUNNING.
    node._get_command_state_response = MagicMock(
        return_value={
            'commandId': 'external-move-race-1',
            'state': 'COMMAND_STATE_RUNNING',
            'command': {
                'moveToPoseCommand': {
                    'x': 1.0,
                    'y': 1.0,
                    'yaw': 0.0
                }
            },
        })

    # (4) publish_result() has no active task to poll for -- it only
    # re-publishes the already-failed completion for Pub/Sub reliability --
    # so it must never bind current_command_id or report a new success.
    asyncio.run(node.publish_result())
    assert node.current_command_id is None
    assert node.task_id is None
    assert all(payload == failure_payload for payload in published_payloads(node))

    # monitor_external_control() correctly reports the same command as
    # external (idle, not attributed to cmd-race-missing-id) once polled.
    asyncio.run(node.monitor_external_control())
    assert node.external_control_active is True
    assert node._external_command_id == 'external-move-race-1'


def test_recently_completed_dock_return_home_is_not_treated_as_external() -> None:
    """A dock that just completed is not mistaken for external control (concern (b)).

    Kachaka can still report a briefly-lingering RUNNING return_home right
    after our own dock completes; the short retention grace period recognizes
    it as ours so a following RMF move is not rejected with external_busy.
    """
    node = make_node(client_methods=['move_to_pose'])
    node.task_id = None  # dock already completed; task_id was reset
    node.last_command = {'id': 'cmd-dock-done', 'method': 'dock', 'args': {}}
    node._recently_completed_own_command_id = 'own-return-home-2'
    node._recently_completed_own_command_until = time.monotonic() + node.own_return_home_retention
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


def test_recently_completed_move_to_pose_is_not_treated_as_external() -> None:
    """A move_to_pose that just completed/was superseded is not mistaken for external control.

    Generalizes concern (b) beyond dock/return_home (Issue #34 Plan §5): a
    cancel/supersession or ordinary completion of a move_to_pose can also
    leave Kachaka briefly reporting the old command_id as RUNNING, and that
    must not be misclassified as an external command either.
    """
    node = make_node(client_methods=['move_to_pose'])
    node.task_id = None  # move_to_pose already completed/superseded; task_id was reset
    node.last_command = {'id': 'cmd-move-done', 'method': 'move_to_pose', 'args': {'x': 1.0, 'y': 1.0}}
    node._recently_completed_own_command_id = 'own-move-2'
    node._recently_completed_own_command_until = time.monotonic() + node.own_return_home_retention
    node._get_command_state_response = MagicMock(
        return_value={
            'commandId': 'own-move-2',
            'state': 'COMMAND_STATE_RUNNING',
            'command': {
                'moveToPoseCommand': {
                    'x': 1.0,
                    'y': 1.0,
                    'yaw': 0.0
                }
            },
        })

    asyncio.run(node.monitor_external_control())

    assert node.external_control_active is False
    assert published_payloads(node) == []

    # A following RMF move must not be rejected with external_busy.
    node._execute_async_stub_dispatch = MagicMock(return_value={'commandId': 'grpc-next-move-2'})
    node._execute_command({'id': 'cmd-next-move-2', 'method': 'move_to_pose', 'args': {'x': 5.0, 'y': 5.0}})
    assert published_payloads(node) == []
    node._execute_async_stub_dispatch.assert_called_once()


def test_own_return_home_retention_recorded_on_dock_completion() -> None:
    """A successful dock completion records its command_id for the retention grace period."""
    node = make_node()
    node.task_id = 'cmd-dock-3'
    node.last_command = {'id': 'cmd-dock-3', 'method': 'dock', 'args': {}}
    node.current_command_id = 'own-return-home-3'

    assert node._publish_command_completion(success=True, error_code=0) is True

    assert node._recently_completed_own_command_id == 'own-return-home-3'
    assert node._recently_completed_own_command_until is not None
    assert node._recently_completed_own_command_until > time.monotonic()
    assert node.task_id is None


def test_own_command_retention_recorded_on_move_to_pose_completion() -> None:
    """A successful move_to_pose completion also records its command_id for the retention grace period.

    Generalizes retention beyond the dock-only gate that used to guard
    _record_own_return_home_retention (Issue #34 Plan §5): any own async
    command with a bound command_id gets the same grace period.
    """
    node = make_node()
    node.task_id = 'cmd-move-3'
    node.last_command = {'id': 'cmd-move-3', 'method': 'move_to_pose', 'args': {'x': 1.0, 'y': 1.0}}
    node.current_command_id = 'own-move-3'

    assert node._publish_command_completion(success=True, error_code=0) is True

    assert node._recently_completed_own_command_id == 'own-move-3'
    assert node._recently_completed_own_command_until is not None
    assert node._recently_completed_own_command_until > time.monotonic()
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
    assert node._recently_completed_own_command_id == 'own-return-home-normal'
    assert node._recently_completed_own_command_until is not None
    assert node._recently_completed_own_command_until > time.monotonic()

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


def test_external_command_while_idle_sets_busy_and_clears_when_gone() -> None:
    """An external command observed while RMF is idle sets busy, and clears once it disappears.

    No active RMF task exists (task_id is None) when the external command
    starts, so there is nothing to preempt -- only external_control_active
    needs to flip to True. Once the command is no longer RUNNING,
    external_control_active clears and a fresh state publish follows on the
    next main-loop iteration's publish_state() call (Issue #34 Plan §5).
    """
    node = make_node()
    node.task_id = None
    node._get_command_state_response = MagicMock(
        return_value={
            'commandId': 'ext-idle-1',
            'state': 'COMMAND_STATE_RUNNING',
            'command': {
                'moveToLocationCommand': {
                    'targetLocationId': 'home'
                }
            },
        })

    asyncio.run(node.monitor_external_control())

    assert published_payloads(node) == []  # nothing to preempt
    assert node.external_control_active is True
    assert node._external_command_id == 'ext-idle-1'

    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'ext-idle-1',
        'state': 'COMMAND_STATE_SUCCEEDED'
    })

    asyncio.run(node.monitor_external_control())

    assert node.external_control_active is False
    assert node._external_command_id is None


def test_external_control_active_persists_across_external_command_id_switch() -> None:
    """Busy must not be cleared just because the external command_id changes to another non-own id.

    Reproduces Issue #34 Plan §5's continuity requirement: e.g. the Kachaka
    app's Return Home completes and the user immediately issues another
    external move -- external_control_active must stay True the whole time,
    only clearing once no non-own RUNNING command remains at all.
    """
    node = make_node()
    node.task_id = None
    node.external_control_active = True
    node._external_command_id = 'ext-first'

    node._get_command_state_response = MagicMock(
        return_value={
            'commandId': 'ext-second',
            'state': 'COMMAND_STATE_RUNNING',
            'command': {
                'moveToPoseCommand': {
                    'x': 1.0,
                    'y': 1.0,
                    'yaw': 0.0
                }
            },
        })

    asyncio.run(node.monitor_external_control())

    assert node.external_control_active is True
    assert node._external_command_id == 'ext-second'
    assert published_payloads(node) == []

    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'ext-second',
        'state': 'COMMAND_STATE_SUCCEEDED'
    })

    asyncio.run(node.monitor_external_control())

    assert node.external_control_active is False
    assert node._external_command_id is None


def test_external_move_to_pose_with_matching_target_preempts_and_is_never_rebound() -> None:
    """An external moveToPoseCommand toward the same target as ours preempts, never rebinds its id.

    Reproduces the ISS34-035 gap analysis finding: an external move whose
    target happens to be within RMF's own command_match tolerance used to
    be able to rebind onto the active task after repeated mismatches
    (_note_ignored_mismatch's old unbind-then-rebind path). Same-target
    coincidence must not matter -- a non-own id is always external.
    """
    node = make_node()
    node.task_id = 'cmd-own-move'
    node.last_command = {'id': 'cmd-own-move', 'method': 'move_to_pose', 'args': {'x': 1.0, 'y': 1.0}}
    node.current_command_id = 'own-move-active'
    node.expected_kachaka_method = 'move_to_pose'
    node.command_target_pose = Pose(1.0, 1.0, 0.0)
    node._get_command_state_response = MagicMock(
        return_value={
            'commandId': 'ext-move-same-target',
            'state': 'COMMAND_STATE_RUNNING',
            'command': {
                'moveToPoseCommand': {
                    'x': 1.0,
                    'y': 1.0,
                    'yaw': 0.0
                }
            },
        })

    asyncio.run(node.monitor_external_control())

    assert published_payloads(node) == [{
        'id': 'cmd-own-move',
        'is_completed': True,
        'success': False,
        'reason': 'external_preempted',
    }]
    assert node.external_control_active is True
    assert node.task_id is None
    assert node.current_command_id != 'ext-move-same-target'


def test_external_move_to_pose_never_rebinds_even_after_repeated_mismatches() -> None:
    """Repeated command-state mismatches against an own binding never let an external id take over.

    End-to-end version of the ISS34-035 gap: even if publish_result() polls
    the mismatching external id MAX_IGNORED_MISMATCHES times first (as could
    happen before monitor_external_control() catches it), current_command_id
    must remain the original own id throughout, and once
    monitor_external_control() runs it must preempt rather than let the
    external id quietly take over.
    """
    node = make_node()
    node.task_id = 'cmd-own-move-2'
    node.last_command = {'id': 'cmd-own-move-2', 'method': 'move_to_pose', 'args': {'x': 1.0, 'y': 1.0}}
    node.current_command_id = 'own-move-active-2'
    node.is_async_command = True
    node.saw_running = True
    node.expected_kachaka_method = 'move_to_pose'
    node.command_target_pose = Pose(1.0, 1.0, 0.0)
    node._get_command_state_response = MagicMock(
        return_value={
            'commandId': 'ext-move-persistent',
            'state': 'COMMAND_STATE_RUNNING',
            'command': {
                'moveToPoseCommand': {
                    'x': 1.0,
                    'y': 1.0,
                    'yaw': 0.0
                }
            },
        })

    for _ in range(KachakaApiClientByZenoh.MAX_IGNORED_MISMATCHES * 2):
        asyncio.run(node.publish_result())
        assert node.current_command_id == 'own-move-active-2'

    assert published_payloads(node) == []

    asyncio.run(node.monitor_external_control())

    assert published_payloads(node) == [{
        'id': 'cmd-own-move-2',
        'is_completed': True,
        'success': False,
        'reason': 'external_preempted',
    }]
    assert node.external_control_active is True
    assert node.task_id is None
    assert node.current_command_id != 'ext-move-persistent'


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
