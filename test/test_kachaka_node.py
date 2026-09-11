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
"""Tests for KachakaApiClientByZenoh.

Test categories:
  - Pure logic tests: no external dependencies, runnable in CI.
  - Integration tests: require KACHAKA_ACCESS_POINT and
    ZENOH_ROUTER_ACCESS_POINT environment variables (real robot on LAN).

Running integration tests:
  export KACHAKA_ACCESS_POINT=192.168.128.30:26400
  export ZENOH_ROUTER_ACCESS_POINT=192.168.1.100:7447
  pytest test/test_kachaka_node.py -v

Running only pure logic tests (CI):
  pytest test/test_kachaka_node.py -v -m "not integration"
"""

import asyncio
import os
from pathlib import Path
import sys
from typing import Generator
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

from connect_openrmf_by_zenoh import KachakaApiClientByZenoh  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_node() -> KachakaApiClientByZenoh:
    """KachakaApiClientByZenoh with all external connections mocked.

    Use this fixture for pure logic tests that do not require a real robot
    or Zenoh router.
    """
    mock_session = MagicMock()
    mock_session.declare_publisher.return_value = MagicMock()
    mock_session.declare_queryable.return_value = MagicMock()
    mock_session.declare_querier.return_value = MagicMock()
    with (
            patch('zenoh.open', return_value=mock_session),
            patch('kachaka_api.KachakaApiClient', return_value=MagicMock()),
    ):
        node = KachakaApiClientByZenoh(
            zenoh_router='127.0.0.1:7447',
            kachaka_access_point='127.0.0.1:26400',
        )
    return node


@pytest.fixture
def real_node() -> Generator[KachakaApiClientByZenoh, None, None]:
    """KachakaApiClientByZenoh connected to a real Kachaka robot on the LAN.

    Requires the following environment variables:
      KACHAKA_ACCESS_POINT      e.g. 192.168.128.30:26400
      ZENOH_ROUTER_ACCESS_POINT e.g. 192.168.1.100:7447

    Start the Zenoh router with:
      ./scripts/start_zenoh_router.sh
    """
    kachaka_ap = os.environ.get('KACHAKA_ACCESS_POINT')
    zenoh_router = os.environ.get('ZENOH_ROUTER_ACCESS_POINT')
    if not kachaka_ap or not zenoh_router:
        pytest.skip('Set KACHAKA_ACCESS_POINT and ZENOH_ROUTER_ACCESS_POINT to run integration tests')
    node = KachakaApiClientByZenoh(zenoh_router=zenoh_router, kachaka_access_point=kachaka_ap)
    yield node
    node.session.close()


# ─────────────────────────────────────────────────────────────────────────────
# Pure logic tests (no external dependencies)
# ─────────────────────────────────────────────────────────────────────────────


class TestIsRunningState:
    """Tests for _is_running_state().

    Verifies that both string and int representations of the RUNNING state
    are correctly identified.
    """

    def test_running_string(self, mock_node: KachakaApiClientByZenoh) -> None:
        assert mock_node._is_running_state('COMMAND_STATE_RUNNING') is True  # noqa: SLF001

    def test_running_string_case_insensitive(self, mock_node: KachakaApiClientByZenoh) -> None:
        assert mock_node._is_running_state('command_state_running') is True  # noqa: SLF001

    def test_non_running_string(self, mock_node: KachakaApiClientByZenoh) -> None:
        assert mock_node._is_running_state('COMMAND_STATE_SUCCEEDED') is False  # noqa: SLF001

    def test_none_returns_false(self, mock_node: KachakaApiClientByZenoh) -> None:
        assert mock_node._is_running_state(None) is False  # noqa: SLF001

    def test_empty_string_returns_false(self, mock_node: KachakaApiClientByZenoh) -> None:
        assert mock_node._is_running_state('') is False  # noqa: SLF001


