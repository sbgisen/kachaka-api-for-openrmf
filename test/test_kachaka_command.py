#!/usr/bin/env python3

# Copyright (c) 2025 SoftBank Corp.
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
"""Generic test script for Kachaka commands via Zenoh.

This script publishes various commands to the Zenoh network
and can be used to test different Kachaka functionalities.
"""

import json
import sys
import time
from typing import Any, Dict, Optional

import zenoh


def create_switch_map_command(robot_name: str,
                              map_name: str,
                              pose: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Create a switch_map command.

    Args:
        robot_name: Name of the robot
        map_name: Name of the map to switch to
        pose: Optional pose with x, y, theta. Defaults to origin.

    Returns:
        Command dictionary
    """
    if pose is None:
        pose = {'x': 0.0, 'y': 0.0, 'theta': 0.0}

    return {
        'id': f'test_switch_map_{int(time.time())}',
        'method': 'switch_map',
        'args': {
            'map_name': map_name,
            'pose': pose
        }
    }


def create_move_to_pose_command(robot_name: str,
                                x: float,
                                y: float,
                                theta: float,
                                map_name: Optional[str] = None) -> Dict[str, Any]:
    """Create a move_to_pose command.

    Args:
        robot_name: Name of the robot
        x: Target x coordinate
        y: Target y coordinate
        theta: Target orientation in radians
        map_name: Optional map name (if different from current)

    Returns:
        Command dictionary
    """
    args = {'x': x, 'y': y, 'theta': theta}

    if map_name:
        args['map_name'] = map_name

    return {'id': f'test_move_to_pose_{int(time.time())}', 'method': 'move_to_pose', 'args': args}


def create_dock_command(robot_name: str) -> Dict[str, Any]:
    """Create a dock command.

    Args:
        robot_name: Name of the robot

    Returns:
        Command dictionary
    """
    return {'id': f'test_dock_{int(time.time())}', 'method': 'dock', 'args': {}}


def publish_command_via_queryable(zenoh_router: str, robot_name: str, command: Dict[str, Any]) -> None:
    """Publish command via Zenoh queryable.

    Args:
        zenoh_router: Zenoh router address (e.g., "127.0.0.1:7447")
        robot_name: Name of the robot
        command: Command to publish
    """
    # Configure Zenoh session
    conf = zenoh.Config()
    conf.insert_json5('connect/endpoints', json.dumps([f'tcp/{zenoh_router}']))

    session = zenoh.open(conf)

    try:
        # Declare queryable for fleet adapter simulation
        def command_handler(query: zenoh.Query) -> None:
            print(f'🔍 Received query from robot: {query.key_expr}')
            # Reply with the command
            reply_payload = json.dumps(command).encode()
            query.reply(query.key_expr, reply_payload, encoding=zenoh.Encoding.APPLICATION_JSON)
            print(f'📤 Sent command: {command}')

        queryable_key = f'robots/{robot_name}/command'
        queryable = session.declare_queryable(queryable_key, command_handler)

        print(f'✅ Queryable declared on key: {queryable_key}')
        print(f'📋 Ready to send command: {json.dumps(command, indent=2)}')
        print('⏳ Waiting for robot to query for commands...')
        print('   (Robot queries every 4 seconds)')
        print('   Press Ctrl+C to stop')

        # Keep the script running
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print('\n🛑 Stopping...')
    finally:
        queryable.undeclare()
        session.close()
        print('✅ Cleaned up and closed Zenoh session')


def print_usage() -> None:
    """Print usage information."""
    print('Usage: python test_kachaka_command.py <zenoh_router> <robot_name> <command> [args...]')
    print()
    print('Commands:')
    print('  switch_map <map_name> [x] [y] [theta]')
    print('    Example: python test_kachaka_command.py 127.0.0.1:7447 kachaka switch_map 27F')
    print('    Example: python test_kachaka_command.py 127.0.0.1:7447 kachaka switch_map L27 1.0 2.0 0.5')
    print()
    print('  move_to_pose <x> <y> <theta> [map_name]')
    print('    Example: python test_kachaka_command.py 127.0.0.1:7447 kachaka move_to_pose 1.5 2.0 0.0')
    print('    Example: python test_kachaka_command.py 127.0.0.1:7447 kachaka move_to_pose 1.5 2.0 0.0 27F')
    print()
    print('  dock')
    print('    Example: python test_kachaka_command.py 127.0.0.1:7447 kachaka dock')


def main() -> None:
    """Execute the main function."""
    if len(sys.argv) < 4:
        print_usage()
        sys.exit(1)

    zenoh_router = sys.argv[1]
    robot_name = sys.argv[2]
    command_type = sys.argv[3]

    command = None

    try:
        if command_type == 'switch_map':
            if len(sys.argv) < 5:
                print('❌ switch_map requires map_name')
                print_usage()
                sys.exit(1)

            map_name = sys.argv[4]
            pose = None

            if len(sys.argv) >= 8:
                pose = {'x': float(sys.argv[5]), 'y': float(sys.argv[6]), 'theta': float(sys.argv[7])}
                print(f'Using custom pose: {pose}')

            command = create_switch_map_command(robot_name, map_name, pose)

        elif command_type == 'move_to_pose':
            if len(sys.argv) < 7:
                print('❌ move_to_pose requires x, y, theta')
                print_usage()
                sys.exit(1)

            x = float(sys.argv[4])
            y = float(sys.argv[5])
            theta = float(sys.argv[6])
            map_name = sys.argv[7] if len(sys.argv) > 7 else None

            command = create_move_to_pose_command(robot_name, x, y, theta, map_name)

        elif command_type == 'dock':
            command = create_dock_command(robot_name)

        else:
            print(f'❌ Unknown command: {command_type}')
            print_usage()
            sys.exit(1)

    except ValueError as e:
        print(f'❌ Invalid argument: {e}')
        print_usage()
        sys.exit(1)

    print(f'🚀 Testing {command_type} command:')
    print(f'   Zenoh Router: {zenoh_router}')
    print(f'   Robot: {robot_name}')
    print()

    publish_command_via_queryable(zenoh_router, robot_name, command)


if __name__ == '__main__':
    main()
