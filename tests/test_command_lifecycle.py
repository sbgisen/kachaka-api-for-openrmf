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
"""Regression tests for command completion lifecycle."""

# ruff: noqa: SLF001

import asyncio
import logging
from pathlib import Path
import sys
import threading
import time
from typing import Any, Dict, List

from grpc import RpcError  # noqa
from grpc import StatusCode  # noqa
import pytest  # noqa

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from connect_openrmf_by_zenoh import KachakaApiClientByZenoh  # noqa
from connect_openrmf_by_zenoh import MapState  # noqa
from connect_openrmf_by_zenoh import Pose  # noqa


class FakeRpcError(RpcError):
    """Minimal RpcError carrying a status code."""

    def __init__(self, status_code: StatusCode) -> None:
        self._status_code = status_code

    def code(self) -> StatusCode:
        """Return the gRPC status code."""
        return self._status_code

    def details(self) -> str:
        """Return readable error details."""
        return self._status_code.name


class FakeKachakaClient:
    """Kachaka client double with method responses supplied per test."""

    def __init__(self) -> None:
        self.responses: Dict[str, Any] = {}
        self.map_list: List[Dict[str, str]] = [{'id': 'map-1', 'name': 'L1'}]
        self.current_map_id = 'map-0'

    def get_map_list(self) -> List[Dict[str, str]]:
        """Return fake map list."""
        return self.map_list

    def get_current_map_id(self) -> str:
        """Return fake current map id."""
        return self.current_map_id

    def move_to_pose(self, **_kwargs: object) -> Dict[str, Any]:
        """Return fake move_to_pose response."""
        return self.responses.get('move_to_pose', {'success': True, 'commandId': 'cmd-1'})

    def dock(self, **_kwargs: object) -> Dict[str, Any]:
        """Return fake dock response."""
        return self.responses.get('dock', {})

    def switch_map(self, **_kwargs: object) -> Dict[str, Any]:
        """Return fake switch_map response."""
        return self.responses.get('switch_map', {'success': True})


def make_client() -> KachakaApiClientByZenoh:
    """Create a bridge instance without opening real gRPC or Zenoh connections."""
    client = object.__new__(KachakaApiClientByZenoh)
    client.method_mapping = {}
    client.map_name_mapping = {}
    client.reverse_map_name_mapping = {}
    client.robot_name = 'kachaka'
    client.task_id = None
    client.last_command = None
    client.last_command_result = None
    client.last_command_id = None
    client.current_command_id = None
    client.is_async_command = False
    client.saw_running = False
    client.async_command_started_at = None
    client.running_state_wait = 5.0
    client.grpc_stuck_threshold = 1.0
    client.retry_enabled = False
    client.max_navigation_retries = 0
    client.retry_interval = 0.0
    client.retry_on_error_types = []
    client.retry_count = 0
    client._first_grpc_failure_time = None
    client._command_lock = threading.RLock()
    client._client_lock = threading.RLock()
    client.logger = logging.getLogger('test_command_lifecycle')
    client.kachaka_client = FakeKachakaClient()
    client.kachaka_access_point = None
    client.last_pose = Pose.zero()
    client.last_battery = 1.0
    client.map_state = MapState.initial()
    client._command_context_map_name = None
    client.pose_pub = 'pose'
    client.battery_pub = 'battery'
    client.map_name_pub = 'map_name'
    client.command_is_completed_pub = 'completion'
    client.published = []
    client.grpc_connection_check = lambda *_args, **_kwargs: True

    def publish_spy(publisher: str, data: object) -> bool:
        client.published.append((publisher, data))
        return True

    client._publish_to_zenoh = publish_spy
    return client


def completion_payloads(client: KachakaApiClientByZenoh) -> List[Dict[str, Any]]:
    """Return command completion payloads published by the bridge."""
    return [data for publisher, data in client.published if publisher == 'completion']


def run_publish_result(client: KachakaApiClientByZenoh) -> None:
    """Run one async publish_result cycle."""
    asyncio.run(client.publish_result())


def test_move_to_pose_dispatch_does_not_publish_immediate_completion() -> None:
    """Async dispatch success must not publish is_completed=True immediately."""
    client = make_client()
    client.kachaka_client.responses['move_to_pose'] = {'success': True, 'commandId': 'cmd-1'}

    client._execute_command({'id': 'task-1', 'method': 'move_to_pose', 'args': {'x': 1.0, 'y': 2.0, 'yaw': 0.0}})

    assert completion_payloads(client) == []
    assert client.task_id == 'task-1'
    assert client.is_async_command is True
    assert client.current_command_id == 'cmd-1'


