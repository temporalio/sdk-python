import os
import subprocess

# Build the Docker image and capture its ID
result = subprocess.run(
    ["docker", "build", "-q", "-f", "scripts/_proto/Dockerfile", "."],
    capture_output=True,
    text=True,
    check=True,
)
image_id = result.stdout.strip()

subprocess.run(
    [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{os.getcwd()}/temporalio/api:/api_new",
        "-v",
        f"{os.getcwd()}/temporalio/bridge/proto:/bridge_new",
        image_id,
    ],
    check=True,
)
subprocess.run(["uv", "run", "poe", "format"], check=True)
