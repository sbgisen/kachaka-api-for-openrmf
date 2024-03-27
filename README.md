# Kachaka API for Open RMF

## Brief Description
Kachaka API for Open RMF is a software integration that connects the Kachaka API with the Open RMF (Robotics Middleware Framework) using Zenoh, a data-centric communication protocol. This project enables seamless communication and control of Kachaka robots within the Open RMF ecosystem.

## Features
- Integration of Kachaka API with Open RMF using Zenoh
- Publishing of robot pose, current map name, and command state to Zenoh topics
- Subscription to command topic for receiving and executing robot commands
- Asynchronous communication using asyncio for responsive robot control
- Easy configuration of Zenoh router and Kachaka API endpoints
- REST API for interacting with the Kachaka API using HTTP requests
- Web-based demo for remote control and monitoring of Kachaka robots

## Installation Instructions
1. Clone the repository:
   ```bash
   git clone https://github.com/sbgisen/kachaka-api-for-openrmf.git --recursive
   ```

2. Navigate to the project directory:
   ```bash
   cd kachaka-api-for-openrmf
   ```

3. Install the required dependencies using Pipenv:
   ```bash
   pipenv install
   ```

4. Generate Python code from the Kachaka API protobuf file:
   ```bash
   pipenv run python -m grpc_tools.protoc -I kachaka-api/protos --python_out=. --grpc_python_out=. kachaka-api/protos/kachaka-api.proto
   ```

## Connect to Zenoh

### Usage Examples
To run the Kachaka API for Open RMF integration script and start publishing data to Zenoh while listening for commands, use the following command:

```
pipenv run python scripts/connect_openrmf_by_zenoh.py --zenoh_router_ip <router_ip> --zenoh_router_port <router_port> --kachaka_access_point <api_endpoint> --robot_name <name>
```

Replace `<router_ip>`, `<router_port>`, `<api_endpoint>`, and `<name>` with your Zenoh router's IP and port, the Kachaka API endpoint, and the name of your robot, respectively.

To display the help message and available options, use the following command:

```
pipenv run python scripts/connect_openrmf_by_zenoh.py -h
```

### Sending Commands
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

### Configuration Options
- `--zenoh_router_ip`: Specify the IP address of your Zenoh router (default: "192.168.1.1").
- `--zenoh_router_port`: Specify the port of your Zenoh router (default: "7447").
- `--kachaka_access_point`: Define the Kachaka API server URL (default: "").
- `--robot_name`: Set a unique name for your robot (default: "robot").

## REST API

### Usage Examples
To start the REST API server, use the following command:

```
pipenv run python scripts/rest_kachaka_api.py --kachaka_access_point <api_endpoint>
```

Replace `<api_endpoint>` with your Kachaka API endpoint.

### REST API Server
The server will start running on `http://localhost:26502`. You can then send HTTP requests to interact with the Kachaka API.

Example requests:
- `GET /kachaka/front_camera_image.jpeg`: Get the latest front camera image.
- `GET /kachaka/get_robot_pose`: Get the current robot pose.
- `POST /kachaka/move_to_location {"target_location_id": "location1"}`: Move the robot to a specific location.

### Configuration Options
- `--kachaka_access_point`: Define the Kachaka API server URL (default: "localhost:26400").

## License
This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for more details.

## Acknowledgements
We would like to express our gratitude to the Open RMF community for their valuable contributions and support in making this integration possible.
