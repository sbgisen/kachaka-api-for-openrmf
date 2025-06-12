# Kachaka API for Open RMF

## Brief Description

This repository contains the source code for the Kachaka API server and Zenoh node script for integrating Kachaka robots with Open RMF. The Kachaka API server provides a REST API for interacting with the Kachaka API using HTTP requests. The Zenoh node script connects Kachaka robots to a Zenoh managed network, enabling communication between the robots and the Open RMF system.

## Features

- REST API server for interacting with the Kachaka API using HTTP requests
- Zenoh node script for connecting Kachaka robots to a certain Zenoh managed network
  - Publishing robot pose, current map name, and command state as Zenoh topics
  - Queryable-based command processing with periodic command polling for reliable operation
- Setup script for launching Kachaka REST API server and Zenoh node on the target device
- Local development environment for testing components on a local machine

<!-- - Integration of Kachaka API with Open RMF using Zenoh -->
<!-- - Web-based demo for remote control and monitoring of Kachaka robots -->

## Usage of Scripts

This repository includes two main scripts for different use cases:

1. `rest_kachaka_api.py`

- Use this script when communicating with an Open-RMF server within the same network without NAT (Network Address Translation) traversal.
- This script uses fleet_adapter_kachaka as the fleet adapter.
- It leverages FastAPI to expose the required topics for the fleet adapter.

1. `connect_openrmf_by_zenoh.py`:

- Use this script when communicating with an Open-RMF server via Zenoh router, requiring NAT traversal.
- It uses a common adapter that sends and receives Zenoh topics as the fleet adapter.
  Choose the appropriate script based on your network setup and communication requirements.

## Cloning the repository

```bash
git clone https://github.com/sbgisen/kachaka-api-for-openrmf.git
cd kachaka-api-for-openrmf
```

## Setup Kachaka internal components

Make sure to enable SSH login from your local environment to the target Kachaka device following [these steps](https://github.com/pf-robotics/kachaka-api?tab=readme-ov-file#playground%E3%81%ABssh%E3%81%A7%E3%83%AD%E3%82%B0%E3%82%A4%E3%83%B3%E3%81%99%E3%82%8B).

Run the following script to make the server launch during starting up the target device. Follow the prompt to configure Zenoh settings if using Zenoh framework.

```bash
export KACHAKA_IP=<ip_address_of_the_target_device>  # 192.168.128.30
./kachaka_server_setup.sh $KACHAKA_IP
```

After rebooting the device, you shall get a response by running commands such as the following.

```bash
curl http://$KACHAKA_IP:26502/kachaka/get_robot_pose
# {"get_robot_pose":{"x":-0.27050725751488025,"y":0.06214801113488019,"theta":0.09612629786430153}}
curl http://$KACHAKA_IP:26502/kachaka/get_map_list
# {"get_map_list":[{"name":"1F","id":"XXX"},{"name":"2F","id":"YYY"}]}
```

## Local development

Instructions for setting up a local development environment for testing the server.

### Common setup

Run the following lines to setup a pipenv environment for testing the server locally.

```bash
cd setup_local
./setup_local.sh  # Old pipenv settings will be removed every time this script is run.
```

Pipenv settings will be located at `~/kachaka_ws`.

### Launching the REST API server

Run the following lines to launch the REST API server.

```bash
cd ~/kachaka_ws
export KACHAKA_ACCESS_POINT=$KACHAKA_IP:26400
PYTHONPATH="/path/to/kachaka-api-for-openrmf/scripts:$PYTHONPATH" pipenv run uvicorn rest_kachaka_api:app --host 0.0.0.0 --port 26502
```

The server will start running on `http://localhost:26502`. You can then send HTTP requests to interact with the Kachaka API.

Example requests:

- `GET /kachaka/front_camera_image.jpeg`: Get the latest front camera image.
- `GET /kachaka/get_robot_pose`: Get the current robot pose.
- `POST /kachaka/move_to_location {"target_location_id": "location1"}`: Move the robot to a specific location.

### Launching Zenoh node script

Run the following lines to launch the Zenoh node for Kachaka.

```bash
cd ~/kachaka_ws
export KACHAKA_ACCESS_POINT=$KACHAKA_IP:26400
export ZENOH_ROUTER_ACCESS_POINT=<access_point_of_the_zenoh_router>  # e.g. 192.168.1.1:7447
pipenv run python /path/to/kachaka-api-for-openrmf/scripts/connect_openrmf_by_zenoh.py
```

The Zenoh node uses a queryable-based architecture where the robot periodically queries for commands. Fleet adapters should provide commands via Zenoh queryables on the `robots/<robot_name>/command` topic with the following JSON structure:

```json
{
  "id": "<unique_command_id>",
  "method": "<method_name>",
  "args": {
    "arg1": "value1",
    "arg2": "value2"
  }
}
```

#### Configuration

The script reads configuration from `config/config.yaml`. See the config file for detailed parameter descriptions and examples:

**Method Mapping** - Maps Open-RMF method names to Kachaka API methods:
```yaml
method_mapping:
  dock: return_home
  localize: switch_map
```

**Map Name Mapping** - Maps user-friendly map names to Kachaka internal names:
```yaml
map_name_mapping:
  27F: L27
  29F: L29
```

**Timing Settings** - Configurable timeouts and intervals:
```yaml
timeouts:
  command_query: 2.0        # Command query timeout in seconds
  grpc_connection: 5        # gRPC connection retry interval

intervals:
  command_check: 4.0        # Command polling interval in seconds
  main_loop: 1              # Main loop sleep time

connection:
  max_retries: 20           # Maximum gRPC connection retries
  max_consecutive_errors: 10 # Error threshold before exit
```

The queryable-based architecture polls for commands every 4 seconds by default, providing better reliability during network disconnections compared to push-based subscribers.

## Testing

### Command Testing Script

The repository includes a test script for validating Kachaka robot commands via Zenoh:

```bash
python test/test_kachaka_command.py <zenoh_router> <robot_name> <command> [args...]
```

#### Supported Commands

**switch_map** - Switch to a different map:
```bash
# Switch to map 27F with default pose (0, 0, 0)
python test/test_kachaka_command.py 127.0.0.1:7447 kachaka switch_map 27F

# Switch to map L27 with custom pose
python test/test_kachaka_command.py 127.0.0.1:7447 kachaka switch_map L27 1.0 2.0 0.5
```

**move_to_pose** - Move robot to specific coordinates:
```bash
# Move to coordinates (1.5, 2.0, 0.0) on current map
python test/test_kachaka_command.py 127.0.0.1:7447 kachaka move_to_pose 1.5 2.0 0.0

# Move to coordinates with specific map
python test/test_kachaka_command.py 127.0.0.1:7447 kachaka move_to_pose 1.5 2.0 0.0 27F
```

**dock** - Return robot to charging dock:
```bash
python test/test_kachaka_command.py 127.0.0.1:7447 kachaka dock
```

#### How it works

1. The test script creates a Zenoh queryable that simulates a fleet adapter
2. When the robot queries for commands (every 4 seconds), the script responds with the specified command
3. The robot executes the command and reports status via Zenoh topics
4. Use Ctrl+C to stop the test script

#### Prerequisites

- Zenoh router must be running and accessible
- `connect_openrmf_by_zenoh.py` must be running on the robot
- Robot must be connected to the same Zenoh network

## License

This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for more details.

## Acknowledgements

We would like to express our gratitude to the Open RMF community for their valuable contributions and support in making this integration possible.
