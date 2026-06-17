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
"""Unit tests for the map guard and its self-healing re-sync behaviour.

The robot's actual map (gRPC) is the single source of truth. The cached
telemetry map name can go stale when a switch_map result is lost or the map
is changed outside the bridge (e.g. from the smartphone app); these tests
verify that the guard re-queries the robot instead of trusting the cache.
"""
# ruff: noqa: SLF001

import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import MagicMock

import grpc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from connect_openrmf_by_zenoh import KachakaApiClientByZenoh  # noqa: E402
from connect_openrmf_by_zenoh import MapState  # noqa: E402
from connect_openrmf_by_zenoh import Pose  # noqa: E402


class FakeRpcError(grpc.RpcError):
    """Minimal RpcError stand-in with the attributes the bridge reads."""

    def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.DEADLINE_EXCEEDED

    def details(self) -> str:
        return 'Deadline Exceeded'


def make_node(
    telemetry_map_name: str,
    robot_map_name: Optional[str] = None,
    grpc_error: bool = False,
) -> KachakaApiClientByZenoh:
    """Build a KachakaApiClientByZenoh without running __init__.

    Args:
        telemetry_map_name: The cached (possibly stale) map name.
        robot_map_name: The Kachaka-side name of the map the robot actually
            reports via gRPC. Ignored when grpc_error is True.
        grpc_error: If True, all gRPC stub calls raise FakeRpcError.

    Returns:
        A node instance with mocked gRPC stub and Zenoh publishers.
    """
    node = object.__new__(KachakaApiClientByZenoh)
    node.logger = logging.getLogger('test_map_state_guard')
    node.robot_name = 'test_robot'
    node.map_state = MapState.initial().with_telemetry_map_name(telemetry_map_name)
    # Kachaka-side names L8/L12 map to RMF-side names 8F/12F
    node.reverse_map_name_mapping = {'L8': '8F', 'L12': '12F'}
    node.map_name_mapping = {'8F': 'L8', '12F': 'L12'}
    node.grpc_status_check_timeout = 0.1
    node.grpc_telemetry_timeout = 0.1
    node.last_pose = Pose.zero()
    node.map_name_pub = MagicMock()
    node.pose_pub = MagicMock()
    node._publish_to_zenoh = MagicMock(return_value=True)
    node._publish_command_completion = MagicMock(return_value=True)

    stub = MagicMock()
    if grpc_error:
        stub.GetMapList.side_effect = FakeRpcError()
        stub.GetCurrentMapId.side_effect = FakeRpcError()
        stub.GetRobotPose.side_effect = FakeRpcError()
    else:
        entries = [
            SimpleNamespace(id='map-id-8f', name='L8'),
            SimpleNamespace(id='map-id-12f', name='L12'),
        ]
        current_id = next(e.id for e in entries if e.name == robot_map_name)
        stub.GetMapList.return_value = SimpleNamespace(map_list_entries=entries)
        stub.GetCurrentMapId.return_value = SimpleNamespace(id=current_id)
        stub.GetRobotPose.return_value = SimpleNamespace(pose=SimpleNamespace(x=1.0, y=2.0, theta=0.5))
    node.kachaka_client = MagicMock()
    node.kachaka_client.stub = stub
    return node


def published_completions(node: KachakaApiClientByZenoh) -> List[dict]:
    """Return the kwargs of all _publish_command_completion calls."""
    return [call.kwargs for call in node._publish_command_completion.call_args_list]


def test_guard_passes_without_grpc_when_cache_matches() -> None:
    """No re-query happens when the requested map matches the cache."""
    node = make_node(telemetry_map_name='8F', robot_map_name='L8')
    assert node._verify_map_for_navigation('8F') is True
    node.kachaka_client.stub.GetCurrentMapId.assert_not_called()
    node._publish_command_completion.assert_not_called()


def test_guard_self_heals_when_cache_is_stale() -> None:
    """A stale cache (12F) is corrected from the robot (8F) and navigation proceeds.

    This is the incident scenario: the robot is actually on the requested
    floor but the cached map name was left on the previous floor.
    """
    node = make_node(telemetry_map_name='12F', robot_map_name='L8')
    assert node._verify_map_for_navigation('8F') is True
    assert node.map_state.telemetry_map_name == '8F'
    node._publish_command_completion.assert_not_called()
    # The corrected map name is published so RMF stays consistent
    node._publish_to_zenoh.assert_called_once_with(node.map_name_pub, '8F')


def test_guard_rejects_genuine_mismatch() -> None:
    """Navigation is rejected when the robot really is on a different map."""
    node = make_node(telemetry_map_name='8F', robot_map_name='L12')
    assert node._verify_map_for_navigation('12F') is True  # sanity: robot is on 12F
    node = make_node(telemetry_map_name='12F', robot_map_name='L12')
    assert node._verify_map_for_navigation('8F') is False
    assert published_completions(node) == [{'success': False, 'error_code': -2, 'task_id': None}]


def test_guard_rejects_when_robot_unreachable() -> None:
    """Navigation is rejected (not allowed through) when gRPC fails."""
    node = make_node(telemetry_map_name='12F', grpc_error=True)
    assert node._verify_map_for_navigation('8F') is False
    assert published_completions(node) == [{'success': False, 'error_code': -2, 'task_id': None}]
    # Cache must not be corrupted on failure
    assert node.map_state.telemetry_map_name == '12F'


def test_recover_interrupted_switch_map_success() -> None:
    """A lost switch_map response is completed as success when the robot switched."""
    node = make_node(telemetry_map_name='12F', robot_map_name='L8')
    recovered = node._recover_interrupted_switch_map({'map_name': '8F', 'pose': {'x': 1.0, 'y': 2.0, 'theta': 0.5}})
    assert recovered is True
    assert node.map_state.telemetry_map_name == '8F'
    assert node.last_pose.as_list() == [1.0, 2.0, 0.5]
    assert published_completions(node) == [{'success': True, 'error_code': 0, 'task_id': None}]


def test_recover_interrupted_switch_map_not_switched() -> None:
    """No recovery happens when the robot never switched; caller reports failure."""
    node = make_node(telemetry_map_name='12F', robot_map_name='L12')
    assert node._recover_interrupted_switch_map({'map_name': '8F'}) is False
    node._publish_command_completion.assert_not_called()
    assert node.map_state.telemetry_map_name == '12F'


def test_recover_interrupted_switch_map_grpc_failure() -> None:
    """No recovery happens when the robot cannot be queried."""
    node = make_node(telemetry_map_name='12F', grpc_error=True)
    assert node._recover_interrupted_switch_map({'map_name': '8F'}) is False
    node._publish_command_completion.assert_not_called()
