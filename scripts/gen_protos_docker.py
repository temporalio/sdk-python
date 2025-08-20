import os
import subprocess

subprocess.run(["docker", "build", "-f", "scripts/_proto/Dockerfile", "."])
image_id = subprocess.check_output(["docker", "images", "-q"], text=True).split()[0]
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
    ]
)
subprocess.run(["uv", "run", "poe", "format"])
