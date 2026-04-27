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
"""Unit tests for KachakaApiClientWithKeepalive."""

from pathlib import Path
import sys

import grpc
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from kachaka_client_with_keepalive import KachakaApiClientWithKeepalive  # noqa: E402


def test_keepalive_options_present() -> None:
    """KEEPALIVE_OPTIONS includes the expected gRPC channel arguments."""
    keys = {opt[0] for opt in KachakaApiClientWithKeepalive.KEEPALIVE_OPTIONS}
    assert 'grpc.keepalive_time_ms' in keys
    assert 'grpc.keepalive_timeout_ms' in keys
    assert 'grpc.keepalive_permit_without_calls' in keys


def test_init_creates_channel_and_stub() -> None:
    """Constructor builds a channel that can be closed without errors."""
    client = KachakaApiClientWithKeepalive('127.0.0.1:1')
    channel = client._channel  # noqa: SLF001
    assert channel is not None
    assert isinstance(channel, grpc.Channel)
    assert client.stub is not None
    client.close()


def test_close_is_idempotent() -> None:
    """close() can be called multiple times without raising."""
    client = KachakaApiClientWithKeepalive('127.0.0.1:1')
    client.close()
    client.close()


def test_invalid_target_raises() -> None:
    """An unresolvable target string surfaces as ValueError."""
    with pytest.raises(ValueError):
        KachakaApiClientWithKeepalive('not-a-valid-target-string')
