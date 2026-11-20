"""A model shared by the native client and the WASM workflow."""

import asyncio
import time

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

import temporalio.workflow as workflow


class WasmModel(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)

    name: str = Field(alias="displayName")

    @field_validator("name")
    @classmethod
    def normalize_name(cls, name: str) -> str:
        if name == "use-clock":
            time.time()
        return name.strip().lower()

    @field_serializer("name")
    def serialize_name(self, name: str) -> str:
        return name.upper()


@workflow.defn
class WasmPydantic:
    @workflow.run
    async def run(self, value: WasmModel) -> WasmModel:
        assert value.name == "temporal"
        await asyncio.sleep(0.01)
        return value.model_copy(update={"name": f"{value.name} wasm"})
