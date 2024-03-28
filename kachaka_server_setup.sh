#!/bin/bash
set -e
# Exit when no arguments are provided.
if [ $# -eq 0 ]; then
  echo "Provide the IP address of the server."
  exit 1
fi
# Function for cleaning up temporary files
cleanup() {
  echo "Cleaning up temporary files..."
  rm -f kachaka_startup.sh
}
trap cleanup EXIT
# Get argument and set as KACHAKA_IP.
KACHAKA_IP=$1
SSH_PORT=26500
RUN_LINE="jupyter-lab --port=26501 --ip='0.0.0.0' & uvicorn sbgisen.rest_kachaka_api:app --host 0.0.0.0 --port 26502"
# Ask whether to set up Zenoh client. If yes, ask the router access point and the robot name.
read -p "Do you want to set up the Zenoh client? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
  read -p "Enter the Zenoh router access point: " ZENOH_ROUTER_ACCESS_POINT
  read -p "Enter the robot name: " ROBOT_NAME
  RUN_LINE="$RUN_LINE & python3 sbgisen/connect_openrmf_by_zenoh.py"
fi
# Create the server setup script with dynamic KACHAKA_IP
cat <<EOF > kachaka_startup.sh
#!/bin/bash
# LINES MODIFIED BY SBGISEN, SEE /home/kachaka/kachaka_startup.sh.backup FOR THE ORIGINAL FILE.
export KACHAKA_IP=$KACHAKA_IP
export KACHAKA_ACCESS_POINT=\$KACHAKA_IP:26400
export ZENOH_ROUTER_ACCESS_POINT=$ZENOH_ROUTER_ACCESS_POINT
export ROBOT_NAME=$ROBOT_NAME
export PATH=/home/kachaka/.local/bin:\$PATH
$RUN_LINE
EOF

ssh -p $SSH_PORT kachaka@$KACHAKA_IP <<EOF
pip install eclipse-zenoh
mkdir -p /home/kachaka/sbgisen
EOF

# Securely copy the server setup script and any additional scripts to the server.
scp -P $SSH_PORT kachaka_startup.sh kachaka@$KACHAKA_IP:~/
scp -P $SSH_PORT scripts/*.py kachaka@$KACHAKA_IP:~/sbgisen
echo "Setup script and additional scripts have been copied to the server."
