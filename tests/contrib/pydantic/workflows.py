import dataclasses
from datetime import datetime, timedelta
from typing import List

from pydantic import BaseModel, create_model

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from tests.contrib.pydantic.activities import pydantic_models_activity

from tests.contrib.pydantic.models import (
    ComplexCustomType,
    ComplexCustomUnionType,
    PydanticModels,
    PydanticModelWithStrictField,
    make_list_of_pydantic_objects,
)


def clone_objects(objects: List[PydanticModels]) -> List[PydanticModels]:
    new_objects = []
    for o in objects:
        fields = {}
        for name, f in o.model_fields.items():
            fields[name] = (f.annotation, f)
        model = create_model(o.__class__.__name__, **fields)  # type: ignore
        new_objects.append(model(**o.model_dump(by_alias=True)))
    for old, new in zip(objects, new_objects):
        assert old.model_dump() == new.model_dump()
    return new_objects


@workflow.defn
class InstantiateModelsWorkflow:
    @workflow.run
    async def run(self) -> None:
        make_list_of_pydantic_objects()


@workflow.defn
class RoundTripObjectsWorkflow:
    @workflow.run
    async def run(self, objects: List[PydanticModels]) -> List[PydanticModels]:
        return await workflow.execute_activity(
            pydantic_models_activity,
            objects,
            start_to_close_timeout=timedelta(minutes=1),
        )


@workflow.defn
class CloneObjectsWorkflow:
    @workflow.run
    async def run(self, objects: List[PydanticModels]) -> List[PydanticModels]:
        return clone_objects(objects)


@workflow.defn
class ComplexCustomUnionTypeWorkflow:
    @workflow.run
    async def run(
        self,
        input: ComplexCustomUnionType,
    ) -> ComplexCustomUnionType:
        data_classes = []
        pydantic_objects: List[PydanticModels] = []
        for o in input:
            if dataclasses.is_dataclass(o):
                data_classes.append(o)
            elif isinstance(o, BaseModel):
                pydantic_objects.append(o)
            else:
                raise TypeError(f"Unexpected type: {type(o)}")
        pydantic_objects = await workflow.execute_activity(
            pydantic_models_activity,
            pydantic_objects,
            start_to_close_timeout=timedelta(minutes=1),
        )
        return data_classes + pydantic_objects  # type: ignore


@workflow.defn
class ComplexCustomTypeWorkflow:
    @workflow.run
    async def run(
        self,
        input: ComplexCustomType,
    ) -> ComplexCustomType:
        data_classes, pydantic_objects = input
        pydantic_objects = await workflow.execute_activity(
            pydantic_models_activity,
            pydantic_objects,
            start_to_close_timeout=timedelta(minutes=1),
        )
        return data_classes, pydantic_objects


@workflow.defn
class PydanticModelUsageWorkflow:
    @workflow.run
    async def run(self) -> None:
        for o in make_list_of_pydantic_objects():
            o._check_instance()


@workflow.defn
class DatetimeUsageWorkflow:
    @workflow.run
    async def run(self) -> None:
        dt = workflow.now()
        assert isinstance(dt, datetime)
        assert issubclass(dt.__class__, datetime)


def _test_pydantic_model_with_strict_field(
    obj: PydanticModelWithStrictField,
):
    roundtripped = PydanticModelWithStrictField.model_validate(obj.model_dump())
    assert roundtripped == obj
    roundtripped2 = PydanticModelWithStrictField.model_validate_json(
        obj.model_dump_json()
    )
    assert roundtripped2 == obj
    return roundtripped


@workflow.defn
class PydanticModelWithStrictFieldWorkflow:
    @workflow.run
    async def run(
        self, obj: PydanticModelWithStrictField
    ) -> PydanticModelWithStrictField:
        return _test_pydantic_model_with_strict_field(obj)
