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
"""Regression tests for shelf_dock / shelf_undock, shelf_driver and the shelf prechecks (Issue #57, #58).

Runs without a Kachaka robot or a Zenoh router: the gRPC client and the
Zenoh publishers are mocked, the same way as test_command_lifecycle.py.
"""
# ruff: noqa: SLF001

import asyncio
import math
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from connect_openrmf_by_zenoh import KachakaApiClientByZenoh  # noqa: E402
import connect_openrmf_by_zenoh  # noqa: E402
from test_command_lifecycle import FakeRpcError  # noqa: E402
from test_command_lifecycle import make_node  # noqa: E402
from test_command_lifecycle import published_payloads  # noqa: E402
from test_command_lifecycle import stub_start_command_response  # noqa: E402


def pose(x: float, y: float, theta: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(x=x, y=y, theta=theta)


# a1 / a2 side by side (0.95m apart); the robot faces +x.
LOCATIONS = [
    SimpleNamespace(id='loc-a1', name='a1', pose=pose(1.0, 0.0)),
    SimpleNamespace(id='loc-a2', name='a2', pose=pose(1.0, 0.95)),
]
SHELVES = [SimpleNamespace(id='S1', pose=pose(1.0, 0.0)), SimpleNamespace(id='S2', pose=pose(1.0, 0.95))]
IN_FRONT_OF_A1 = pose(0.5, 0.0)
AT_A1 = pose(1.0, 0.0)


def make_shelf_node(driver: object, carrying: str = '') -> object:
    """Build a node with the given shelf_driver and a mocked locations/shelf stub.

    The robot stands in front of a1 when empty and at a1 when carrying.
    """
    node = make_node(client_methods=['stub', 'cancel_command'])
    node.shelf_driver = driver
    node.grpc_connection_check = MagicMock(return_value=True)
    node.kachaka_client.stub.GetLocations = MagicMock(return_value=SimpleNamespace(locations=LOCATIONS))
    node.kachaka_client.stub.GetShelves = MagicMock(return_value=SimpleNamespace(shelves=SHELVES))
    node.kachaka_client.stub.GetMovingShelfId = MagicMock(return_value=SimpleNamespace(shelf_id=carrying))
    node._dry_run_moving_shelf_id = carrying
    place_robot(node, AT_A1 if carrying else IN_FRONT_OF_A1)
    node.kachaka_client.stub.StartCommand = MagicMock(return_value=stub_start_command_response(command_id='grpc-s'))
    return node


def place_robot(node: object, robot_pose: SimpleNamespace) -> None:
    node.kachaka_client.stub.GetRobotPose = MagicMock(return_value=SimpleNamespace(pose=robot_pose))


def rejected(task_id: str, reason: str) -> list:
    return [{'id': task_id, 'is_completed': True, 'success': False, 'reason': reason}]


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
    node = make_shelf_node(driver, carrying='S1' if method == 'shelf_undock' else '')
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
    node = make_shelf_node('standard', carrying='S1')
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


def test_positive_error_code_failure_is_not_retried() -> None:
    """A positive error_code is retriable for ordinary commands; precheck_/physical_ must still stop (N1)."""
    node = make_shelf_node('standard')
    node.retry_enabled = True
    node.max_navigation_retries = 3
    node.retry_on_error_types = ['Error']
    node._execute_command(dock())
    finish_on_robot(node, 'dockShelfCommand', success=False, error_code=5)
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{
        'id': 'cmd-dock',
        'is_completed': True,
        'success': False,
        'reason': 'physical_command_failed'
    }]
    assert node.kachaka_client.stub.StartCommand.call_count == 1


@pytest.mark.parametrize('method, command_field', [('shelf_dock', 'dockShelfCommand'),
                                                   ('shelf_undock', 'undockShelfCommand')])
def test_get_moving_shelf_id_rpc_error_is_physical_state_unknown(method: str, command_field: str) -> None:
    """If the post-completion shelf check cannot run, the result is a failure (N2)."""
    node = make_shelf_node('standard', carrying='S1' if method == 'shelf_undock' else '')
    node._execute_command(dock() if method == 'shelf_dock' else undock())
    node.kachaka_client.stub.GetMovingShelfId = MagicMock(side_effect=FakeRpcError())
    finish_on_robot(node, command_field)
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{
        'id': 'cmd-dock' if method == 'shelf_dock' else 'cmd-undock',
        'is_completed': True,
        'success': False,
        'reason': 'physical_shelf_state_unknown'
    }]


