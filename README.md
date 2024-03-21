# Kachaka-Zenoh Integration

## Brief Description
Kachaka-Zenoh Integration is a powerful software solution designed to bridge the gap between the Kachaka API and Zenoh networking layer, facilitating seamless communication and data exchange in robotics applications. This project enables efficient, real-time robot management by publishing crucial robot data to Zenoh topics and subscribing to commands intended for robotic control.

## Features
- **Real-Time Data Publishing**: Publishes robot's pose, current map name, and command state to specific Zenoh topics.
- **Command Subscription**: Listens for and executes commands received on a Zenoh topic designated for robot control.
- **Asynchronous Support**: Utilizes asyncio for non-blocking network communications, ensuring responsive robot operations.
- **Protobuf Integration**: Uses Protocol Buffers for efficient data serialization, catering to robust and scalable applications.
- **Easy Configuration**: Offers straightforward setup with customizable options for Zenoh router connections and Kachaka API endpoints.

## Installation Instructions
1. **Clone the Repository**: Start by cloning the repository to your local machine.
    ```
    git clone https://github.com/your-repository/kachaka-zenoh.git
    ```
2. **Install Dependencies**: Navigate to the cloned directory and install the necessary Python dependencies.
    ```
    cd kachaka-zenoh
    pipenv install
    pipenv run pip install eclipse-zenoh
    pipenv run python -m grpc_tools.protoc -I kachaka-api/protos --python_out=. --grpc_python_out=. kachaka-api/protos/kachaka-api.proto 
    ```

## Usage Examples
To run the Kachaka-Zenoh Integration script and start publishing data to Zenoh while listening for commands, use the following command:

```
pipenv run python connect_openrmf_by_zenoh.py --zenoh_router_ip <router_ip> --zenoh_router_port <router_port> --kachaka_access_point <api_endpoint> --robot_name <name>
```
Replace `<router_ip>`, `<router_port>`, `<api_endpoint>`, and `<name>` with your Zenoh router's IP and port, the Kachaka API endpoint, and the name of your robot, respectively.

If you want to show `help`, use the following command:

```
pipenv run python connect_openrmf_by_zenoh.py -h
```

## Configuration Options
- **Zenoh Router IP and Port**: Specify the IP address and port of your Zenoh router using `--zenoh_router_ip` and `--zenoh_router_port`.
- **Kachaka Access Point**: Define the Kachaka API server URL with `--kachaka_access_point`.
- **Robot Name**: Set a unique name for your robot using `--robot_name`, which will be used in Zenoh topic names.

## License
This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## Acknowledgements/Credits
Special thanks to all contributors and the robotics community for their valuable insights and feedback that have greatly shaped this project.
