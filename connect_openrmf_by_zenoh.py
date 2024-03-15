#!/usr/bin/env pipenv-shebang
# -*- encoding: utf-8 -*-

# Copyright (c) 2024 SoftBank Corp.
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

import argparse
import asyncio
import json
import time

import kachaka_api
import zenoh
from google._upb._message import RepeatedCompositeContainer
from google.protobuf.json_format import MessageToDict
from zenoh import Sample


class KachakaApiClientByZenoh:
    def __init__(self, zenoh_router: str, kachaka_access_point: str, robot_name: str) -> None:
        self.kachaka_client = kachaka_api.KachakaApiClient(
            kachaka_access_point)
        self.robot_name = robot_name
        self.zenoh_conf = zenoh.Config()
        self.zenoh_conf.insert_json5(
            zenoh.config.CONNECT_KEY, json.dumps([f"tcp/{zenoh_router}"]))
        self.session = zenoh.open(self.zenoh_conf)
        self.pose_pub = self.session.declare_publisher(
            f"kachaka/{self.robot_name}/pose")
        self.map_name_pub = self.session.declare_publisher(
            f"kachaka/{self.robot_name}/map_name")
        self.command_result_pub = self.session.declare_publisher(
            f"kachaka/{self.robot_name}/command_result")

    def to_dict(self, response: dict | list | RepeatedCompositeContainer
                | object) -> dict | list | RepeatedCompositeContainer:
        if response.__class__.__module__ == "kachaka_api_pb2":
            return MessageToDict(response)
        if (
            isinstance(response, tuple)
            or isinstance(response, list)
            or isinstance(response, RepeatedCompositeContainer)
        ):
            return [self.to_dict(r) for r in response]
        return response

    async def run_method(self, method_name: str, args: dict = {}) -> dict | list | RepeatedCompositeContainer:
        if len(args) == 0:
            args = {}
        method = getattr(self.kachaka_client, method_name)
        response = method(**args)
        response = self.to_dict(response)
        return response

    async def publish_pose(self) -> None:
        pose = await self.run_method("get_robot_pose")
        self.pose_pub.put(pose)

    async def publish_map_name(self) -> None:
        map_list = await self.run_method("get_map_list")
        search_id = await self.run_method("get_current_map_id")
        map_name = next(
            (item for item in map_list if item["id"] == search_id), {'name': "L1"})
        self.map_name_pub.put(map_name["name"])

    async def publish_result(self) -> None:
        result = await self.run_method("get_last_command_result")
        self.command_result_pub.put(result)

    def command_callback(self, sample: Sample) -> None:
        try:
            command = json.loads(sample.payload.decode('utf-8'))
        except json.JSONDecodeError:
            print(
                f"Invalid command: {sample.payload.decode('utf-8')} is not a valid JSON string")
            return
        # check command has 'method' and 'args' keys
        if not all(k in command for k in ('method', 'args')):
            print("Invalid command")
            return
        if not hasattr(self.kachaka_client, command['method']):
            print("Invalid command")
            return
        print(f"Received command: {command}")
        asyncio.run(self.run_method(command['method'], command['args']))

    def subscribe_command(self) -> zenoh.Subscriber:
        key = f"kachaka/{self.robot_name}/command"
        sub = self.session.declare_subscriber(key, self.command_callback)
        return sub


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--zenoh_router_ip", "-zi", help="Zenoh router IP", default="192.168.1.1")
    parser.add_argument(
        "--zenoh_router_port", "-zp", help="Zenoh router port", default="7447")
    parser.add_argument(
        "--kachaka_access_point", "-ka", help="Kachaka access point", default="")
    parser.add_argument("--robot_name", "-rn",
                        help="Robot name", default="robot")
    zenoh_router = f"{parser.parse_args().zenoh_router_ip}:{parser.parse_args().zenoh_router_port}"
    kachaka_access_point = parser.parse_args().kachaka_access_point
    robot_name = parser.parse_args().robot_name
    node = KachakaApiClientByZenoh(
        zenoh_router, kachaka_access_point, robot_name)
    try:
        sub = node.subscribe_command()
        print(f"Subscribed to {sub}")
        while (True):
            asyncio.run(node.publish_pose())
            asyncio.run(node.publish_map_name())
            asyncio.run(node.publish_result())
            time.sleep(1)
    except KeyboardInterrupt:
        node.session.delete(f"kachaka/{robot_name}/**")
        node.session.close()


if __name__ == "__main__":
    main()