@pytest.mark.parametrize('config, expected', [({}, None), ({'shelf_driver': 'pro'}, 'pro')])
def test_shelf_driver_comes_from_config_without_default(tmp_path: Path, config: dict, expected: object) -> None:
    """Load shelf_driver through __init__'s config path; an absent key gets no default (N3)."""
    config_file = tmp_path / 'config.yaml'
    config_file.write_text(yaml.safe_dump(config))
    with patch.object(connect_openrmf_by_zenoh, 'zenoh'), \
            patch.object(connect_openrmf_by_zenoh, 'KachakaApiClientWithKeepalive'):
        node = KachakaApiClientByZenoh(zenoh_router='127.0.0.1:7447', config_file=str(config_file))
    assert node.shelf_driver == expected
    if expected is None:
        shelf_node = make_shelf_node(node.shelf_driver)
        shelf_node._execute_command(dock())
        assert published_payloads(shelf_node)[-1]['reason'] == 'precheck_no_shelf_driver'


def test_shelf_dispatch_reconciles_pending_snapshot_like_other_async_commands() -> None:
    """A RUNNING id seen during a shelf dispatch is reconciled, not discarded as for sync methods (N4)."""
    node = make_shelf_node('standard')
    node.external_control_active = False
    node._external_command_id = None
    node._handle_external_command_started = MagicMock()

    def start_command(*_args: object, **_kwargs: object) -> object:
        node._pending_dispatch_snapshot_id = 'ext-during-dispatch'
        return stub_start_command_response(command_id='grpc-s')

    node.kachaka_client.stub.StartCommand = MagicMock(side_effect=start_command)
    node._execute_command(dock())
    node._handle_external_command_started.assert_called_once_with('ext-during-dispatch')


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
    assert node._dry_run_moving_shelf_id == 'S1'

    place_robot(node, AT_A1)
    node._execute_command(undock())
    asyncio.run(node.publish_result())
    assert published_payloads(node)[-1] == {'id': 'cmd-undock', 'is_completed': True, 'success': True}
    assert node._dry_run_moving_shelf_id == ''
    node.kachaka_client.stub.StartCommand.assert_not_called()
    node.kachaka_client.stub.GetMovingShelfId.assert_not_called()


# ---------------------------------------------------------------------------
# Issue #58: position / carrying-state prechecks and shelf-ID match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('driver', ['standard', 'dry_run'])
@pytest.mark.parametrize(
    'robot_pose',
    [pose(0.9, 0.0), pose(-1.0, 0.0), pose(0.5, 0.0, math.pi / 2),
     pose(0.5, 0.5)],
    ids=['too_close', 'too_far', 'facing_away', 'off_axis'],
)
def test_dock_not_in_front_is_rejected_without_api_call(driver: str, robot_pose: SimpleNamespace) -> None:
    node = make_shelf_node(driver)
    place_robot(node, robot_pose)
    node._execute_command(dock())
    assert published_payloads(node) == rejected('cmd-dock', 'precheck_not_in_front')
    node.kachaka_client.stub.StartCommand.assert_not_called()


def test_front_yaw_offset_allows_backing_in() -> None:
    node = make_shelf_node('standard')
    node.shelf_front_yaw_offset = math.pi
    place_robot(node, pose(0.5, 0.0, math.pi))
    node._execute_command(dock())
    assert sent_command(node).WhichOneof('command') == 'dock_shelf_command'


@pytest.mark.parametrize('driver', ['standard', 'dry_run'])
def test_undock_away_from_location_is_rejected(driver: str) -> None:
    node = make_shelf_node(driver, carrying='S1')
    place_robot(node, IN_FRONT_OF_A1)
    node._execute_command(undock())
    assert published_payloads(node) == rejected('cmd-undock', 'precheck_not_in_front')
    node.kachaka_client.stub.StartCommand.assert_not_called()


@pytest.mark.parametrize('driver', ['standard', 'dry_run'])
def test_dock_while_carrying_another_shelf_is_rejected(driver: str) -> None:
    node = make_shelf_node(driver, carrying='S2')
    node._carried_shelf = ('loc-a2', 'S2')
    node._execute_command(dock())
    assert published_payloads(node) == rejected('cmd-dock', 'precheck_already_carrying_other')
    node.kachaka_client.stub.StartCommand.assert_not_called()


