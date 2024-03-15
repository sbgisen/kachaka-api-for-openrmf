# kachaka-zenoh

## Installation

```
pipenv install
pipenv run pip install eclipse-zenoh
pipenv run python -m grpc_tools.protoc -I kachaka-api/protos --python_out=. --grpc_python_out=. kachaka-api/protos/kachaka-api.proto 
```
