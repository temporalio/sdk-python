import dataclasses
import datetime
import os
import pathlib
import uuid

import pydantic
import pytest
from pydantic import BaseModel

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox._restrictions import (
    RestrictionContext,
    SandboxMatcher,
    _RestrictedProxy,
)
from tests.contrib.pydantic.models import (
    PydanticModels,
    PydanticModelWithStrictField,
    make_dataclass_objects,
    make_list_of_pydantic_objects,
)
from tests.contrib.pydantic.workflows import (
    CloneObjectsWorkflow,
    ComplexCustomTypeWorkflow,
    ComplexCustomUnionTypeWorkflow,
    DatetimeUsageWorkflow,
    InstantiateModelsWorkflow,
    NoTypeAnnotationsWorkflow,
    PydanticModelUsageWorkflow,
    PydanticModelWithStrictFieldWorkflow,
    RoundTripMiscObjectsWorkflow,
    RoundTripPydanticObjectsWorkflow,
    _test_pydantic_model_with_strict_field,
    clone_objects,
    misc_objects_activity,
    pydantic_objects_activity,
)


async def test_instantiation_outside_sandbox():
    make_list_of_pydantic_objects()


async def test_instantiation_inside_sandbox(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[InstantiateModelsWorkflow],
    ):
        await client.execute_workflow(
            InstantiateModelsWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )


@pytest.mark.parametrize("typed", [True, False])
async def test_round_trip_pydantic_objects(client: Client, typed: bool):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    orig_objects = make_list_of_pydantic_objects()

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[RoundTripPydanticObjectsWorkflow],
        activities=[pydantic_objects_activity],
    ):
        if typed:
            returned_objects = await client.execute_workflow(
                RoundTripPydanticObjectsWorkflow.run,
                orig_objects,
                id=str(uuid.uuid4()),
                task_queue=task_queue_name,
            )
        else:
            returned_objects = await client.execute_workflow(
                "RoundTripPydanticObjectsWorkflow",
                orig_objects,
                id=str(uuid.uuid4()),
                task_queue=task_queue_name,
                result_type=list[PydanticModels],
            )

    assert returned_objects == orig_objects
    for o in returned_objects:
        o._check_instance()