def test_dock_while_carrying_away_from_location_is_rejected_after_restart() -> None:
    node = make_shelf_node('dry_run', carrying='S1')
    place_robot(node, IN_FRONT_OF_A1)
    node._execute_command(dock())
    assert published_payloads(node) == rejected('cmd-dock', 'precheck_already_carrying_other')


def test_dry_run_rerun_of_finished_dock_succeeds_without_api() -> None:
    node = make_shelf_node('dry_run')
    node._execute_command(dock())
    asyncio.run(node.publish_result())
    place_robot(node, AT_A1)
    node._execute_command(dock(task_id='cmd-dock-again'))
    assert published_payloads(node)[-1] == {'id': 'cmd-dock-again', 'is_completed': True, 'success': True}
    assert node._dry_run_shelf is None


@pytest.mark.parametrize('driver', ['standard', 'dry_run'])
def test_rerun_of_finished_dock_after_restart_uses_position_only(driver: str) -> None:
    node = make_shelf_node(driver, carrying='S1')
    node._execute_command(dock())
    assert published_payloads(node) == [{'id': 'cmd-dock', 'is_completed': True, 'success': True}]
    node.kachaka_client.stub.StartCommand.assert_not_called()


@pytest.mark.parametrize('driver', ['standard', 'dry_run'])
def test_rerun_of_finished_undock_succeeds_without_api(driver: str) -> None:
    node = make_shelf_node(driver)
    place_robot(node, AT_A1)
    node._execute_command(undock())
    assert published_payloads(node) == [{'id': 'cmd-undock', 'is_completed': True, 'success': True}]
    node.kachaka_client.stub.StartCommand.assert_not_called()


def test_undock_with_no_shelf_anywhere_is_unknown() -> None:
    node = make_shelf_node('dry_run')
    node.kachaka_client.stub.GetShelves.return_value = SimpleNamespace(shelves=SHELVES[1:])
    node._execute_command(undock())
    assert published_payloads(node) == rejected('cmd-undock', 'precheck_unknown')


def test_dry_run_mismatched_shelf_is_physical_and_blocks_redock() -> None:
    node = make_shelf_node('dry_run')
    node.shelf_dry_run_shelf_id = 'S2'
    node._execute_command(dock())
    asyncio.run(node.publish_result())
    assert published_payloads(node) == rejected('cmd-dock', 'physical_shelf_mismatch')
    assert node._dry_run_moving_shelf_id == 'S2'

    place_robot(node, AT_A1)
    node._execute_command(dock(task_id='cmd-dock-again'))
    assert published_payloads(node)[-1] == rejected('cmd-dock-again', 'precheck_already_carrying_other')[0]


def test_standard_dock_of_wrong_shelf_is_mismatch() -> None:
    node = make_shelf_node('standard')
    node._execute_command(dock())
    finish_on_robot(node, 'dockShelfCommand')
    node.kachaka_client.stub.GetMovingShelfId.return_value = SimpleNamespace(shelf_id='S2')
    asyncio.run(node.publish_result())
    assert published_payloads(node) == rejected('cmd-dock', 'physical_shelf_mismatch')
    assert node._carried_shelf == (None, 'S2')


def test_unregistered_shelf_dock_relies_on_front_check() -> None:
    node = make_shelf_node('standard')
    node.kachaka_client.stub.GetShelves.return_value = SimpleNamespace(shelves=SHELVES[1:])
    node._execute_command(dock())
    finish_on_robot(node, 'dockShelfCommand')
    node.kachaka_client.stub.GetMovingShelfId.return_value = SimpleNamespace(shelf_id='S9')
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{'id': 'cmd-dock', 'is_completed': True, 'success': True}]
    assert node._carried_shelf == ('loc-a1', 'S9')


@pytest.mark.parametrize('stub_method', ['GetMovingShelfId', 'GetShelves', 'GetRobotPose'])
def test_unreadable_state_is_rejected(stub_method: str) -> None:
    node = make_shelf_node('standard')
    getattr(node.kachaka_client.stub, stub_method).side_effect = FakeRpcError()
    node._execute_command(dock())
    assert published_payloads(node) == rejected('cmd-dock', 'precheck_state_unavailable')
    node.kachaka_client.stub.StartCommand.assert_not_called()


