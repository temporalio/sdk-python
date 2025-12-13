"""Quick test to see message flow."""

import asyncio
import logging
import signal

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Set a timeout
signal.alarm(5)

asyncio.run(__import__("test_debug_claude").main())