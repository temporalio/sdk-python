import asyncio

import mywf

from temporalio.worker import DebugReplayer

if __name__ == "__main__":
    asyncio.run(DebugReplayer.start_debug_replayer([mywf.GreetingWorkflow]))