def test_dry_run_reports_in_progress_until_duration_elapses() -> None:
    node = make_shelf_node('dry_run')
    node.shelf_dry_run_duration = 60.0
    node._execute_command(dock())
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{'id': 'cmd-dock', 'is_completed': False}]
    assert node._dry_run_moving_shelf_id == ''


def test_dry_run_cancel_by_new_command_completes_shelf_task() -> None:
    node = make_shelf_node('dry_run')
    node.shelf_dry_run_duration = 60.0
    node._execute_command(dock())
    node._execute_command(undock(task_id='cmd-next'))
    assert published_payloads(node)[0] == {'id': 'cmd-dock', 'is_completed': True, 'success': False}
    assert node._dry_run_moving_shelf_id == ''


def test_dry_run_cancel_of_simulated_shelf_command_does_not_call_robot() -> None:
    node = make_shelf_node('dry_run')
    node.shelf_dry_run_duration = 60.0
    node._execute_command(dock())
    node._execute_command({'id': 'cmd-cancel', 'method': 'cancel_command', 'args': {}})
    node.kachaka_client.cancel_command.assert_not_called()
    assert published_payloads(node) == [
        {
            'id': 'cmd-dock',
            'is_completed': True,
            'success': False
        },
        {
            'id': 'cmd-cancel',
            'is_completed': True,
            'success': True
        },
    ]
    assert node._dry_run_moving_shelf_id == ''


def test_dry_run_cancel_of_other_command_calls_robot() -> None:
    """A move started on the robot is cancelled on the robot even during dry_run."""
    node = make_shelf_node('dry_run')
    node.kachaka_client.move_to_pose = MagicMock()
    node._execute_command({'id': 'cmd-move', 'method': 'move_to_pose', 'args': {'x': 1.0, 'y': 2.0, 'yaw': 0.0}})
    node.kachaka_client.stub.StartCommand.assert_called_once()
    assert sent_command(node).WhichOneof('command') == 'move_to_pose_command'
    node._execute_command({'id': 'cmd-cancel', 'method': 'cancel_command', 'args': {}})
    node.kachaka_client.cancel_command.assert_called_once_with()


def test_dry_run_cancel_after_simulated_shelf_command_finished_calls_robot() -> None:
    """Once the simulated shelf command has finished, a cancel goes to the robot."""
    node = make_shelf_node('dry_run')
    node._execute_command(dock())
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{'id': 'cmd-dock', 'is_completed': True, 'success': True}]
    node._execute_command({'id': 'cmd-cancel', 'method': 'cancel_command', 'args': {}})
    node.kachaka_client.cancel_command.assert_called_once_with()


def test_dry_run_move_superseding_simulated_shelf_command_reaches_robot() -> None:
    """A move that replaces a simulated shelf command is sent to the robot, and so is its cancel."""
    node = make_shelf_node('dry_run')
    node.shelf_dry_run_duration = 60.0
    node.kachaka_client.move_to_pose = MagicMock()
    node._execute_command(dock())
    node._execute_command({'id': 'cmd-move', 'method': 'move_to_pose', 'args': {'x': 1.0, 'y': 2.0, 'yaw': 0.0}})
    node.kachaka_client.stub.StartCommand.assert_called_once()
    assert sent_command(node).WhichOneof('command') == 'move_to_pose_command'
    node._execute_command({'id': 'cmd-cancel', 'method': 'cancel_command', 'args': {}})
    node.kachaka_client.cancel_command.assert_called_once_with()


def test_command_completion_timeout_defaults_to_180_without_config(tmp_path: Path) -> None:
    """The bridge watchdog (180 s) must stay shorter than the adapter's completion wait (240 s)."""
    config_file = tmp_path / 'config.yaml'
    config_file.write_text(yaml.safe_dump({}))
    with patch.object(connect_openrmf_by_zenoh, 'zenoh'), \
            patch.object(connect_openrmf_by_zenoh, 'KachakaApiClientWithKeepalive'):
        node = KachakaApiClientByZenoh(zenoh_router='127.0.0.1:7447', config_file=str(config_file))
    assert node.command_completion_timeout == 180.0


def test_config_yaml_command_completion_is_180() -> None:
    config_file = Path(__file__).resolve().parents[1] / 'config' / 'config.yaml'
    config = yaml.safe_load(config_file.read_text())
    assert config['timeouts']['command_completion'] == 180.0


