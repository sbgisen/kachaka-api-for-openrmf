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

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from typing import Union

import kachaka_api
import yaml
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
    """

    def __init__(self, zenoh_router: str, kachaka_access_point: str, robot_name: str, config_file: str) -> None:
        """Constructor method.
        Args:
            zenoh_router (str): The address of the Zenoh router to connect to,
                in the format "ip:port".
            kachaka_access_point (str): The URL of the Kachaka API server.
            robot_name (str): The name of the robot, used in Zenoh topic names.
        """

        file_path = Path(__file__).resolve().parent.parent
        with open(file_path / "config" / config_file, 'r') as f:
            config = yaml.safe_load(f)
        self.method_mapping = config.get('method_mapping', {})
        self.kachaka_client = kachaka_api.KachakaApiClient(kachaka_access_point)
        self.robot_name = robot_name
        self.task_id = None

        # Initialize Zenoh session and publishers in constructor
        self.session = zenoh.open(self._get_zenoh_config(zenoh_router))
        self.pose_pub = self.session.declare_publisher(f"robots/{self.robot_name}/pose")
        self.battery_pub = self.session.declare_publisher(f"robots/{self.robot_name}/battery")
        self.map_name_pub = self.session.declare_publisher(f"robots/{self.robot_name}/map_name")
        self.command_is_completed_pub = self.session.declare_publisher(
            f"robots/{self.robot_name}/command_is_completed")

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

    async def run_method(self, method_name: str, args: dict = {}) -> Any:  # noqa: ANN401
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
        pose_raw = await self.run_method("get_robot_pose")
        try:
            pose = [pose_raw["x"], pose_raw["y"], pose_raw["theta"]]
        except KeyError:
            # Handle unexpected format or missing data appropriately
            print(f"{pose_raw} is unexpected response format")
            return
        self.pose_pub.put(json.dumps(pose).encode(), encoding=zenoh.Encoding.APP_JSON())

    async def publish_battery(self) -> None:
        """Publish the current robot battery to Zenoh."""
        res = await self.run_method("get_battery_info")
        if isinstance(res, (list, tuple)) and len(res) > 0:
            battery = res[0] / 100.0
        self.battery_pub.put(json.dumps(battery).encode(), encoding=zenoh.Encoding.APP_JSON())

    async def publish_map_name(self) -> None:
        """Publish the current map name to Zenoh."""
        map_list = await self.run_method("get_map_list")
        search_id = await self.run_method("get_current_map_id")
        map_name = next((item["name"] for item in map_list if item["id"] == search_id), "L1")
        self.map_name_pub.put(json.dumps(map_name).encode(), encoding=zenoh.Encoding.APP_JSON())

    async def switch_map(self, args: dict) -> None:
        """Switch the robot to the specified map.
        Args:
            args (dict): The arguments for the switch_map method.
        """
        map_list = await self.run_method("get_map_list")
        map_id = next((item["id"] for item in map_list if item["name"] == args.get('map_name')), None)
        if map_id is not None:
            payload = {"map_id": map_id, "pose": args.get("pose", {"x": 0.0, "y": 0.0, "theta": 0.0})}
            await self.run_method("switch_map", payload)
        else:
            print(f"Map {args.get('map_name')} not found")

    async def publish_result(self) -> None:
        """Publish the last command is_completed to Zenoh."""
        res = await self.run_method("get_command_state")
        result = {"id": self.task_id, "is_completed": False}
        if isinstance(res, (list, tuple)) and len(res) > 0 and isinstance(res[0], int):
            result["is_completed"] = True if res[0] == 1 else False
        else:
            # Handle unexpected format or missing data appropriately
            raise ValueError(f"{res} is unexpected response format")
        self.command_is_completed_pub.put(json.dumps(result).encode(), encoding=zenoh.Encoding.APP_JSON())

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
            method_name = self.method_mapping.get(method_name, method_name)
            self.task_id = command.get('id', None)
            if not hasattr(self.kachaka_client, method_name):
                raise AttributeError(f"Invalid method: {method_name}")
            print(f"Received command: {command}")
            if method_name == "switch_map":
                asyncio.run(self.switch_map(command['args']))
            else:
                asyncio.run(self.run_method(method_name, command['args']))
        except (json.JSONDecodeError, ValueError, AttributeError) as e:
            print(f"Invalid command: {str(e)}")

    def subscribe_command(self) -> zenoh.Subscriber:
        """Subscribe to the command topic.
        Returns:
            zenoh.Subscriber: The Zenoh subscriber object.
        """
        return self.session.declare_subscriber(
            f"robots/{self.robot_name}/command", self._command_callback)


def main() -> None:
    """The main function to run the KachakaApiClientByZenoh.
    This function parses command-line arguments, creates an instance of
    KachakaApiClientByZenoh, subscribes to the command topic, and publishes
    the robot's pose, current map name, and command state to Zenoh in a loop.
    """
    zenoh_router_ap = os.getenv('ZENOH_ROUTER_ACCESS_POINT')
    kachaka_access_point = os.getenv('KACHAKA_ACCESS_POINT')
    robot_name = os.getenv('ROBOT_NAME', 'kachaka')
    config_file = os.getenv('CONFIG_FILE', 'config.yaml')
    if not zenoh_router_ap or not kachaka_access_point:
        raise ValueError("ZENOH_ROUTER_ACCESS_POINT and KACHAKA_ACCESS_POINT must be set as environment variables.")

    node = KachakaApiClientByZenoh(zenoh_router_ap, kachaka_access_point, robot_name, config_file)
    try:
        sub = node.subscribe_command()
        print(f"Subscribed to {sub}")
        while True:
            asyncio.run(node.publish_pose())
            asyncio.run(node.publish_battery())
            asyncio.run(node.publish_map_name())
            asyncio.run(node.publish_result())
            time.sleep(1)
    except KeyboardInterrupt:
        node.session.delete(f"robots/{robot_name}/**")
        node.session.close()


if __name__ == "__main__":
    main()
