#!/bin/bash
set -e
# Exit when no arguments are provided.
if [ $# -eq 0 ]; then
  echo "Provide the IP address of the server."
  exit 1
fi
# Get argument and set as KACHAKA_IP.
KACHAKA_IP=$1
echo $KACHAKA_IP
# Create the server setup script with dynamic KACHAKA_IP
cat <<EOF > kachaka_startup.sh
#!/bin/bash
# LINES MODIFIED BY SBGISEN, SEE /home/kachaka/kachaka_startup.sh.backup FOR THE ORIGINAL FILE.
export KACHAKA_IP=$KACHAKA_IP
export KACHAKA_ACCESS_POINT=\$KACHAKA_IP:26400
export PATH=/home/kachaka/.local/bin:\$PATH
jupyter-lab --port=26501 --ip='0.0.0.0' & uvicorn sbgisen.rest_kachaka_api:app --host 0.0.0.0 --port 26502
EOF
# Securely copy the server setup script and any additional scripts to the server
ssh -p 26500 kachaka@$KACHAKA_IP "mkdir -p /home/kachaka/sbgisen"
scp -P 26500 kachaka_startup.sh kachaka@$KACHAKA_IP:~/
scp -P 26500 scripts/rest_kachaka_api.py kachaka@$KACHAKA_IP:~/sbgisen
rm kachaka_startup.sh
