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
"""Unit tests for the Zenoh connect endpoint built by KachakaApiClientByZenoh."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from connect_openrmf_by_zenoh import KachakaApiClientByZenoh  # noqa: E402

TLS_CONFIG = """{
  mode: "client",
  transport: { link: { tls: { enable_mtls: true, verify_name_on_connect: true } } },
}
"""


def _endpoints(zenoh_router: str, zenoh_config: str = None) -> list:
    """Build a config the way the client does and read back its endpoints."""
    client = SimpleNamespace(zenoh_config=zenoh_config)
    conf = KachakaApiClientByZenoh._get_zenoh_config(client, zenoh_router)  # noqa: SLF001
    return json.loads(conf.get_json('connect/endpoints'))


@pytest.mark.parametrize(('zenoh_router', 'expected'), [
    ('192.168.1.100:7447', 'tcp/192.168.1.100:7447'),
    ('tls/192.168.1.100:7447', 'tls/192.168.1.100:7447'),
])
def test_scheme_defaults_to_tcp_and_is_kept_when_given(zenoh_router: str, expected: str) -> None:
    """A bare "ip:port" stays TCP; an explicit scheme reaches Zenoh untouched."""
    assert _endpoints(zenoh_router) == [expected]


def test_custom_config_keeps_its_tls_settings(tmp_path: Path) -> None:
    """The endpoint override must not discard the mTLS block of a custom config."""
    config_file = tmp_path / 'zenoh_client_mtls.json5'
    config_file.write_text(TLS_CONFIG)

    client = SimpleNamespace(zenoh_config=str(config_file))
    conf = KachakaApiClientByZenoh._get_zenoh_config(client, 'tls/192.168.1.100:7447')  # noqa: SLF001

    assert json.loads(conf.get_json('connect/endpoints')) == ['tls/192.168.1.100:7447']
    assert json.loads(conf.get_json('transport/link/tls/enable_mtls')) is True
