"""DataConverter that supports conversion of types used by OpenAI Agents SDK.

These are mostly Pydantic types. Some of them should be explicitly imported.
"""

from __future__ import annotations

import temporalio.contrib.pydantic

open_ai_data_converter = temporalio.contrib.pydantic.pydantic_data_converter
"""DEPRECATED, use temporalio.contrib.pydantic.pydantic_data_converter"""
