import dataclasses
import uuid

from pydantic import BaseModel

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker
from tests.contrib.pydantic.models import (
    make_dataclass_objects,
    make_list_of_pydantic_objects,
)
from tests.contrib.pydantic.workflows import (
    CloneObjectsWorkflow,
    ComplexCustomTypeWorkflow,
    ComplexCustomUnionTypeWorkflow,
    DatetimeUsageWorkflow,
    InstantiateModelsWorkflow,
    PydanticModelUsageWorkflow,
    RoundTripObjectsWorkflow,
    clone_objects,
    pydantic_models_activity,
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


async def test_round_trip_pydantic_objects(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    orig_objects = make_list_of_pydantic_objects()

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[RoundTripObjectsWorkflow],
        activities=[pydantic_models_activity],
    ):
        returned_objects = await client.execute_workflow(
            RoundTripObjectsWorkflow.run,
            orig_objects,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
    assert returned_objects == orig_objects
    for o in returned_objects:
        o._check_instance()


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
        activities=[pydantic_models_activity],
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
        activities=[pydantic_models_activity],
    ):
        returned_objects = await client.execute_workflow(
            ComplexCustomUnionTypeWorkflow.run,
            orig_objects,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
    returned_dataclass_objects, returned_pydantic_objects = [], []
    for o in returned_objects:
        if dataclasses.is_dataclass(o):
            returned_dataclass_objects.append(o)
        elif isinstance(o, BaseModel):
            returned_pydantic_objects.append(o)
        else:
            raise TypeError(f"Unexpected type: {type(o)}")
    assert sorted(orig_dataclass_objects) == sorted(returned_dataclass_objects)
    assert sorted(orig_pydantic_objects, key=lambda o: o.__class__.__name__) == sorted(
        returned_pydantic_objects, key=lambda o: o.__class__.__name__
    )
    for o in returned_pydantic_objects:
        o._check_instance()


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
