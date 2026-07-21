import os
import subprocess

# Build the Docker image and capture its ID
result = subprocess.run(
    [
        "docker",
        "build",
        "-q",
        "-f",
        os.path.join("scripts", "_proto", "Dockerfile"),
        ".",
    ],
    stdout=subprocess.PIPE,
    text=True,
    check=True,
)
image_id = result.stdout.strip()

docker_run_command = [
    "docker",
    "run",
    "--rm",
]

getuid = getattr(os, "getuid", None)
getgid = getattr(os, "getgid", None)
if callable(getuid) and callable(getgid):
    docker_run_command.extend(["--user", f"{getuid()}:{getgid()}"])

docker_run_command.extend(
    [
        "-v",
        os.path.join(os.getcwd(), "temporalio", "api") + ":/api_new",
        "-v",
        os.path.join(os.getcwd(), "temporalio", "bridge", "proto") + ":/bridge_new",
        image_id,
    ]
)

subprocess.run(
    docker_run_command,
    check=True,
)