async def test_round_trip_misc_objects(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    orig_objects = (
        7,
        "7",
        {"7": 7.0},
        [{"7": 7.0}],
        ({"7": 7.0},),
        datetime.datetime(2025, 1, 2, 3, 4, 5),
        uuid.uuid4(),
    )

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[RoundTripMiscObjectsWorkflow],
        activities=[misc_objects_activity],
    ):
        returned_objects = await client.execute_workflow(
            RoundTripMiscObjectsWorkflow.run,
            orig_objects,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
    assert returned_objects == orig_objects


async def test_clone_objects_outside_sandbox():
    clone_objects(make_list_of_pydantic_objects())


async def test_clone_objects_in_sandbox(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    orig_objects = make_list_of_pydantic_objects()

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[CloneObjectsWorkflow],
    ):
        returned_objects = await client.execute_workflow(
            CloneObjectsWorkflow.run,
            orig_objects,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
    assert returned_objects == orig_objects
    for o in returned_objects:
        o._check_instance()


async def test_complex_custom_type(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    orig_dataclass_objects = make_dataclass_objects()
    orig_pydantic_objects = make_list_of_pydantic_objects()

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[ComplexCustomTypeWorkflow],
        activities=[pydantic_objects_activity],
    ):
        (
            returned_dataclass_objects,
            returned_pydantic_objects,
        ) = await client.execute_workflow(
            ComplexCustomTypeWorkflow.run,
            (orig_dataclass_objects, orig_pydantic_objects),
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
    assert orig_dataclass_objects == returned_dataclass_objects
    assert orig_pydantic_objects == returned_pydantic_objects
    for o in returned_pydantic_objects:
        o._check_instance()


async def test_complex_custom_union_type(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    orig_dataclass_objects = make_dataclass_objects()
    orig_pydantic_objects = make_list_of_pydantic_objects()
    orig_objects = orig_dataclass_objects + orig_pydantic_objects
    import random

    random.shuffle(orig_objects)

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[ComplexCustomUnionTypeWorkflow],
        activities=[pydantic_objects_activity],
    ):
        returned_objects = await client.execute_workflow(
            ComplexCustomUnionTypeWorkflow.run,
            orig_objects,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
    returned_dataclass_objects = []
    returned_pydantic_objects: list[BaseModel] = []
    for o in returned_objects:
        if dataclasses.is_dataclass(o):
            returned_dataclass_objects.append(o)
        elif isinstance(o, BaseModel):
            returned_pydantic_objects.append(o)
        else:
            raise TypeError(f"Unexpected type: {type(o)}")
    assert sorted(orig_dataclass_objects, key=lambda o: o.__class__.__name__) == sorted(
        returned_dataclass_objects, key=lambda o: o.__class__.__name__
    )
    assert sorted(orig_pydantic_objects, key=lambda o: o.__class__.__name__) == sorted(
        returned_pydantic_objects, key=lambda o: o.__class__.__name__
    )
    for o2 in returned_pydantic_objects:
        o2._check_instance()  # type: ignore


async def test_pydantic_model_usage_in_workflow(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[PydanticModelUsageWorkflow],
    ):
        await client.execute_workflow(
            PydanticModelUsageWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )


async def test_datetime_usage_in_workflow(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[DatetimeUsageWorkflow],
    ):
        await client.execute_workflow(
            DatetimeUsageWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )


def test_pydantic_model_with_strict_field_outside_sandbox():
    _test_pydantic_model_with_strict_field(
        PydanticModelWithStrictField(
            strict_field=datetime.datetime(2025, 1, 2, 3, 4, 5)
        )
    )


async def test_pydantic_model_with_strict_field_inside_sandbox(client: Client):
    client_config = client.config()
    client_config["data_converter"] = pydantic_data_converter
    client = Client(**client_config)
    tq = str(uuid.uuid4())
    async with Worker(
        client,
        workflows=[PydanticModelWithStrictFieldWorkflow],
        task_queue=tq,
    ):
        orig = PydanticModelWithStrictField(
            strict_field=datetime.datetime(2025, 1, 2, 3, 4, 5)
        )
        result = await client.execute_workflow(
            PydanticModelWithStrictFieldWorkflow.run,
            orig,
            id=str(uuid.uuid4()),
            task_queue=tq,
        )
        assert result == orig


async def test_no_type_annotations(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())
    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[NoTypeAnnotationsWorkflow],
    ):
        result = await client.execute_workflow(
            "NoTypeAnnotationsWorkflow",
            (7,),
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
    assert result == [7]


async def test_validation_error(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[NoTypeAnnotationsWorkflow],
    ):
        with pytest.raises(pydantic.ValidationError):
            await client.execute_workflow(
                "NoTypeAnnotationsWorkflow",
                "not-an-int",
                id=str(uuid.uuid4()),
                task_queue=task_queue_name,
                result_type=tuple[int],
            )


class RestrictedProxyFieldsModel(BaseModel):
    path_field: pathlib.Path
    uuid_field: uuid.UUID
    datetime_field: datetime.datetime


def test_model_instantiation_from_restricted_proxy_values():
    restricted_path_cls = _RestrictedProxy(
        "Path",
        pathlib.Path,
        RestrictionContext(),
        SandboxMatcher(),
    )
    restricted_uuid_cls = _RestrictedProxy(
        "uuid",
        uuid.UUID,
        RestrictionContext(),
        SandboxMatcher(),
    )
    restricted_datetime_cls = _RestrictedProxy(
        "datetime",
        datetime.datetime,
        RestrictionContext(),
        SandboxMatcher(),
    )

    restricted_path = restricted_path_cls("test/path")
    restricted_uuid = restricted_uuid_cls(bytes=os.urandom(16), version=4)
    restricted_datetime = restricted_datetime_cls(2025, 1, 2, 3, 4, 5)

    assert type(restricted_path) is _RestrictedProxy
    assert type(restricted_uuid) is _RestrictedProxy
    assert type(restricted_datetime) is not _RestrictedProxy
    p = RestrictedProxyFieldsModel(
        path_field=restricted_path,  # type: ignore
        uuid_field=restricted_uuid,  # type: ignore
        datetime_field=restricted_datetime,  # type: ignore
    )
    assert p.path_field == restricted_path
    assert p.uuid_field == restricted_uuid
    assert p.datetime_field == restricted_datetime