def test_dry_run_timeout_is_reported_by_watchdog() -> None:
    node = make_shelf_node('dry_run')
    node.shelf_dry_run_duration = 600.0
    node._execute_command(dock())
    node.last_progress_at = time.monotonic() - node.command_completion_timeout - 1.0
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{'id': 'cmd-dock', 'is_completed': True, 'success': False}]
    assert node.task_id is None
    assert node._dry_run_moving_shelf_id == ''


def test_dry_run_reissue_adopts_new_id_and_still_completes() -> None:
    node = make_shelf_node('dry_run')
    node.shelf_dry_run_duration = 60.0
    node._execute_command(dock())
    node._execute_command(dock(task_id='cmd-dock-2'))
    node._dry_run_shelf = (node._dry_run_shelf[0], 0.0)
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{'id': 'cmd-dock-2', 'is_completed': True, 'success': True}]
    assert node._dry_run_moving_shelf_id == 'S1'


# ---------------------------------------------------------------------------
# Review follow-ups (Issue #58 N1-N3): config, boundaries, nearest shelf, undock memory
# ---------------------------------------------------------------------------

FULL_SECTION = {
    'front_distance_min': 0.1,
    'front_distance_max': 2.0,
    'front_yaw_tolerance': 0.5,
    'front_yaw_offset': 1.0,
    'at_location_tolerance': 0.6,
    'match_radius': 0.7,
}


def test_shelf_precheck_config_values_are_applied() -> None:
    node = make_node()
    node._load_shelf_precheck_config(dict(FULL_SECTION))
    for key, value in FULL_SECTION.items():
        assert getattr(node, f'shelf_{key}') == value


def test_shelf_precheck_config_keeps_defaults_for_missing_keys() -> None:
    node = make_node()
    node._load_shelf_precheck_config({})
    assert node.shelf_front_distance_max == 1.2
    assert node.shelf_match_radius == 0.3


@pytest.mark.parametrize('section, key', [
    (None, 'shelf_precheck'),
    ({
        'front_yaw_tolerance': 'abc'
    }, 'front_yaw_tolerance'),
    ({
        'front_yaw_tolerance': None
    }, 'front_yaw_tolerance'),
    ({
        'match_radius': -0.1
    }, 'match_radius'),
    ({
        'at_location_tolerance': float('nan')
    }, 'at_location_tolerance'),
    ({
        'match_radius': float('inf')
    }, 'match_radius'),
    ({
        'front_distance_min': 1.5,
        'front_distance_max': 1.0
    }, 'front_distance_min'),
])
def test_shelf_precheck_config_rejects_bad_values(section: object, key: str) -> None:
    node = make_node()
    with pytest.raises(ValueError, match=key):
        node._load_shelf_precheck_config(section)
    assert node.shelf_match_radius == 0.3  # nothing applied


# Exactly representable in binary, so the boundary comparisons are stable.
@pytest.mark.parametrize('distance, ok', [(0.5, True), (0.5625, False), (0.25, True)])
def test_dock_front_distance_min_max_inclusive(distance: float, ok: bool) -> None:
    node = make_shelf_node('dry_run')
    node.shelf_front_distance_min = 0.25
    node.shelf_front_distance_max = 0.5
    place_robot(node, pose(1.0 - distance, 0.0))
    node._execute_command(dock())
    assert (published_payloads(node) != rejected('cmd-dock', 'precheck_not_in_front')) is ok


@pytest.mark.parametrize('offset, ok', [(0.5, True), (0.625, False)])
def test_at_location_tolerance_boundary(offset: float, ok: bool) -> None:
    node = make_shelf_node('dry_run', carrying='S1')
    node.shelf_at_location_tolerance = 0.5
    place_robot(node, pose(1.0 - offset, 0.0))
    node._execute_command(undock())
    assert (published_payloads(node) != rejected('cmd-undock', 'precheck_not_in_front')) is ok


@pytest.mark.parametrize('shelf_x, expected', [(1.5, 'S1'), (1.625, None)])
def test_match_radius_boundary(shelf_x: float, expected: object) -> None:
    node = make_shelf_node('dry_run')
    node.shelf_match_radius = 0.5
    node.kachaka_client.stub.GetShelves = MagicMock(return_value=SimpleNamespace(
        shelves=[SimpleNamespace(id='S1', pose=pose(shelf_x, 0.0))]))
    assert node._shelf_at_location(LOCATIONS[0]) == expected


