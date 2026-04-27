#!/usr/bin/env pipenv-shebang
# -*- encoding: utf-8 -*-

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

import grpc
import kachaka_api
from kachaka_api.base import _resolve_target
from kachaka_api.base import ShelfLocationResolver
from kachaka_api.generated.kachaka_api_pb2_grpc import KachakaApiStub


class KachakaApiClientWithKeepalive(kachaka_api.KachakaApiClient):
    """KachakaApiClient with HTTP/2 keepalive enabled for half-open detection.

    The upstream library opens an unconfigured grpc.insecure_channel, leaving
    no protocol-level liveness check. A persistently unresponsive server then
    keeps producing DEADLINE_EXCEEDED rather than UNAVAILABLE because the
    client never realizes the channel is dead.

    This subclass replaces parent initialization to own the channel directly,
    enabling explicit close during reconstruction and HTTP/2 PINGs that mark
    the channel BROKEN within keepalive_timeout_ms when ACKs stop arriving.
    """

    KEEPALIVE_OPTIONS = [
        ('grpc.keepalive_time_ms', 30000),
        ('grpc.keepalive_timeout_ms', 10000),
        ('grpc.keepalive_permit_without_calls', 1),
        ('grpc.http2.max_pings_without_data', 0),
    ]

    def __init__(self, target: str = '100.94.1.1:26400') -> None:
        target_resolved = _resolve_target(target)
        if target_resolved is None:
            raise ValueError(f'Invalid target: {target}')
        self._channel = grpc.insecure_channel(target_resolved, options=self.KEEPALIVE_OPTIONS)
        self.stub = KachakaApiStub(self._channel)
        self.resolver = ShelfLocationResolver()

    def close(self) -> None:
        """Close the underlying gRPC channel. Idempotent."""
        try:
            self._channel.close()
        except Exception:
            pass
