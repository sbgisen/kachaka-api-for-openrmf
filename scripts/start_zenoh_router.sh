#!/bin/bash
# Copyright (c) 2026 SoftBank Corp.
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
#
# Start a Zenoh router using Docker for local development and review testing.
#
# Usage:
#   ./scripts/start_zenoh_router.sh [--port PORT] [--stop] [--help]
#
# Examples:
#   ./scripts/start_zenoh_router.sh                  # Start on default port 7447
#   ./scripts/start_zenoh_router.sh --port 7448      # Start on custom port
#   ./scripts/start_zenoh_router.sh --stop           # Stop the running router

set -e

CONTAINER_NAME='kachaka-zenoh-router'
ZENOH_IMAGE='eclipse/zenoh:latest'
DEFAULT_PORT=7447

usage() {
  echo 'Usage: ./scripts/start_zenoh_router.sh [OPTIONS]'
  echo ''
  echo 'Start a Zenoh router for Kachaka API development and review testing.'
  echo ''
  echo 'Options:'
  echo '  --port PORT   Zenoh router port (default: 7447)'
  echo '  --stop        Stop the running Zenoh router container'
  echo '  --help        Show this help message'
  echo ''
  echo 'After starting, connect Kachaka robot with:'
  echo "  ZENOH_ROUTER_ACCESS_POINT=<this-host-ip>:${DEFAULT_PORT}"
  echo ''
  echo 'Run test commands:'
  echo '  python test/test_kachaka_command.py <this-host-ip>:7447 <robot_name> move_to_pose 1.0 0.0 0.0'
  echo '  python test/test_kachaka_command.py <this-host-ip>:7447 <robot_name> dock'
  echo '  python test/test_kachaka_command.py <this-host-ip>:7447 <robot_name> switch_map 27F'
}

stop_router() {
  if docker ps -q --filter "name=${CONTAINER_NAME}" | grep -q .; then
    echo "Stopping Zenoh router container: ${CONTAINER_NAME}"
    docker stop "${CONTAINER_NAME}"
    echo 'Zenoh router stopped.'
  else
    echo 'Zenoh router is not running.'
  fi
}

PORT=${DEFAULT_PORT}

while [[ $# -gt 0 ]]; do
  case $1 in
    --port)
      PORT="$2"
      shift 2
      ;;
    --stop)
      stop_router
      exit 0
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

if ! command -v docker &> /dev/null; then
  echo 'Error: Docker is not installed or not in PATH.'
  echo 'Install Docker: https://docs.docker.com/get-docker/'
  exit 1
fi

# Stop existing container if running
if docker ps -q --filter "name=${CONTAINER_NAME}" | grep -q .; then
  echo "Stopping existing Zenoh router container..."
  docker stop "${CONTAINER_NAME}" > /dev/null
fi

# Remove existing container if it exists
if docker ps -aq --filter "name=${CONTAINER_NAME}" | grep -q .; then
  docker rm "${CONTAINER_NAME}" > /dev/null
fi

HOST_IP=$(hostname -I | awk '{print $1}')

echo "Starting Zenoh router on port ${PORT}..."
docker run -d \
  --name "${CONTAINER_NAME}" \
  --network host \
  "${ZENOH_IMAGE}" \
  --listen "tcp/0.0.0.0:${PORT}"

echo ''
echo 'Zenoh router started successfully.'
echo ''
echo "  Router address for Kachaka robot: ${HOST_IP}:${PORT}"
echo ''
echo 'Next steps:'
echo "  1. Run kachaka_server_setup.sh with ZENOH_ROUTER_ACCESS_POINT=${HOST_IP}:${PORT}"
echo "  2. Test with: python test/test_kachaka_command.py ${HOST_IP}:${PORT} <robot_name> <command>"
echo ''
echo "To stop: ./scripts/start_zenoh_router.sh --stop"
