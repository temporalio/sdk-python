import asyncio

import pytest

from .fixtures.utils import client, server, worker


@pytest.fixture(scope="session")
def event_loop():
    # See https://github.com/pytest-dev/pytest-asyncio/issues/68
    # See https://github.com/pytest-dev/pytest-asyncio/issues/257
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()
