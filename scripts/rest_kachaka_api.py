#!/usr/bin/env pipenv-shebang
# -*- encoding: utf-8 -*-

# Copyright (c) 2024 SoftBank Corp.
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

import io
import os
from typing import Any
from typing import Dict
from typing import Union

import kachaka_api
from fastapi import BackgroundTasks
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from google._upb._message import RepeatedCompositeContainer
from google.protobuf.json_format import MessageToDict

background_task_results: Dict[str, Any] = {}

app = FastAPI()


@app.on_event("startup")
async def init_channel() -> None:
    """
    Initialize the Kachaka API client.
    """
    global kachaka_client
    kachaka_client = kachaka_api.aio.KachakaApiClient(
        os.getenv("KACHAKA_ACCESS_POINT", "localhost:26400"))
    await kachaka_client.update_resolver()


def to_dict(response: Any) -> Union[dict, list]:  # noqa: ANN401
    """
    Convert a Protobuf response to a dictionary.

    Args:
        response: The Protobuf response to convert.

    Returns:
        The response as a dictionary.
    """
    if response.__class__.__module__ == "kachaka_api_pb2":
        return MessageToDict(response)
    if (
        isinstance(response, tuple)
        or isinstance(response, list)
        or isinstance(response, RepeatedCompositeContainer)
    ):
        return [to_dict(r) for r in response]
    return response


async def run_method_or_404(attr: str, params: dict = {}, to_dict: bool = True) -> Any:  # noqa: ANN401
    """
    Run a method on the Kachaka API client or raise a 404 error if the method does not exist.

    Args:
        attr (str): The name of the method to run.
        params (dict): The arguments to pass to the method.
        to_dict (bool): Whether to convert the response to a dictionary.

    Returns:
        The response from the method or the response converted to a dictionary.

    Raises:
        HTTPException: If the method does not exist.
    """
    if not hasattr(kachaka_client, attr):
        raise HTTPException(status_code=404, detail="Method not found")
    method = getattr(kachaka_client, attr)
    response = await method(**params)
    return to_dict(response) if to_dict else response


@app.get("/kachaka/{front_or_back}_camera_image.jpeg")
async def front_or_back_camera_image(front_or_back: str) -> StreamingResponse:
    """
    Get the latest image from the front or back camera.

    Args:
        front_or_back (str): "front" or "back" to specify which camera to use.

    Returns:
        A StreamingResponse containing the image data.

    Raises:
        HTTPException: If the specified camera is not "front" or "back".
    """
    if front_or_back not in ["front", "back"]:
        raise HTTPException(status_code=404, detail="Camera not found")
    response = await run_method_or_404(f"get_{front_or_back}_camera_ros_compressed_image", to_dict=False)
    image_data = response.data
    image_format = response.format
    image_bytes = io.BytesIO(image_data)
    return StreamingResponse(image_bytes, media_type=f"image/{image_format}")


@app.get("/kachaka/{method:path}")
async def get(method: str) -> dict:
    """
    Run a GET method on the Kachaka API client.

    Args:
        method (str): The name of the method to run.

    Returns:
        The response from the method as a dictionary.
    """
    return await run_method_or_404(method)


@app.post("/kachaka/{method:path}")
async def post(method: str, params: dict, background_tasks: BackgroundTasks) -> dict:
    """
    Run a POST method on the Kachaka API client as a background task.

    Args:
        method (str): The name of the method to run.
        params (dict): The arguments to pass to the method.
        background_tasks (BackgroundTasks): FastAPI background tasks.

    Returns:
        A dictionary containing the task ID.
    """
    task_id = f"{method}_{id(params)}"

    async def background_task() -> None:
        result = await run_method_or_404(method, params)
        background_task_results[task_id] = result

    background_tasks.add_task(background_task)
    return {"id": f"{task_id}"}


@app.get("/command_result")
def get_command_result(task_id: str) -> dict:
    """
    Get the result of a background task.

    Args:
        task_id (str): The ID of the task.

    Returns:
        The result of the task as a dictionary.

    Raises:
        HTTPException: If the task is not found or has not completed.
    """
    result = background_task_results.get(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found or not completed")
    return result