def test_nearest_shelf_within_radius_is_chosen() -> None:
    node = make_shelf_node('dry_run')
    node.shelf_match_radius = 0.5
    shelves = [
        SimpleNamespace(id='FAR', pose=pose(1.25, 0.0)),
        SimpleNamespace(id='NEAR', pose=pose(1.0, 0.125)),
        SimpleNamespace(id='MID', pose=pose(1.0, -0.25)),
    ]
    node.kachaka_client.stub.GetShelves = MagicMock(return_value=SimpleNamespace(shelves=shelves))
    assert node._shelf_at_location(LOCATIONS[0]) == 'NEAR'


def test_dry_run_undock_clears_carried_shelf_memory() -> None:
    node = make_shelf_node('dry_run')
    node._execute_command(dock())
    asyncio.run(node.publish_result())
    assert node._carried_shelf == ('loc-a1', 'S1')
    place_robot(node, AT_A1)
    node._execute_command(undock())
    asyncio.run(node.publish_result())
    assert published_payloads(node)[-1] == {'id': 'cmd-undock', 'is_completed': True, 'success': True}
    assert node._carried_shelf is None


def dry_run_dock_and_undock_at_a1() -> object:
    """Dock S1 at a1 and undock it there again, so the bridge remembers (loc-a1, S1)."""
    node = make_shelf_node('dry_run')
    node._execute_command(dock())
    asyncio.run(node.publish_result())
    place_robot(node, AT_A1)
    node._execute_command(undock())
    asyncio.run(node.publish_result())
    return node


def test_undock_remembers_placed_shelf() -> None:
    node = make_shelf_node('standard', carrying='S1')
    node._execute_command(undock())
    finish_on_robot(node, 'undockShelfCommand')
    node.kachaka_client.stub.GetMovingShelfId.return_value = SimpleNamespace(shelf_id='')
    asyncio.run(node.publish_result())
    assert published_payloads(node) == [{'id': 'cmd-undock', 'is_completed': True, 'success': True}]
    assert node._placed_shelf == ('loc-a1', 'S1')


def test_rerun_undock_with_remembered_shelf_at_location_succeeds() -> None:
    node = dry_run_dock_and_undock_at_a1()
    node._execute_command(undock(task_id='cmd-undock-again'))
    assert published_payloads(node)[-1] == {'id': 'cmd-undock-again', 'is_completed': True, 'success': True}
    assert node._dry_run_shelf is None


def test_rerun_undock_with_another_shelf_at_location_is_unknown() -> None:
    node = dry_run_dock_and_undock_at_a1()
    node.kachaka_client.stub.GetShelves.return_value = SimpleNamespace(
        shelves=[SimpleNamespace(id='S2', pose=pose(1.0, 0.0))])
    node._execute_command(undock(task_id='cmd-undock-again'))
    assert published_payloads(node)[-1] == rejected('cmd-undock-again', 'precheck_unknown')[0]


def test_rerun_undock_of_remembered_shelf_at_another_location_is_unknown() -> None:
    """The memory is (location, shelf): the same shelf found at another location does not match."""
    node = dry_run_dock_and_undock_at_a1()
    node.kachaka_client.stub.GetShelves.return_value = SimpleNamespace(
        shelves=[SimpleNamespace(id='S1', pose=pose(1.0, 0.95))])
    place_robot(node, pose(1.0, 0.95))
    node._execute_command(undock(task_id='cmd-undock-a2', location='a2'))
    assert published_payloads(node)[-1] == rejected('cmd-undock-a2', 'precheck_unknown')[0]


def test_placed_shelf_memory_survives_a_dock() -> None:
    """A successful dock does not clear the memory, so it never falls back to the looser no-memory check."""
    node = dry_run_dock_and_undock_at_a1()
    place_robot(node, IN_FRONT_OF_A1)
    node._execute_command(dock(task_id='cmd-dock-again'))
    asyncio.run(node.publish_result())
    assert published_payloads(node)[-1] == {'id': 'cmd-dock-again', 'is_completed': True, 'success': True}
    assert node._placed_shelf == ('loc-a1', 'S1')


