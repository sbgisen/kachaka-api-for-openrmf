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
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional, Union

from google._upb._message import RepeatedCompositeContainer
from google.protobuf.json_format import MessageToDict
from grpc import RpcError
from grpc import StatusCode
import kachaka_api
import yaml
import zenoh


class KachakaApiClientByZenoh:
    """A client for the Kachaka API that publishes data to Zenoh.

    This class connects to a Kachaka API server and a Zenoh router,
    and provides methods to publish the robot's pose, current map name,
    and command state to Zenoh topics. It also subscribes to a command
    topic to receive and execute commands.
    """

    session: zenoh.Session
    pose_pub: zenoh.Publisher
    battery_pub: zenoh.Publisher
    map_name_pub: zenoh.Publisher
    command_is_completed_pub: zenoh.Publisher
    status_queryable: zenoh.Queryable
    command_querier: zenoh.Querier
    kachaka_client: Any  # kachaka_api.KachakaApiClient
    method_mapping: Dict[str, str]
    map_name_mapping: Dict[str, str]
    reverse_map_name_mapping: Dict[str, str]
    zenoh_config: Optional[str]
    robot_name: str
    task_id: Optional[str]
    last_command: Optional[Dict[str, Any]]
    last_command_result: Optional[Dict[str, Any]]
    last_command_id: Optional[str]
    command_check_interval: float
    logger: logging.Logger

    def __init__(
        self,
        zenoh_router: str,
        kachaka_access_point: Optional[str] = None,
        robot_name: str = 'kachaka',
        config_file: str = 'config.yaml',
    ) -> None:
        """Construct method.

        Args:
            zenoh_router (str): The address of the Zenoh router to connect to,
                in the format "ip:port".
            kachaka_access_point (str, optional): The URL of the Kachaka API server.
                Can be None when running internally on Kachaka.
            robot_name (str): The name of the robot, used in Zenoh topic names.
                Defaults to 'kachaka'.
            config_file (str): The name of the configuration file to load.
                Defaults to 'config.yaml'.
        """
        file_path = Path(__file__).resolve().parent
        config_path = file_path / '..' / 'config' if (file_path / '..' / 'config').exists() else file_path / 'config'
        with open(config_path / config_file, 'r') as f:
            config = yaml.safe_load(f)
        self.method_mapping = config.get('method_mapping', {})
        self.map_name_mapping = config.get('map_name_mapping', {})
        self.reverse_map_name_mapping = {v: k for k, v in self.map_name_mapping.items()}
        self.zenoh_config = config.get('zenoh_config', None)

        # Load timing and connection settings
        timeouts = config.get('timeouts', {})
        intervals = config.get('intervals', {})
        connection = config.get('connection', {})

        self.command_query_timeout = timeouts.get('command_query', 2.0)
        self.grpc_connection_sleep = timeouts.get('grpc_connection', 5)
        self.command_check_interval = intervals.get('command_check', 4.0)
        self.main_loop_sleep = intervals.get('main_loop', 1)
        self.max_retries = connection.get('max_retries', 20)
        self.max_consecutive_errors = connection.get('max_consecutive_errors', 10)
        self.kachaka_client = (kachaka_api.KachakaApiClient(kachaka_access_point)
                               if kachaka_access_point else kachaka_api.KachakaApiClient())
        self.robot_name = robot_name
        self.task_id = None
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            filename='kachaka_api.log',
        )
        self.logger = logging.getLogger(__name__)

        # Initialize Zenoh session and publishers in constructor
        self.session = zenoh.open(self._get_zenoh_config(zenoh_router))
        self.pose_pub = self.session.declare_publisher(f'robots/{self.robot_name}/pose')
        self.battery_pub = self.session.declare_publisher(f'robots/{self.robot_name}/battery')
        self.map_name_pub = self.session.declare_publisher(f'robots/{self.robot_name}/map_name')
        self.command_is_completed_pub = self.session.declare_publisher(
            f'robots/{self.robot_name}/command_is_completed')

        # Initialize queryable for request-reply pattern
        self.status_queryable = self.session.declare_queryable(f'robots/{self.robot_name}/status',
                                                               self._status_query_handler)

        # Initialize querier for fetching commands from fleet adapter
        self.command_querier = self.session.declare_querier(
            f'robots/{self.robot_name}/command',
            target=zenoh.QueryTarget.ALL,
            timeout=self.command_query_timeout,
        )

        self.logger.info(f'Initialized KachakaApiClientByZenoh for robot {robot_name}')
        self.last_pose = [0.0, 0.0, 0.0]
        self.last_battery = 100.0
        self.map_name = 'L1'
        self.last_command = None
        self.last_command_result = None
        self.last_command_id = None

    def _get_zenoh_config(self, zenoh_router: str) -> zenoh.Config:
        """Get Zenoh configuration with the provided router.

        Args:
            zenoh_router (str): The address of the Zenoh router to connect to,
                in the format "ip:port".

        Returns:
            zenoh.Config: A Zenoh configuration object.
        """
        conf = zenoh.Config.from_file(self.zenoh_config) if self.zenoh_config is not None else zenoh.Config()
        conf.insert_json5('connect/endpoints', json.dumps([f'tcp/{zenoh_router}']))
        return conf

    async def run_method(self, method_name: str, args: Dict[str, Any] = {}) -> Any:  # noqa: ANN401
        """Run a KachakaApiClient method with the provided arguments.

        Args:
            method_name (str): The name of the method to run.
            args (dict, optional): The arguments to pass to the method.
                Defaults to None.

        Returns:
            Any: The result of the method call, converted to a dictionary
                or list if it is a protobuf message.

        Raises:
            ConnectionError: If gRPC connection check fails after max retries
            RpcError: If any other gRPC error occurs
        """
        if not self.grpc_connection_check():
            error_msg = f'Failed to connect to Kachaka API server after max retries for method {method_name}'
            self.logger.error(error_msg)
            raise ConnectionError(error_msg)

        args = args or {}
        try:
            method = getattr(self.kachaka_client, method_name)
            response = self._to_dict(method(**args))
            return response
        except RpcError as e:
            self.logger.error(f'RPC error in {method_name}: {e.details()}')
            raise

    def _log_error(self, error_type: str, method_name: str, error: Exception) -> None:
        """Log an error with consistent formatting.

        Args:
            error_type (str): Type of error (Connection, RPC, etc.)
            method_name (str): Name of the method where the error occurred
            error (Exception): The exception object
        """
        error_msg = f'{error_type} error during {method_name}: {str(error)}'
        if isinstance(error, RpcError):
            error_msg = f'{error_type} error during {method_name}: {error.details()}'

        self.logger.error(error_msg)
        print(error_msg)

    def _publish_to_zenoh(self, publisher: zenoh.Publisher, data: Union[Dict, List, str, int, float, bool]) -> bool:
        """Publish data to a Zenoh topic with consistent encoding.

        Args:
            publisher: The Zenoh publisher to use
            data: The data to publish (will be JSON-encoded)
        """
        try:
            publisher.put(json.dumps(data).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
        except Exception as e:
            self.logger.error(f'Failed to publish data to Zenoh: {str(e)}')
            print(f'Failed to publish data to Zenoh: {str(e)}')
            return False
        return True

    async def publish_pose(self) -> None:
        """Publish the current robot pose to Zenoh.

        Gets the robot's current pose from Kachaka API and publishes it to Zenoh.
        The pose is formatted as a list [x, y, theta] where:
        - x, y: position coordinates in meters
        - theta: orientation in radians

        Handles connection errors and unexpected response formats.

        Raises:
            Does not raise exceptions as they are caught and logged internally.
        """
        method_name = 'publish_pose'
        try:
            pose_raw = await self.run_method('get_robot_pose')
            try:
                pose = [pose_raw['x'], pose_raw['y'], pose_raw['theta']]
                self.last_pose = pose
            except KeyError:
                # Handle unexpected format or missing data appropriately
                self.logger.error(f'{pose_raw} is unexpected response format')
                print(f'{pose_raw} is unexpected response format')
                return

            self._publish_to_zenoh(self.pose_pub, pose)
        except ConnectionError as e:
            self._log_error('Connection', method_name, e)
        except RpcError as e:
            self._log_error('RPC', method_name, e)
        except Exception as e:
            self._log_error('Unexpected', method_name, e)

    async def publish_battery(self) -> None:
        """Publish the current robot battery to Zenoh.

        Gets battery information from Kachaka API and publishes it to Zenoh.
        Battery level is normalized to a 0.0-1.0 range from the original percentage.

        Handles connection errors and unexpected response formats.

        Raises:
            Does not raise exceptions as they are caught and logged internally.
        """
        method_name = 'publish_battery'
        try:
            res = await self.run_method('get_battery_info')
            if isinstance(res, (list, tuple)) and len(res) > 0:
                battery = res[0] / 100.0
                self.last_battery = battery
            else:
                self.logger.error(f'Unexpected battery info format: {res}')
                print(f'Unexpected battery info format: {res}')
                return

            self._publish_to_zenoh(self.battery_pub, battery)
        except ConnectionError as e:
            self._log_error('Connection', method_name, e)
        except RpcError as e:
            self._log_error('RPC', method_name, e)
        except Exception as e:
            self._log_error('Unexpected', method_name, e)

    async def publish_map_name(self) -> None:
        """Publish the current map name to Zenoh.

        Gets the current map ID from Kachaka, looks up the map name
        from the map list, applies any name mapping defined in the configuration,
        and publishes the map name to Zenoh.

        Uses the reverse_map_name_mapping to convert Kachaka's internal map names
        (e.g., 'L27') to more descriptive names (e.g., '27F').

        Handles connection errors and unexpected response formats.

        Raises:
            Does not raise exceptions as they are caught and logged internally.
        """
        method_name = 'publish_map_name'
        try:
            map_list = await self.run_method('get_map_list')
            search_id = await self.run_method('get_current_map_id')

            kachaka_map_name = next((item['name'] for item in map_list if item['id'] == search_id), 'L1')
            map_name = self.reverse_map_name_mapping.get(kachaka_map_name, kachaka_map_name)
            self.map_name = map_name

            self._publish_to_zenoh(self.map_name_pub, map_name)
        except ConnectionError as e:
            self._log_error('Connection', method_name, e)
        except RpcError as e:
            self._log_error('RPC', method_name, e)
        except Exception as e:
            self._log_error('Unexpected', method_name, e)

    async def publish_result(self) -> None:
        """Publish the last command completion status to Zenoh.

        Checks the status of the most recently executed command and publishes
        its completion status (true/false) to Zenoh along with the task ID.

        Only publishes if there is an active task (task_id is set).
        The task_id is set when a command is received via the Zenoh command topic.

        The published result has the format:
        {
            "id": "<task_id>",
            "is_completed": true/false
        }

        Handles connection errors and unexpected response formats.

        Raises:
            Does not raise exceptions as they are caught and logged internally.
        """
        method_name = 'publish_result'
        try:
            if not self.task_id:
                # No active task to check
                return

            res = await self.run_method('get_command_state')
            result = {'id': self.task_id, 'is_completed': False}

            if isinstance(res, (list, tuple)) and len(res) > 0 and isinstance(res[0], int):
                result['is_completed'] = True if res[0] != 2 else False
            else:
                # Handle unexpected format or missing data appropriately
                self.logger.error(f'Unexpected command state format: {res}')
                print(f'Unexpected command state format: {res}')
                return

            self.logger.debug(f'Command {self.task_id} completed: {result["is_completed"]}')
            self.last_command_result = result
            put_result = self._publish_to_zenoh(self.command_is_completed_pub, result)
            # Reset task_id if the command is completed
            if put_result and result['is_completed']:
                self.task_id = None
        except ConnectionError as e:
            self._log_error('Connection', method_name, e)
        except RpcError as e:
            self._log_error('RPC', method_name, e)
        except Exception as e:
            self._log_error('Unexpected', method_name, e)

    def _to_dict(
        self, response: Union[dict, list, RepeatedCompositeContainer,
                              object]) -> Union[dict, list, RepeatedCompositeContainer]:
        """Convert a response object to a dictionary or list.

        Args:
            response (Union[dict, list, RepeatedCompositeContainer, object]): The response object to convert.

        Returns:
            Union[dict, list, RepeatedCompositeContainer]: The converted response object.
        """
        if response.__class__.__module__ == 'kachaka_api_pb2':
            return MessageToDict(response)
        if isinstance(response, (tuple, list, RepeatedCompositeContainer)):
            return [self._to_dict(item) for item in response]
        return response

    def _status_query_handler(self, query: zenoh.Query) -> None:
        """Handle queries for robot status.

        Args:
            query (zenoh.Query): The received query
        """

        try:
            status_data = {
                'robot_name': self.robot_name,
                'pose': self.last_pose,
                'map_name': self.map_name,
                'battery': self.last_battery,
                'command_is_completed': self.last_command_result if self.last_command_result else {},
            }
            reply_value = json.dumps(status_data).encode()
            query.reply(query.key_expr, reply_value, encoding=zenoh.Encoding.APPLICATION_JSON)
            self.logger.debug('Status query replied')
        except Exception as e:
            self.logger.error(f'Error handling status query: {str(e)}')

    async def periodic_command_check(self) -> None:
        """Periodically check for new commands from the command server using Query.

        This replaces the push-based subscriber model with a pull-based query model
        to improve reliability in case of temporary network disconnections.
        """
        self.logger.info('Starting periodic command check...')
        while True:
            try:
                # Query the command server for the latest command using Querier
                replies = self.command_querier.get()

                for reply in replies:
                    if reply.ok:
                        command = json.loads(reply.ok.payload.to_string())
                        command_id = command.get('id')

                        # Check if this is a new command
                        if command_id and command_id != self.last_command_id:
                            self.logger.info(f'Received new command: {command}')
                            print(f'New command: {command["method"]} (ID: {command_id})')

                            # Update last command ID to prevent duplicate processing
                            self.last_command_id = command_id

                            # Process the command using unified logic
                            try:
                                self._execute_command(command)
                            except Exception as e:
                                self.logger.error(f'Error processing command: {str(e)}')
                                print(f'Error processing command: {str(e)}')
                        else:
                            self.logger.debug(f'Skipping duplicate command ID: {command_id}')
                    else:
                        error_payload = (reply.err.payload.to_string()
                                         if reply.err and reply.err.payload else 'Unknown error')
                        if error_payload == 'Timeout':
                            self.logger.debug('Query timeout (normal if no new commands)')
                        else:
                            self.logger.warning(f'Reply error: {error_payload}')

            except Exception as e:
                self.logger.debug(f'Command check error or no new commands: {e}')

            # Wait before next check
            await asyncio.sleep(self.command_check_interval)

    def _execute_command(self, command: Dict[str, Any]) -> None:
        """Unified command execution logic."""
        method_name = 'execute_command'
        try:
            if not all(k in command for k in ('method', 'args')):
                raise ValueError('Invalid command structure')

            method_name = command['method']
            method_name = self.method_mapping.get(method_name, method_name)
            self.task_id = command.get('id', None)

            if not hasattr(self.kachaka_client, method_name):
                raise AttributeError(f'Invalid method: {method_name}')


            self.logger.info(f'Executing command: {method_name} (ID: {self.task_id})')
            print(f'Executing: {method_name}')
            self.last_command = command
            self.last_command_result = None

            # Execute the command
            if method_name == 'switch_map':
                args = command['args']
                self._execute_switch_map_sync(args)
            elif method_name == 'move_to_pose':
                args = command['args']
                map_name = args.pop('map_name', None)
                if map_name is not None and map_name != self.map_name:
                    self.logger.warning(f'Map name {map_name} does not match current map {self.map_name}')
                    return
                if 'cancel_all' not in args:
                    args['cancel_all'] = True
                if 'wait_for_completion' not in args:
                    args['wait_for_completion'] = False
                self.logger.debug(f'move_to_pose args: {args}')
                self._execute_sync_method(method_name, args)
            else:
                self._execute_sync_method(method_name, command['args'])
        except (json.JSONDecodeError, ValueError, AttributeError) as e:
            self.logger.error(f'Invalid command: {str(e)}')
            print(f'Invalid command: {str(e)}')
        except (ConnectionError, RpcError, Exception) as e:
            self._log_error('Unexpected', f'executing command {method_name}', e)

    def _execute_switch_map_sync(self, args: Dict[str, Any]) -> None:
        """Execute switch_map command synchronously.

        Args:
            args (dict): The arguments for the switch_map method, including:
                - map_name (str): The name of the map to switch to
                - pose (dict, optional): The initial pose on the new map
        """
        method_name = 'switch_map'
        try:
            if not self.grpc_connection_check():
                error_msg = f'Failed to connect to Kachaka API server for method {method_name}'
                self.logger.error(error_msg)
                raise ConnectionError(error_msg)

            map_name = self.map_name_mapping.get(args.get('map_name'), args.get('map_name'))
            map_list = self._to_dict(self.kachaka_client.get_map_list())
            map_id = next((item['id'] for item in map_list if item['name'] == map_name), None)

            if map_id is None:
                self.logger.error(f'Map {map_name} not found')
                print(f'Map {map_name} not found')
                return

            current_map_id = self.kachaka_client.get_current_map_id()
            payload = {'map_id': map_id, 'pose': args.get('pose', {'x': 0.0, 'y': 0.0, 'theta': 0.0})}

            # switch map only if the map is different from the current map id
            # because switch_map method takes long time to complete
            if map_id == current_map_id:
                self.logger.info('Nothing to do - already on target map')
            else:
                self._execute_sync_method('switch_map', payload)
                self.logger.info(f'Switched to map {map_name}', payload['pose'])
        except RpcError as e:
            self._log_error('RPC', method_name, e)
        except Exception as e:
            self._log_error('Unexpected', method_name, e)

    def _execute_sync_method(self, method_name: str, args: Dict[str, Any]) -> None:
        """Execute a method synchronously without asyncio.run().

        Args:
            method_name (str): The name of the method to execute
            args (Dict[str, Any]): The arguments for the method
        """
        try:
            if not self.grpc_connection_check():
                error_msg = f'Failed to connect to Kachaka API server for method {method_name}'
                self.logger.error(error_msg)
                raise ConnectionError(error_msg)

            method = getattr(self.kachaka_client, method_name)
            response = self._to_dict(method(**args))
            self.logger.info(f'Command {method_name} executed successfully')
            return response
        except RpcError as e:
            self.logger.error(f'RPC error in {method_name}: {e.details()}')
            raise
        except Exception as e:
            self.logger.error(f'Unexpected error in {method_name}: {str(e)}')
            raise

    def grpc_connection_check(self, max_retries: Optional[int] = None) -> bool:
        """Check if the gRPC connection to Kachaka API server is alive.

        Attempts to make a simple API call (get_robot_pose) to check if the
        connection is working. If the connection fails with UNAVAILABLE status,
        it will retry up to max_retries times with a sleep interval between retries.

        For other RPC errors, the error is re-raised as they indicate issues
        other than connection problems.

        Args:
            max_retries (int, optional): The maximum number of retries to check the connection.
                                       Uses config value if not specified.

        Returns:
            bool: True if the connection is alive, False if it could not be
                 established after max_retries

        Raises:
            RpcError: If an RPC error occurs that is not related to connection
                      availability (not StatusCode.UNAVAILABLE)
        """
        if max_retries is None:
            max_retries = self.max_retries
        sleep_time = self.grpc_connection_sleep
        retry_count = 0
        last_error = None

        for i in range(max_retries):
            try:
                self.kachaka_client.get_robot_pose()
                if retry_count > 0:
                    self.logger.info(f'gRPC connection restored after {retry_count} retries')
                    print(f'gRPC connection restored after {retry_count} retries')
                return True
            except RpcError as e:
                retry_count += 1
                last_error = e
                self.logger.info('Send Dummy data')
                self._publish_to_zenoh(self.pose_pub, self.last_pose)
                self._publish_to_zenoh(self.battery_pub, self.last_battery)
                self._publish_to_zenoh(self.map_name_pub, self.map_name)
                if e.code() == StatusCode.UNAVAILABLE:
                    self.logger.error(f'gRPC connection error ({retry_count}/{max_retries}): {e.details()}')
                    print(f'gRPC connection error ({retry_count}/{max_retries}): {e.details()}')
                    time.sleep(sleep_time)
                else:
                    self.logger.error(f'Unexpected gRPC error: {e.details()} (code: {e.code()})')
                    print(f'Unexpected gRPC error: {e.details()} (code: {e.code()})')
                    raise e

        error_details = last_error.details() if last_error else 'Unknown error'
        self.logger.error(
            f'Failed to connect to gRPC server after {max_retries} attempts. Last error: {error_details}')
        print(f'Failed to connect to gRPC server after {max_retries} attempts')
        return False


def _handle_main_loop_error(
    node: KachakaApiClientByZenoh,
    error: Exception,
    consecutive_errors: int,
    max_consecutive_errors: int,
) -> int:
    """Handle errors in the main loop.

    Args:
        node: The KachakaApiClientByZenoh node instance
        error: The exception that was raised
        consecutive_errors: Current count of consecutive errors
        max_consecutive_errors: Maximum allowed consecutive errors

    Returns:
        int: Updated consecutive_errors count

    Raises:
        Exception: If too many consecutive errors occur
    """
    consecutive_errors += 1
    if isinstance(error, RpcError) and error.code() == StatusCode.UNAVAILABLE:
        node.logger.error(f'Connection RPC error in main loop: {error.details()}')
    elif isinstance(error, ConnectionError):
        node.logger.error(f'Connection error in main loop: {str(error)}')
    elif isinstance(error, RpcError):
        node.logger.error(f'Unexpected RPC error in main loop: {error.details()}')
        raise error
    else:
        node.logger.error(f'Unexpected error in main loop: {str(error)}')

    if consecutive_errors >= max_consecutive_errors:
        node.logger.error(f'Too many consecutive errors ({consecutive_errors}), exiting')
        print(f'Too many consecutive errors ({consecutive_errors}), exiting')
        raise error

    return consecutive_errors


def main() -> None:
    """Run the main function to run the KachakaApiClientByZenoh.

    This function parses command-line arguments, creates an instance of
    KachakaApiClientByZenoh, subscribes to the command topic, and publishes
    the robot's pose, current map name, and command state to Zenoh in a loop.
    """
    zenoh_router_ap = os.getenv('ZENOH_ROUTER_ACCESS_POINT')
    kachaka_access_point = os.getenv('KACHAKA_ACCESS_POINT')
    robot_name = os.getenv('ROBOT_NAME', 'kachaka')
    config_file = os.getenv('CONFIG_FILE', 'config.yaml')
    if not zenoh_router_ap:
        raise ValueError('ZENOH_ROUTER_ACCESS_POINT must be set as an environment variable.')

    try:
        node = KachakaApiClientByZenoh(zenoh_router_ap, kachaka_access_point, robot_name, config_file)

        try:
            # Start the node with periodic command checking via Queryable pattern
            print('Starting KachakaApiClientByZenoh with periodic command checking...')
            node.logger.info('Starting KachakaApiClientByZenoh with periodic command checking...')

            consecutive_errors = 0
            max_consecutive_errors = node.max_consecutive_errors
            sleep_time = node.main_loop_sleep

            # Create a separate thread for command checking
            def run_command_check() -> None:
                asyncio.run(node.periodic_command_check())

            command_thread = threading.Thread(target=run_command_check, daemon=True)
            command_thread.start()

            while True:
                try:
                    asyncio.run(node.publish_pose())
                    asyncio.run(node.publish_battery())
                    asyncio.run(node.publish_map_name())
                    asyncio.run(node.publish_result())
                    consecutive_errors = 0  # Reset on success
                except (ConnectionError, RpcError, Exception) as e:
                    consecutive_errors = _handle_main_loop_error(node, e, consecutive_errors, max_consecutive_errors)

                time.sleep(sleep_time)

        except KeyboardInterrupt:
            print('Interrupted by user, cleaning up...')
        finally:
            # Clean up queryable and querier
            node.status_queryable.undeclare()
            node.command_querier.undeclare()
            # Clean up zenoh session
            node.session.delete(f'robots/{robot_name}/**')
            node.session.close()
            node.logger.info('Closed Zenoh session')
            node.logger.info('Exiting KachakaApiClientByZenoh')

    except Exception as e:
        print(f'Failed to initialize or run KachakaApiClientByZenoh: {str(e)}')
        logging.error(f'Failed to initialize or run KachakaApiClientByZenoh: {str(e)}')
        raise


if __name__ == '__main__':
    main()
