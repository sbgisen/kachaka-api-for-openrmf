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
"""Regression tests for shelf_dock / shelf_undock and shelf_driver (Issue #57).

Runs without a Kachaka robot or a Zenoh router: the gRPC client and the
Zenoh publishers are mocked, the same way as test_command_lifecycle.py.
"""
# ruff: noqa: SLF001

import asyncio
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from test_command_lifecycle import make_node  # noqa: E402
from test_command_lifecycle import published_payloads  # noqa: E402
from test_command_lifecycle import stub_start_command_response  # noqa: E402

LOCATIONS = [SimpleNamespace(id='loc-a1', name='a1'), SimpleNamespace(id='loc-a2', name='a2')]


def make_shelf_node(driver: object) -> object:
    """Build a node with the given shelf_driver and a mocked locations/shelf stub."""
    node = make_node(client_methods=['stub', 'cancel_command'])
    node.shelf_driver = driver
    node.grpc_connection_check = MagicMock(return_value=True)
    node.kachaka_client.stub.GetLocations = MagicMock(return_value=SimpleNamespace(locations=LOCATIONS))
    node.kachaka_client.stub.GetMovingShelfId = MagicMock(return_value=SimpleNamespace(shelf_id=''))
    node.kachaka_client.stub.StartCommand = MagicMock(return_value=stub_start_command_response(command_id='grpc-s'))
    return node


def dock(task_id: str = 'cmd-dock', location: str = 'a1', forward: bool = True) -> dict:
    return {'id': task_id, 'method': 'shelf_dock', 'args': {'location': location, 'forward': forward}}


def undock(task_id: str = 'cmd-undock', location: str = 'a1') -> dict:
    return {'id': task_id, 'method': 'shelf_undock', 'args': {'location': location}}


def finish_on_robot(node: object, command_field: str, success: bool = True, error_code: int = 0) -> None:
    """Make the next publish_result() see the bound command as finished."""
    node.saw_running = True
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'grpc-s',
        'state': 'COMMAND_STATE_PENDING'
    })
    result = {'success': True} if success else {'errorCode': error_code}
    node._get_last_command_result_response = MagicMock(return_value={
        'commandId': 'grpc-s',
        'result': result,
        'command': {
            command_field: {}
        },
    })


def sent_command(node: object) -> object:
    return node.kachaka_client.stub.StartCommand.call_args.args[0].command


# ---------------------------------------------------------------------------
# Prechecks: no API call, precheck_* reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('driver, reason', [(None, 'precheck_no_shelf_driver'),
                                            ('turbo', 'precheck_invalid_shelf_driver')])
def test_missing_or_unknown_shelf_driver_is_rejected(driver: object, reason: str) -> None:
    node = make_shelf_node(driver)
    node._execute_command(dock())
    assert published_payloads(node) == [{'id': 'cmd-dock', 'is_completed': True, 'success': False, 'reason': reason}]
    node.kachaka_client.stub.StartCommand.assert_not_called()
    assert node.task_id is None


@pytest.mark.parametrize('driver', ['pro', 'standard', 'dry_run'])
def test_unknown_location_is_rejected_without_api_call(driver: str) -> None:
    node = make_shelf_node(driver)
    node._execute_command(dock(location='zz'))
    assert published_payloads(node) == [{
        'id': 'cmd-dock',
        'is_completed': True,
        'success': False,
        'reason': 'precheck_unknown_location'
    }]
    node.kachaka_client.stub.StartCommand.assert_not_called()


@pytest.mark.parametrize('args', [{}, {'location': 1}, {'location': 'a1', 'forward': 'yes'}])
def test_invalid_args_are_rejected(args: dict) -> None:
    node = make_shelf_node('standard')
    node._execute_command({'id': 'cmd-bad', 'method': 'shelf_dock', 'args': args})
    assert published_payloads(node)[-1]['reason'] == 'precheck_invalid_args'
    node.kachaka_client.stub.StartCommand.assert_not_called()


# ---------------------------------------------------------------------------
# pro / standard: dispatched through the async stub path
# ---------------------------------------------------------------------------


def test_pro_dock_dispatches_dock_any_shelf_with_registration() -> None:
    node = make_shelf_node('pro')
    node._execute_command(dock(forward=True))
    command = sent_command(node)
    assert command.WhichOneof('command') == 'dock_any_shelf_with_registration_command'
    assert command.dock_any_shelf_with_registration_command.target_location_id == 'loc-a1'
    assert command.dock_any_shelf_with_registration_command.dock_forward is True
    # Bound on the existing async path: own id, no completion yet.
    assert node.current_command_id == 'grpc-s'
    assert node.is_async_command is True
    assert published_payloads(node) == []