class TestPrepareAsyncCommandArgs:
    """Tests for _prepare_async_command_args().

    Verifies argument preparation for async StartCommand calls:
    wait_for_completion defaults to False and is_async_command flag is updated.
    """

    def test_sets_wait_for_completion_false_by_default(self, mock_node: KachakaApiClientByZenoh) -> None:
        result = mock_node._prepare_async_command_args({})  # noqa: SLF001
        assert result['wait_for_completion'] is False
        assert mock_node.is_async_command is True

    def test_preserves_explicit_wait_for_completion_true(self, mock_node: KachakaApiClientByZenoh) -> None:
        result = mock_node._prepare_async_command_args({'wait_for_completion': True})  # noqa: SLF001
        assert result['wait_for_completion'] is True
        assert mock_node.is_async_command is False

    def test_applies_defaults_when_key_missing(self, mock_node: KachakaApiClientByZenoh) -> None:
        result = mock_node._prepare_async_command_args({}, defaults={'speed': 1.0})  # noqa: SLF001
        assert result['speed'] == 1.0

    def test_does_not_override_existing_key_with_default(self, mock_node: KachakaApiClientByZenoh) -> None:
        result = mock_node._prepare_async_command_args({'speed': 0.5}, defaults={'speed': 1.0})  # noqa: SLF001
        assert result['speed'] == 0.5

    def test_does_not_mutate_original_args(self, mock_node: KachakaApiClientByZenoh) -> None:
        original = {'x': 1.0}
        mock_node._prepare_async_command_args(original)  # noqa: SLF001
        assert 'wait_for_completion' not in original

    def test_none_args_returns_dict_with_wait_for_completion(self, mock_node: KachakaApiClientByZenoh) -> None:
        result = mock_node._prepare_async_command_args(None)  # noqa: SLF001
        assert 'wait_for_completion' in result


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests (real Kachaka + Zenoh router required)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestExecuteCommandTracking:
    """Integration tests for command_id-based tracking in _execute_command().

    These tests require a real Kachaka robot accessible on the LAN.
    Start the Zenoh router with ./scripts/start_zenoh_router.sh before running.
    """

    def test_wrong_map_returns_error_code_minus_2(self, real_node: KachakaApiClientByZenoh) -> None:
        """_execute_command with an unknown map_name must set error_code=-2 immediately."""
        command = {
            'id': 'test_wrong_map',
            'method': 'move_to_pose',
            'args': {
                'x': 0.0,
                'y': 0.0,
                'yaw': 0.0,
                'map_name': 'NON_EXISTENT_MAP_FOR_TESTING',
            },
        }
        real_node._execute_command(command)  # noqa: SLF001
        assert real_node.last_command_result is not None
        assert real_node.last_command_result.get('error_code') == -2

    def test_command_id_set_after_async_command(self, real_node: KachakaApiClientByZenoh) -> None:
        """current_command_id must be populated after move_to_pose is dispatched."""
        command = {
            'id': 'test_cmd_id_tracking',
            'method': 'move_to_pose',
            'args': {
                'x': 0.0,
                'y': 0.0,
                'yaw': 0.0
            },
        }
        real_node._execute_command(command)  # noqa: SLF001
        assert real_node.current_command_id is not None

    def test_stale_command_id_does_not_trigger_completion(self, real_node: KachakaApiClientByZenoh) -> None:
        """Results for a stale command_id must not be published as completion.

        Sends a command then overwrites current_command_id with a fake value.
        publish_result must not mark the stale command as completed.
        """
        command = {
            'id': 'test_stale_cmd',
            'method': 'move_to_pose',
            'args': {
                'x': 0.0,
                'y': 0.0,
                'yaw': 0.0
            },
        }
        real_node._execute_command(command)  # noqa: SLF001
        real_node.current_command_id = 'stale_id_that_will_never_match'
        asyncio.run(real_node.publish_result())
        result = real_node.last_command_result
        if result is not None:
            assert result.get('id') != 'stale_id_that_will_never_match' or result.get('is_completed') is False
