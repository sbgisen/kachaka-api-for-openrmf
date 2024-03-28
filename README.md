# Kachaka API for Open RMF

## Brief Description

This repository contains the source code for the Kachaka API server and Zenoh node script for integrating Kachaka robots with Open RMF. The Kachaka API server provides a REST API for interacting with the Kachaka API using HTTP requests. The Zenoh node script connects Kachaka robots to a Zenoh managed network, enabling communication between the robots and the Open RMF system.

## Features

- REST API server for interacting with the Kachaka API using HTTP requests
- Zenoh node script for connecting Kachaka robots to a certain Zenoh managed network
  - Publishing robot pose, current map name, and command state as Zenoh topics
  - Subscription to command topic for receiving and executing robot commands
- Setup script for launching Kachaka REST API server and Zenoh node on the target device
- Local development environment for testing components on a local machine

<!-- - Integration of Kachaka API with Open RMF using Zenoh -->
<!-- - Web-based demo for remote control and monitoring of Kachaka robots -->

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
PYTHONPATH="/home/gisen/work/kachaka-api-for-openrmf/scripts:$PYTHONPATH" pipenv run uvicorn rest_kachaka_api:app --host 0.0.0.0 --port 26502
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

To send commands to the robot via Zenoh, publish a JSON-formatted message to the `kachaka/<robot_name>/command` topic with the following structure:

```json
{
  "method": "<method_name>",
  "args": {
    "arg1": "value1",
    "arg2": "value2"
  }
}
```

## License

This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for more details.

## Acknowledgements

We would like to express our gratitude to the Open RMF community for their valuable contributions and support in making this integration possible.