@pytest.mark.parametrize(
    'driver, method, field',
    [
        ('standard', 'shelf_dock', 'dock_shelf_command'),
        ('standard', 'shelf_undock', 'undock_shelf_command'),
        ('pro', 'shelf_undock', 'undock_shelf_command'),
    ],
)
def test_driver_maps_to_expected_kachaka_command(driver: str, method: str, field: str) -> None:
    node = make_shelf_node(driver)
    node._execute_command(dock() if method == 'shelf_dock' else undock())
    assert sent_command(node).WhichOneof('command') == field


def test_standard_dock_success_requires_moving_shelf() -> None:
    node = make_shelf_node('standard')
    node._execute_command(dock())
    finish_on_robot(node, 'dockShelfCommand')
    node.kachaka_client.stub.GetMovingShelfId.return_value = SimpleNamespace(shelf_id='S1')
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{'id': 'cmd-dock', 'is_completed': True, 'success': True}]


def test_standard_dock_reported_success_without_shelf_is_physical_failure() -> None:
    node = make_shelf_node('standard')
    node._execute_command(dock())
    finish_on_robot(node, 'dockShelfCommand')
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{
        'id': 'cmd-dock',
        'is_completed': True,
        'success': False,
        'reason': 'physical_shelf_not_docked'
    }]


def test_standard_undock_success_requires_no_moving_shelf() -> None:
    node = make_shelf_node('standard')
    node._execute_command(undock())
    finish_on_robot(node, 'undockShelfCommand')
    node.kachaka_client.stub.GetMovingShelfId.return_value = SimpleNamespace(shelf_id='S1')
    asyncio.run(node.publish_result())
    assert published_payloads(node)[-1]['reason'] == 'physical_shelf_still_docked'


def test_kachaka_failure_is_physical_and_not_retried() -> None:
    node = make_shelf_node('standard')
    node.retry_enabled = True
    node.max_navigation_retries = 3
    node._execute_command(dock())
    finish_on_robot(node, 'dockShelfCommand', success=False, error_code=0)
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{
        'id': 'cmd-dock',
        'is_completed': True,
        'success': False,
        'reason': 'physical_command_failed'
    }]
    assert node.kachaka_client.stub.StartCommand.call_count == 1


def test_own_shelf_command_is_not_treated_as_external() -> None:
    node = make_shelf_node('standard')
    node._execute_command(dock())
    node._get_command_state_response = MagicMock(return_value={
        'commandId': 'grpc-s',
        'state': 'COMMAND_STATE_RUNNING',
        'command': {
            'dockShelfCommand': {}
        },
    })
    asyncio.run(node.monitor_external_control())
    assert node.external_control_active is False
    assert node.task_id == 'cmd-dock'


# ---------------------------------------------------------------------------
# dry_run: no shelf API, simulated carrying state
# ---------------------------------------------------------------------------


def test_dry_run_dock_then_undock_succeed_without_shelf_api() -> None:
    node = make_shelf_node('dry_run')
    node._execute_command(dock())
    assert published_payloads(node) == []
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{'id': 'cmd-dock', 'is_completed': True, 'success': True}]
    assert node._dry_run_carrying is True

    node._execute_command(undock())
    asyncio.run(node.publish_result())
    assert published_payloads(node)[-1] == {'id': 'cmd-undock', 'is_completed': True, 'success': True}
    assert node._dry_run_carrying is False
    node.kachaka_client.stub.StartCommand.assert_not_called()
    node.kachaka_client.stub.GetMovingShelfId.assert_not_called()


def test_dry_run_reports_in_progress_until_duration_elapses() -> None:
    node = make_shelf_node('dry_run')
    node.shelf_dry_run_duration = 60.0
    node._execute_command(dock())
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{'id': 'cmd-dock', 'is_completed': False}]
    assert node._dry_run_carrying is False


def test_dry_run_cancel_by_new_command_completes_shelf_task() -> None:
    node = make_shelf_node('dry_run')
    node.shelf_dry_run_duration = 60.0
    node._execute_command(dock())
    node._execute_command(undock(task_id='cmd-next'))
    assert published_payloads(node)[0] == {'id': 'cmd-dock', 'is_completed': True, 'success': False}
    assert node._dry_run_carrying is False


def test_dry_run_timeout_is_reported_by_watchdog() -> None:
    node = make_shelf_node('dry_run')
    node.shelf_dry_run_duration = 600.0
    node._execute_command(dock())
    node.last_progress_at = time.monotonic() - node.command_completion_timeout - 1.0
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{'id': 'cmd-dock', 'is_completed': True, 'success': False}]
    assert node.task_id is None
    assert node._dry_run_carrying is False


def test_dry_run_reissue_adopts_new_id_and_still_completes() -> None:
    node = make_shelf_node('dry_run')
    node.shelf_dry_run_duration = 60.0
    node._execute_command(dock())
    node._execute_command(dock(task_id='cmd-dock-2'))
    node._dry_run_shelf = (node._dry_run_shelf[0], 0.0)
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{'id': 'cmd-dock-2', 'is_completed': True, 'success': True}]
    assert node._dry_run_carrying is True
