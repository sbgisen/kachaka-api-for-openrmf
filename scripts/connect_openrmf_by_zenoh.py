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
from typing import Any
from typing import Union

import kachaka_api
import zenoh
from google._upb._message import RepeatedCompositeContainer
from google.protobuf.json_format import MessageToDict
from zenoh import Sample


class KachakaApiClientByZenoh:
    """A client for the Kachaka API that publishes data to Zenoh.
    This class connects to a Kachaka API server and a Zenoh router,
    and provides methods to publish the robot's pose, current map name,
    and command state to Zenoh topics. It also subscribes to a command
    topic to receive and execute commands.
    Args:
        zenoh_router (str): The address of the Zenoh router to connect to,
            in the format "ip:port".
        kachaka_access_point (str): The URL of the Kachaka API server.
        robot_name (str): The name of the robot, used in Zenoh topic names.
    """

    def __init__(self, zenoh_router: str, kachaka_access_point: str, robot_name: str) -> None:
        self.kachaka_client = kachaka_api.KachakaApiClient(kachaka_access_point)
        self.robot_name = robot_name

        # Initialize Zenoh session and publishers in constructor
        self.session = zenoh.open(self._get_zenoh_config(zenoh_router))
        self.pose_pub = self.session.declare_publisher(f"kachaka/{self.robot_name}/pose")
        self.map_name_pub = self.session.declare_publisher(f"kachaka/{self.robot_name}/map_name")
        self.command_state_pub = self.session.declare_publisher(f"kachaka/{self.robot_name}/command_state")

    def _get_zenoh_config(self, zenoh_router: str) -> zenoh.Config:
        """Get Zenoh configuration with the provided router.
        Args:
            zenoh_router (str): The address of the Zenoh router to connect to,
                in the format "ip:port".
        Returns:
            zenoh.Config: A Zenoh configuration object.
        """
        conf = zenoh.Config()
        conf.insert_json5(zenoh.config.CONNECT_KEY, json.dumps([f"tcp/{zenoh_router}"]))
        return conf

    async def run_method(self, method_name: str, args: dict = {}) -> Any:
        """Run a KachakaApiClient method with the provided arguments.
        Args:
            method_name (str): The name of the method to run.
            args (dict, optional): The arguments to pass to the method.
                Defaults to None.
        Returns:
            Any: The result of the method call, converted to a dictionary
                or list if it is a protobuf message.
        """
        args = args or {}
        method = getattr(self.kachaka_client, method_name)
        response = self._to_dict(method(**args))
        return response

    async def publish_pose(self) -> None:
        """Publish the current robot pose to Zenoh."""
        pose = await self.run_method("get_robot_pose")
        self.pose_pub.put(pose)

    async def publish_map_name(self) -> None:
        """Publish the current map name to Zenoh."""
        map_list = await self.run_method("get_map_list")
        search_id = await self.run_method("get_current_map_id")
        map_name = next((item["name"] for item in map_list if item["id"] == search_id), "L1")
        self.map_name_pub.put(map_name)

    async def publish_result(self) -> None:
        """Publish the last command state to Zenoh."""
        result = await self.run_method("get_command_state")
        self.command_state_pub.put(result)

    def _to_dict(self,
                 response: Union[dict, list, RepeatedCompositeContainer, object]
                 ) -> Union[dict, list, RepeatedCompositeContainer]:
        """Convert a response object to a dictionary or list.
        Args:
            response (Union[dict, list, RepeatedCompositeContainer, object]):
                The response object to convert.
        Returns:
            Union[dict, list, RepeatedCompositeContainer]: The converted
                response object.
        """
        if response.__class__.__module__ == "kachaka_api_pb2":
            return MessageToDict(response)
        if isinstance(response, (tuple, list, RepeatedCompositeContainer)):
            return [self._to_dict(item) for item in response]
        return response

    def _command_callback(self, sample: Sample) -> None:
        """Handle received command samples.
        This method is called whenever a command is received on the subscribed
        Zenoh topic. It parses the command JSON, validates it, and runs the
        specified method with the provided arguments.
        Args:
            sample (Sample): The received Zenoh sample containing the command.
        Raises:
            ValueError: If the command structure is invalid.
            AttributeError: If the specified method does not exist on the
                KachakaApiClient.
            json.JSONDecodeError: If the command payload is not valid JSON.
        """
        try:
            command = json.loads(sample.payload.decode('utf-8'))
            if not all(k in command for k in ('method', 'args')):
                raise ValueError("Invalid command structure")
            method_name = command['method']
            if not hasattr(self.kachaka_client, method_name):
                raise AttributeError(f"Invalid method: {method_name}")
            print(f"Received command: {command}")
            asyncio.run(self.run_method(method_name, command['args']))
        except (json.JSONDecodeError, ValueError, AttributeError) as e:
            print(f"Invalid command: {str(e)}")

    def subscribe_command(self) -> zenoh.Subscriber:
        """Subscribe to the command topic.
        Returns:
            zenoh.Subscriber: The Zenoh subscriber object.
        """
        return self.session.declare_subscriber(
            f"kachaka/{self.robot_name}/command", self._command_callback)


def main() -> None:
    """The main function to run the KachakaApiClientByZenoh.
    This function parses command-line arguments, creates an instance of
    KachakaApiClientByZenoh, subscribes to the command topic, and publishes
    the robot's pose, current map name, and command state to Zenoh in a loop.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--zenoh_router_ip", "-zi", default="192.168.1.1", help="Zenoh router IP")
    parser.add_argument("--zenoh_router_port", "-zp", default="7447", help="Zenoh router port")
    parser.add_argument("--kachaka_access_point", "-ka", default="", help="Kachaka access point")
    parser.add_argument("--robot_name", "-rn", default="robot", help="Robot name")
    args = parser.parse_args()

    zenoh_router = f"{args.zenoh_router_ip}:{args.zenoh_router_port}"

    node = KachakaApiClientByZenoh(zenoh_router, args.kachaka_access_point, args.robot_name)
    try:
        sub = node.subscribe_command()
        print(f"Subscribed to {sub}")
        while True:
            asyncio.run(node.publish_pose())
            asyncio.run(node.publish_map_name())
            asyncio.run(node.publish_result())
            time.sleep(1)
    except KeyboardInterrupt:
        node.session.delete(f"kachaka/{args.robot_name}/**")
        node.session.close()


if __name__ == "__main__":
    main()
