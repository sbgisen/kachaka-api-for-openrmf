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

The script installs the Python packages the servers depend on (`kachaka-api`, `fastapi`, `uvicorn`, `eclipse-zenoh`) into the Playground, copies the scripts to `/home/kachaka/sbgisen`, and replaces `/home/kachaka/kachaka_startup.sh` so that the servers start at boot. A fresh Playground does not ship these packages, so this step is required even if you only use the REST API. The stock startup script is kept as `/home/kachaka/kachaka_startup.sh.backup`.

After rebooting the device, you shall get a response by running commands such as the following.

```bash
curl http://$KACHAKA_IP:26502/kachaka/get_robot_pose
# {"get_robot_pose":{"x":-0.27050725751488025,"y":0.06214801113488019,"theta":0.09612629786430153}}
curl http://$KACHAKA_IP:26502/kachaka/get_map_list
# {"get_map_list":[{"name":"1F","id":"XXX"},{"name":"2F","id":"YYY"}]}
```

If `curl` cannot connect, log in to the Playground (`ssh -p 26500 kachaka@$KACHAKA_IP`), check `/tmp/kachaka_startup.log`, and make sure `python3 -c "import kachaka_api, fastapi, uvicorn"` succeeds.

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
export LOG_LEVEL=INFO  # recommended default; use DEBUG only while investigating an issue, not for routine operation
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
  command_completion: 180.0 # Watchdog: fail a command with no RUNNING/completion for this long

intervals:
  command_check: 4.0        # Command polling interval in seconds
  main_loop: 1              # Main loop sleep time

connection:
  max_retries: 20           # Maximum gRPC connection retries
  max_consecutive_errors: 10 # Error threshold before exit
```

`timeouts.command_completion` defaults to 180.0 s. Keep it shorter than the adapter's completion wait (240 s by default), so the bridge's watchdog fails the command before the adapter gives up on it.

**Shelf Driver** - Selects how `shelf_dock` / `shelf_undock` are executed. There is no default; while it is unset, shelf commands fail with `reason=precheck_no_shelf_driver`:
```yaml
shelf_driver: pro             # pro | standard | dry_run
shelf_dry_run_duration: 0.0   # dry_run only: seconds before the simulated command succeeds
```

| `shelf_driver` | `shelf_dock` | `shelf_undock` |
|---|---|---|
| `pro` | `dock_any_shelf_with_registration(location, forward)` | `undock_shelf()` |
| `standard` | `dock_shelf()` | `undock_shelf()` |
| `dry_run` | no API call, simulated carrying = true | no API call, simulated carrying = false |

Command args: `shelf_dock` takes `{"location": str, "forward": bool (optional, default false)}`, `shelf_undock` takes `{"location": str}`. `location` is matched against the id or name of `get_locations()`. Failures carry a `reason`: `precheck_*` means no shelf API was called, `physical_*` means it failed after the API was issued. A failure with no `reason` (from existing paths such as a rejected `StartCommand`, supersede, or the watchdog) is treated by the adapter as physical state unknown.

`dry_run` simulates only the shelf API. Movement and other commands still reach the robot. A `cancel_command` is simulated (the robot's `cancel_command` is not called) only while a simulated shelf command is in progress; any other cancel is sent to the robot as usual.

Reasons a command can fail with, from this bridge (shelf commands and `move_to_pose`) and from the adapter. They are labels for diagnosis: robot_control cancels the task, stops the robot where it is and decommissions it on every failure (#90, agreed on 2026-11-06), so the reason does not change how a failure is handled.

| reason | Raised when |
|---|---|
| `precheck_no_shelf_driver` | `shelf_driver` is not set |
| `precheck_invalid_shelf_driver` | `shelf_driver` is not `pro` / `standard` / `dry_run` |
| `precheck_invalid_args` | `location` is not a non-empty string, or `forward` is not a bool |
| `precheck_location_unavailable` | `get_locations()` failed |
| `precheck_unknown_location` | `location` matches no Kachaka location |
| `precheck_not_in_front` | dock: not in front of the location; undock: carrying but not at the location |
| `precheck_already_carrying_other` | dock while carrying another shelf (or away from the location) |
| `precheck_unknown` | undock while empty, and the location has no shelf, or not the shelf this bridge last undocked |
| `precheck_state_unavailable` | the robot pose or the shelf state could not be read |
| `physical_command_failed` | Kachaka reported the shelf command as failed |
| `physical_shelf_not_docked` | dock reported success but `get_moving_shelf_id()` is empty |
| `physical_shelf_still_docked` | undock reported success but `get_moving_shelf_id()` is not empty |
| `physical_shelf_state_unknown` | `get_moving_shelf_id()` failed after the command finished |
| `physical_shelf_mismatch` | dock picked up another shelf than `get_shelves()` showed at the location |
| `external_busy` | rejected while a command started outside this bridge is running |
| `external_preempted` | preempted by a `returnHome` started outside this bridge |
| `start_timeout` | no RUNNING within `timeouts.command_start` |
| `progress_timeout` | no pose change within `timeouts.motion_progress` while RUNNING |
| `missing_command_id` | `StartCommand` returned no command id |
| `map_mismatch` | `move_to_pose` only: the robot is not on the requested map, or the map could not be read |
| `undock_endpoint_rejected` | `move_to_pose` only: the undock endpoint is too close to another charger or too far from every lane |
| `undock_failed` | `move_to_pose` only: Kachaka reported the undock move as failed |
| `undock_start_timeout` | `move_to_pose` only: no RUNNING of the undock move within `undock.timeouts.start` |
| `undock_progress_timeout` | `move_to_pose` only: no pose change of the undock move within `undock.timeouts.progress` |
| `undock_phase_timeout` | `move_to_pose` only: the undock phase exceeded `undock.timeouts.phase_total` |
| `physical_state_unknown` | set by the adapter, not this bridge: a failure that came without a `reason` |
| `adapter_completion_timeout` | set by the adapter, not this bridge: no completion within the adapter's wait limit |

**Shelf Precheck** - Before calling a shelf API the bridge checks the robot position and the carrying state (Issue #58). The tolerances are shared by all drivers and are placeholders until measured:
```yaml
shelf_precheck:
  front_distance_min: 0.3     # m, shelf_dock: robot-to-location distance range
  front_distance_max: 1.2
  front_yaw_tolerance: 0.35   # rad, shelf_dock: |bearing to the location - (robot yaw + offset)|
  front_yaw_offset: 0.0       # rad, set pi if the robot backs into the shelf
  at_location_tolerance: 0.3  # m, robot-to-location distance while carrying
  match_radius: 0.3           # m, a get_shelves() shelf this close is at the location
