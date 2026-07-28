from __future__ import annotations

import json

import pytest

from temporalio.converter import DataConverter, JSONPlainPayloadConverter
from temporalio.contrib.sanitizer import SanitizingPayloadCodec


@pytest.mark.asyncio
async def test_redacts_dict_keys():
    dc = DataConverter(payload_codec=SanitizingPayloadCodec())
    payloads = await dc.encode([{"api_key": "abc", "name": "bob"}])
    assert payloads[0].metadata.get("encoding") == b"json/plain"
    val = json.loads(payloads[0].data)
    assert val["api_key"] == "[REDACTED]"
    assert val["name"] == "bob"


@pytest.mark.asyncio
async def test_nested_and_list_traversal():
    dc = DataConverter(payload_codec=SanitizingPayloadCodec(key_patterns=["password"]))
    obj = {"user": {"password": "secret", "emails": ["a@x", "b@y"]}}
    payloads = await dc.encode([obj])
    val = json.loads(payloads[0].data)
    assert val["user"]["password"] == "[REDACTED]"
    assert val["user"]["emails"] == ["a@x", "b@y"]


@pytest.mark.asyncio
async def test_noop_when_no_sensitive_keys():
    dc = DataConverter(payload_codec=SanitizingPayloadCodec())
    payloads = await dc.encode([[1, 2, 3]])
    assert json.loads(payloads[0].data) == [1, 2, 3]
