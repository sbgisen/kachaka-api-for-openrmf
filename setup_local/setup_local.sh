#!/bin/bash
set -e
echo "Setting up local environment..."
# Set execution directory as variable.
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
sudo apt install python3-pip
pip install --upgrade pip
pip install pipenv
mkdir -p ~/kachaka_ws && cd ~/kachaka_ws
# Clone repository when it does not exist.
if [ ! -d "kachaka-api" ]; then
    git clone https://github.com/pf-robotics/kachaka-api.git
else
    # Optionally, pull the latest changes if the repository already exists.
    cd kachaka-api && git pull && cd ..
fi
cp $DIR/Pipfile Pipfile
cp $DIR/Pipfile.lock Pipfile.lock
# Remove existing Pipenv environment if it exists.
if pipenv --venv 2> /dev/null; then
   echo "Removing existing Pipenv environment..."
   pipenv --rm
   pipenv --clear
fi
pipenv install
pipenv run python -m grpc_tools.protoc -I kachaka-api/protos --python_out=. --grpc_python_out=. kachaka-api/protos/kachaka-api.proto
echo "pipenv successfully installed. Run 'pipenv shell' to enter the virtual environment."
