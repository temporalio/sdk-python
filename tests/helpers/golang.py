import asyncio
import asyncio.subprocess
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


async def start_external_go_process(
    source_dir: str, exe_name: str, *args: str
) -> asyncio.subprocess.Process:
    # First, build the executable. We accept the performance issues of building
    # this each run.
    logger.info("Building %s", exe_name)
    subprocess.run(["go", "build", "-o", exe_name, "."], cwd=source_dir, check=True)
    logger.info("Starting %s", exe_name)
    return await asyncio.create_subprocess_exec(
        os.path.join(source_dir, exe_name), *args
    )
