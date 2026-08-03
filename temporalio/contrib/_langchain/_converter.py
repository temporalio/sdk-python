"""Payload converter shared by the LangChain-family plugins.

This module eagerly imports ``temporalio.contrib.pydantic``; only plugins
that actually wire the converter import it, so the core package itself stays
importable without pydantic installed.
"""

from __future__ import annotations

import dataclasses

from temporalio.contrib.pydantic import PydanticPayloadConverter, ToJsonOptions
from temporalio.converter import DataConverter


class LangChainPayloadConverter(PydanticPayloadConverter):
    """Pydantic payload converter pinned to ``exclude_unset=True``.

    LangChain request/response types are deeply nested with many
    ``Optional[...] = None`` fields. Shipping every unset default inflates
    payloads several-fold and some peers reject the explicit nulls on
    round-trip, so unset fields are excluded by convention.
    """

    def __init__(self) -> None:
        """Construct the converter with ``exclude_unset`` serialization."""
        super().__init__(ToJsonOptions(exclude_unset=True))


data_converter = DataConverter(payload_converter_class=LangChainPayloadConverter)
"""The family default data converter (LangChain messages are shipped as their
``dumpd`` JSON form, so the Pydantic converter only ever sees plain
containers)."""


def build_data_converter(
    user_converter: DataConverter | None,
    *,
    plugin_name: str,
) -> DataConverter:
    """Compose the family converter with whatever the caller already set.

    * ``None`` — install the family default.
    * the SDK default converter — swap in the LangChain-aware Pydantic
      converter via :func:`dataclasses.replace`.
    * a custom converter — refuse rather than silently clobber it; the caller
      must fold ``LangChainPayloadConverter`` into their own converter.
      ``plugin_name`` names the refusing plugin in the error.
    """
    if user_converter is None:
        return data_converter
    if user_converter is DataConverter.default:
        return dataclasses.replace(
            user_converter, payload_converter_class=LangChainPayloadConverter
        )
    raise ValueError(
        f"{plugin_name} cannot compose with a custom data_converter "
        "automatically. Set payload_converter_class=LangChainPayloadConverter "
        "on your own DataConverter (so LangChain messages serialize with "
        "exclude_unset=True), or omit data_converter to use the plugin default."
    )