def test_rerun_undock_without_memory_accepts_any_shelf_at_location() -> None:
    node = make_shelf_node('dry_run')
    place_robot(node, AT_A1)
    node.kachaka_client.stub.GetShelves.return_value = SimpleNamespace(
        shelves=[SimpleNamespace(id='S2', pose=pose(1.0, 0.0))])
    node._execute_command(undock())
    assert published_payloads(node) == [{'id': 'cmd-undock', 'is_completed': True, 'success': True}]


# ---------------------------------------------------------------------------
# switch_map while carrying a shelf (Issue #59)
# ---------------------------------------------------------------------------

SWITCH_MAP = {'map_name': '10F', 'pose': {'x': 1.0, 'y': 2.0, 'theta': 0.5}}


def make_switch_map_node(driver: object, carrying: str) -> object:
    """Shelf node on map 8F whose switch_map to 10F succeeds."""
    node = make_shelf_node(driver, carrying)
    node.kachaka_client.get_map_list = MagicMock(return_value=[{
        'id': 'm8',
        'name': '8F'
    }, {
        'id': 'm10',
        'name': '10F'
    }])
    node.kachaka_client.get_current_map_id = MagicMock(return_value='m8')
    node.kachaka_client.switch_map = MagicMock(return_value={'success': True})
    return node


@pytest.mark.parametrize('driver', ['standard', 'dry_run'])
def test_switch_map_while_carrying_is_rejected_without_standard_inherit(driver: str) -> None:
    node = make_switch_map_node(driver, carrying='S1')
    node._execute_switch_map_sync(SWITCH_MAP, 'cmd-map')
    assert published_payloads(node) == rejected('cmd-map', 'carrying_on_standard')
    node.kachaka_client.switch_map.assert_not_called()
    node.map_name_pub.assert_not_called()


@pytest.mark.parametrize('driver', [None, 'pro', 'standard', 'dry_run'])
def test_switch_map_when_empty_switches_without_inherit(driver: object) -> None:
    node = make_switch_map_node(driver, carrying='')
    node._execute_switch_map_sync(SWITCH_MAP, 'cmd-map')
    node.kachaka_client.switch_map.assert_called_once_with(map_id='m10', pose=SWITCH_MAP['pose'])
    assert published_payloads(node) == [{'id': 'cmd-map', 'is_completed': True, 'success': True}]


def test_switch_map_without_shelf_driver_ignores_carrying_state() -> None:
    node = make_switch_map_node(None, carrying='S1')
    node._execute_switch_map_sync(SWITCH_MAP, 'cmd-map')
    node.kachaka_client.switch_map.assert_called_once_with(map_id='m10', pose=SWITCH_MAP['pose'])
    node.kachaka_client.stub.GetMovingShelfId.assert_not_called()


def test_pro_switch_map_while_carrying_inherits_and_completes_after_map_and_pose() -> None:
    node = make_switch_map_node('pro', carrying='S1')
    node._execute_switch_map_sync(SWITCH_MAP, 'cmd-map')
    node.kachaka_client.switch_map.assert_called_once_with(map_id='m10',
                                                           pose=SWITCH_MAP['pose'],
                                                           inherit_docking_state_and_docked_shelf=True)
    order = [c.args[0] for c in node._publish_to_zenoh.call_args_list]
    assert order == [node.map_name_pub, node.pose_pub, node.command_is_completed_pub]
    assert published_payloads(node) == [{'id': 'cmd-map', 'is_completed': True, 'success': True}]


def test_switch_map_shelf_state_rpc_error_is_rejected() -> None:
    node = make_switch_map_node('pro', carrying='S1')
    node.kachaka_client.stub.GetMovingShelfId = MagicMock(side_effect=FakeRpcError())
    node._execute_switch_map_sync(SWITCH_MAP, 'cmd-map')
    assert published_payloads(node) == rejected('cmd-map', 'precheck_state_unavailable')
    node.kachaka_client.switch_map.assert_not_called()


@pytest.mark.parametrize('driver', ['standard', 'dry_run'])
def test_switch_map_to_current_map_while_carrying_still_succeeds(driver: str) -> None:
    node = make_switch_map_node(driver, carrying='S1')
    node.kachaka_client.get_current_map_id = MagicMock(return_value='m10')
    node._execute_switch_map_sync(SWITCH_MAP, 'cmd-map')
    node.kachaka_client.switch_map.assert_not_called()
    assert published_payloads(node) == [{'id': 'cmd-map', 'is_completed': True, 'success': True}]
