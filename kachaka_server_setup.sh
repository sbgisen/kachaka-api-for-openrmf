#!/bin/bash
set -e
# Exit when no arguments are provided.
if [ $# -eq 0 ]; then
  echo "Provide the IP address of the server."
  exit 1
fi
# Function for cleaning up temporary files.
cleanup() {
  echo "Cleaning up temporary files..."
  rm -f kachaka_startup.sh
}
# Clean up temporary files on exit.
trap cleanup EXIT
# Get argument and set as KACHAKA_IP.
KACHAKA_IP=$1
SSH_PORT=26500

timeout=3  # Timeout in seconds
if ! nc -z -w $timeout $KACHAKA_IP $SSH_PORT; then
    echo "SSH port on $KACHAKA_IP:$SSH_PORT is not accessible, configure the server to allow SSH connections on port."
    echo "See https://github.com/pf-robotics/kachaka-api?tab=readme-ov-file#playground%E3%81%ABssh%E3%81%A7%E3%83%AD%E3%82%B0%E3%82%A4%E3%83%B3%E3%81%99%E3%82%8B"
    exit 1
fi
RUN_ZENOH=""
# Ask whether to set up Zenoh client. If yes, ask the router access point and the robot name.
read -p "Do you want to set up the Zenoh client? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
  read -p "Enter the Zenoh router access point (IP:Port, e.g., 192.168.1.100:7447): " ZENOH_ROUTER_ACCESS_POINT
  read -p "Enter the robot name: " ROBOT_NAME
  read -p "Enter log level (DEBUG/INFO/WARNING/ERROR/CRITICAL) [INFO]: " LOG_LEVEL
  LOG_LEVEL=${LOG_LEVEL:-INFO}
  RUN_ZENOH=1
fi
# Create the server setup script with dynamic KACHAKA_IP.
# REST API (uvicorn) is launched by default; set DISABLE_REST_API=1 in the
# Kachaka environment to skip it (useful for OpenRMF-only deployments and
# for diagnosing gRPC server load issues).
cat <<EOF > kachaka_startup.sh
#!/bin/bash
# LINES MODIFIED BY SBGISEN, SEE /home/kachaka/kachaka_startup.sh.backup FOR THE ORIGINAL FILE.
export KACHAKA_IP=$KACHAKA_IP
# When running on the Kachaka robot internally, KACHAKA_ACCESS_POINT is optional
if [ -n "$KACHAKA_IP" ]; then
  export KACHAKA_ACCESS_POINT=\$KACHAKA_IP:26400
fi
export ZENOH_ROUTER_ACCESS_POINT=$ZENOH_ROUTER_ACCESS_POINT
export ROBOT_NAME=$ROBOT_NAME
export LOG_LEVEL=$LOG_LEVEL
export PATH=/home/kachaka/.local/bin:\$PATH
jupyter-lab --port=26501 --ip='0.0.0.0' &
if [ "\${DISABLE_REST_API:-0}" != "1" ]; then
  uvicorn sbgisen.rest_kachaka_api:app --host 0.0.0.0 --port 26502 &
fi
${RUN_ZENOH:+python3 sbgisen/connect_openrmf_by_zenoh.py &}
# Exit when any child process dies so the supervisor can restart the whole stack
# (matches the original foreground-bridge behaviour where bridge death exited the script).
trap 'kill 0' EXIT
wait -n
EOF

ssh -p $SSH_PORT kachaka@$KACHAKA_IP <<EOF
pip install eclipse-zenoh
mkdir -p /home/kachaka/sbgisen
EOF

# Securely copy the server setup script and any additional scripts to the server.
scp -P $SSH_PORT kachaka_startup.sh kachaka@$KACHAKA_IP:~/
scp -P $SSH_PORT scripts/*.py kachaka@$KACHAKA_IP:~/sbgisen
scp -r -P $SSH_PORT config/ kachaka@$KACHAKA_IP:~/sbgisen
echo "Setup script and additional scripts have been copied to the server."