# shelf_dry_run_shelf_id: S2  # dry_run only: shelf a simulated dock picks up (reproduces physical_shelf_mismatch)
```

`front_yaw_offset` is a single global value and does not follow the `forward` argument of `shelf_dock`; mixing forward and backward approaches makes one of them fail with `precheck_not_in_front`.

| Case | Result |
|---|---|
| `shelf_dock`, empty, not in front of the location | `precheck_not_in_front` |
| `shelf_dock`, carrying the shelf this bridge docked at the location (or any shelf after a restart) and at the location | `success: true`, no API call |
| `shelf_dock`, carrying any other shelf | `precheck_already_carrying_other` |
| `shelf_dock` finished with another shelf than `get_shelves()` showed at the location | `physical_shelf_mismatch` |
| `shelf_undock`, carrying, not at the location | `precheck_not_in_front` |
| `shelf_undock`, empty, the shelf this bridge last undocked is at the location (or any shelf after a restart) | `success: true`, no API call |
| `shelf_undock`, empty, another shelf or no shelf at the location | `precheck_unknown` |
| robot pose / shelf state could not be read | `precheck_state_unavailable` |

In dry_run the robot does not move under the shelf, so before a dry_run `shelf_undock` the robot must be at the location (or `at_location_tolerance` must be raised).

The queryable-based architecture polls for commands every 4 seconds by default, providing better reliability during network disconnections compared to push-based subscribers.

#### Connecting to an mTLS router

When the Zenoh router requires mutual TLS, the robot needs its own certificate
and a Zenoh config file that points at it.

1. Issue a certificate for the robot with
   [certpaste](https://github.com/sbgisen/certpaste) on the CA host (the
   machine holding `ca.key`), then upload the result. certpaste does not need
   to be installed on the robot:

    ```bash
    # on the CA host; use one directory per robot
    D=~/zenoh-robots/<robot_name>
    # --san is required; the router does not check it, so the robot name is fine
    certpaste --dir $D request --san <robot_name> -o /tmp/<robot_name>.csr
    certpaste sign /tmp/<robot_name>.csr -o /tmp/<robot_name>.bundle
    certpaste --dir $D install /tmp/<robot_name>.bundle

    # upload ca.crt / peer.crt / peer.key from $D to the robot
    ssh -p 26500 kachaka@<kachaka_ip> mkdir -p /home/kachaka/sbgisen/certs
    scp -P 26500 $D/ca.crt $D/peer.crt $D/peer.key kachaka@<kachaka_ip>:/home/kachaka/sbgisen/certs/
    ```

   `peer.key` is the robot's private key. Delete the copy on the CA host
   after uploading it.

2. Copy `config/zenoh_client_mtls.json5.example` onto the robot, fix the
   certificate paths, and point `config/config.yaml` at it:

    ```yaml
    zenoh_config: "/home/kachaka/sbgisen/zenoh_client_mtls.json5"
    ```

3. Give the router address the `tls` scheme:

    ```bash
    export ZENOH_ROUTER_ACCESS_POINT=tls/192.168.1.1:7447
    ```

An address without a scheme (`192.168.1.1:7447`) keeps using plain TCP, so
existing setups are unaffected. Note that `verify_name_on_connect` makes the
robot check the dialed address against the router certificate's SAN — the
router must be enrolled with every address robots use to reach it.

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
