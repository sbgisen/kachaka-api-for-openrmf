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
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from connect_openrmf_by_zenoh import CommandCompletion  # noqa: E402
from connect_openrmf_by_zenoh import KachakaApiClientByZenoh  # noqa: E402
from connect_openrmf_by_zenoh import MapState  # noqa: E402
from connect_openrmf_by_zenoh import Pose  # noqa: E402


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
    node._publish_to_zenoh = MagicMock(return_value=True)
    node.kachaka_client = MagicMock(spec=client_methods)
    return node


def published_payloads(node: KachakaApiClientByZenoh) -> list:
    """Return all payloads published on the completion publisher."""
    return [
        call.args[1] for call in node._publish_to_zenoh.call_args_list if call.args[0] is node.command_is_completed_pub
    ]


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