def test_running_async_command_publishes_incomplete() -> None:
    """RUNNING state is published as is_completed=False."""
    client = make_client()
    client.task_id = 'task-1'
    client.is_async_command = True
    client.current_command_id = 'cmd-1'
    client._get_command_state_response = lambda: {'commandId': 'cmd-1', 'state': 'COMMAND_STATE_RUNNING'}

    run_publish_result(client)

    assert completion_payloads(client) == [{'id': 'task-1', 'is_completed': False}]
    assert client.saw_running is True


def test_repeated_running_state_never_publishes_completed_true() -> None:
    """Repeated RUNNING polls must keep completion false."""
    client = make_client()
    client.task_id = 'task-1'
    client.is_async_command = True
    client.current_command_id = 'cmd-1'
    client._get_command_state_response = lambda: {'commandId': 'cmd-1', 'state': 'COMMAND_STATE_RUNNING'}

    run_publish_result(client)
    run_publish_result(client)
    run_publish_result(client)

    assert completion_payloads(client) == [
        {
            'id': 'task-1',
            'is_completed': False
        },
        {
            'id': 'task-1',
            'is_completed': False
        },
        {
            'id': 'task-1',
            'is_completed': False
        },
    ]


def test_completion_publishes_true_only_for_matching_command_id() -> None:
    """Completion is accepted only when GetLastCommandResult matches the active command."""
    client = make_client()
    client.task_id = 'task-1'
    client.is_async_command = True
    client.saw_running = True
    client.current_command_id = 'cmd-1'
    client._get_command_state_response = lambda: {'commandId': 'cmd-1', 'state': 'COMMAND_STATE_IDLE'}
    results = iter([
        {
            'commandId': 'other-cmd',
            'result': {
                'success': True
            }
        },
        {
            'commandId': 'cmd-1',
            'result': {
                'success': True
            }
        },
    ])
    client._get_last_command_result_response = lambda: next(results)

    run_publish_result(client)
    assert completion_payloads(client) == []

    run_publish_result(client)
    assert completion_payloads(client) == [{'id': 'task-1', 'is_completed': True, 'success': True}]
    assert client.task_id is None


@pytest.mark.parametrize('response', [{'success': True}, {'result': {'success': True}}])
def test_bare_and_wrapped_result_are_treated_as_success(response: Dict[str, Any]) -> None:
    """Dispatch responses may carry a bare Result or a wrapped Result."""
    client = make_client()
    client.task_id = 'task-1'
    client.kachaka_client.responses['dock'] = response

    client._execute_sync_method('dock', {})

    assert completion_payloads(client) == [{'id': 'task-1', 'is_completed': True, 'success': True}]


def test_response_without_result_information_is_treated_as_success() -> None:
    """Responses without result metadata keep the existing success behavior."""
    client = make_client()
    client.task_id = 'task-1'
    client.kachaka_client.responses['dock'] = {}

    client._execute_sync_method('dock', {})

    assert completion_payloads(client) == [{'id': 'task-1', 'is_completed': True, 'success': True}]


def test_switch_map_publishes_map_name_before_completion() -> None:
    """switch_map must publish map_name and pose before command completion."""
    client = make_client()
    client.task_id = 'task-1'
    client.kachaka_client.responses['switch_map'] = {'success': True}

    client._execute_switch_map_sync({'map_name': 'L1', 'pose': {'x': 3.0, 'y': 4.0, 'theta': 1.57}})

    assert client.published == [
        ('map_name', 'L1'),
        ('pose', [3.0, 4.0, 1.57]),
        ('completion', {
            'id': 'task-1',
            'is_completed': True,
            'success': True
        }),
    ]


def test_switch_map_bare_failure_result_is_not_treated_as_success() -> None:
    """A bare failure Result from switch_map must publish failed completion."""
    client = make_client()
    client.task_id = 'task-1'
    client.kachaka_client.responses['switch_map'] = {'success': False, 'errorCode': 7}

    client._execute_switch_map_sync({'map_name': 'L1', 'pose': {'x': 0.0, 'y': 0.0, 'theta': 0.0}})

    assert completion_payloads(client) == [{'id': 'task-1', 'is_completed': True, 'success': False}]


def test_grpc_stuck_threshold_publishes_failure_and_reconstructs_client() -> None:
    """Persistent DEADLINE_EXCEEDED completes the task as failure and reconstructs the client."""
    client = make_client()
    client.task_id = 'task-1'
    client.is_async_command = True
    client.reconstruct_calls = 0
    client._get_command_state_response = lambda: (_ for _ in ()).throw(FakeRpcError(StatusCode.DEADLINE_EXCEEDED))

    def reconstruct_spy() -> bool:
        client.reconstruct_calls += 1
        return True

    client._reconstruct_kachaka_client = reconstruct_spy
    client._first_grpc_failure_time = time.monotonic() - 2.0

    run_publish_result(client)
    assert completion_payloads(client) == [{'id': 'task-1', 'is_completed': True, 'success': False}]
    assert client.reconstruct_calls == 1
    assert client.task_id is None
