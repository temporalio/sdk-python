from __future__ import annotations

import asyncio
import dataclasses
import functools
import inspect
import os
import sys
import time
import uuid
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import IntEnum
from typing import Any

import pytest

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker, WorkflowInboundInterceptor
from temporalio.worker._interceptor import (
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInterceptorClassInput,
)
from temporalio.worker.workflow_sandbox import (
    RestrictedWorkflowAccessError,
    SandboxedWorkflowRunner,
    SandboxMatcher,
    SandboxRestrictions,
    UnintentionalPassthroughError,
)
from temporalio.workflow import SandboxImportNotificationPolicy
from tests.helpers import assert_eq_eventually
from tests.worker.workflow_sandbox.testmodules import stateful_module
from tests.worker.workflow_sandbox.testmodules.proto import SomeMessage

global_state = ["global orig"]
# We just access os.name in here to show we _can_. It's access-restricted at
# runtime only
_ = os.name

# This used to fail because our __init__ couldn't handle metaclass init
import zipfile  # noqa: E402


class MyZipFile(zipfile.ZipFile):
    pass


# This used to fail because subclass checks were inaccurate
assert issubclass(datetime, datetime)


@dataclass
class GlobalStateWorkflowParams:
    fail_on_first_attempt: bool = False


@workflow.defn
class GlobalStateWorkflow:
    def __init__(self) -> None:
        # We sleep here to defer to other threads so we can test the
        # thread-safety of global state isolation
        with workflow.unsafe.sandbox_unrestricted():
            time.sleep(0)
        self.append("inited")

    @workflow.run
    async def run(self, params: GlobalStateWorkflowParams) -> dict[str, list[str]]:
        self.append("started")
        if params.fail_on_first_attempt:
            raise ApplicationError("Failing first attempt")
        # Wait for "finish" to be in the state
        await workflow.wait_condition(lambda: "finish" in global_state)
        return self.state()

    @workflow.signal
    def append(self, str: str) -> None:
        global_state.append(str)
        stateful_module.module_state.append(str)

    @workflow.query
    def state(self) -> dict[str, list[str]]:
        return {"global": global_state, "module": stateful_module.module_state}


@pytest.mark.parametrize(
    "sandboxed_passthrough_modules",
    [
        # TODO(cretz): Disabling until https://github.com/protocolbuffers/upb/pull/804
        # SandboxRestrictions.passthrough_modules_minimum,
        SandboxRestrictions.passthrough_modules_with_temporal,
        SandboxRestrictions.passthrough_modules_maximum,
    ],
)
async def test_workflow_sandbox_global_state(
    client: Client,
    sandboxed_passthrough_modules: set[str],
):
    global global_state
    async with new_worker(
        client,
        GlobalStateWorkflow,
        sandboxed_passthrough_modules=sandboxed_passthrough_modules,
    ) as worker:
        # Start several workflows in the sandbox and make sure none of it
        # clashes
        handles: list[WorkflowHandle] = []
        for _ in range(10):
            handles.append(
                await client.start_workflow(
                    GlobalStateWorkflow.run,
                    GlobalStateWorkflowParams(),
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
            )

        # Wait until every workflow started
        async def handle_started(handle: WorkflowHandle) -> bool:
            return (
                "started" in (await handle.query(GlobalStateWorkflow.state))["global"]
            )

        for handle in handles:
            await assert_eq_eventually(True, functools.partial(handle_started, handle))

        # Send a signal to each one, but specific to its index
        for i, handle in enumerate(handles):
            await handle.signal(GlobalStateWorkflow.append, f"signal{i}")

        # Tell each one to finish
        for handle in handles:
            await handle.signal(GlobalStateWorkflow.append, "finish")

        # Get result of each and confirm state is as expected
        for i, handle in enumerate(handles):
            assert {
                "global": ["global orig", "inited", "started", f"signal{i}", "finish"],
                "module": ["module orig", "inited", "started", f"signal{i}", "finish"],
            } == await handle.result()

        # Confirm that _this_ global state wasn't even touched
        assert global_state == ["global orig"]
        assert stateful_module.module_state == ["module orig"]


@workflow.defn
class RunCodeWorkflow:
    @workflow.run
    async def run(self, code: str) -> None:
        try:
            exec(code)
        except RestrictedWorkflowAccessError as err:
            raise ApplicationError(
                str(err), type="RestrictedWorkflowAccessError"
            ) from err


async def test_workflow_sandbox_restrictions(client: Client):
    async with new_worker(client, RunCodeWorkflow) as worker:
        # Check restricted stuff
        invalid_code_to_check = [
            # Restricted module-level callable
            "import os\nos.getcwd()",
            # Even if called from import
            "import os\nfrom os import getcwd\ngetcwd()",
            # Wildcard "use"
            "import glob\nglob.glob('whatever')",
            # Runtime-only access restriction on var
            "import os\n_ = os.name",
            # Builtin
            "open('somefile')",
            # Importing module calling bad thing
            "import tests.worker.workflow_sandbox.testmodules.module_calling_invalid",
            # General library restrictions
            "import datetime\ndatetime.date.today()",
            "import datetime\ndatetime.datetime.now()",
            "import os\ngetattr(os.environ, 'foo')",
            "import os\nos.getenv('foo')",
            "import random\nrandom.choice(['foo', 'bar'])",
            "import secrets\nsecrets.choice(['foo', 'bar'])",
            "import threading\nthreading.current_thread()",
            "import time\ntime.sleep(1)",
            "import concurrent.futures\nconcurrent.futures.ThreadPoolExecutor()",
            "import urllib.request\nurllib.request.urlopen('example.com')",
            "import http.client\nhttp.client.HTTPConnection('example.com')",
            "import uuid\nuuid.uuid4()",
        ]

        # We can only validate this restriction prior to 3.13.12 because we had to exempt it due to
        # https://github.com/python/cpython/issues/140228
        if sys.version_info < (3, 13, 12):
            invalid_code_to_check.append("import os.path\nos.path.abspath('foo')")  # type: ignore[reportUnreachable]

        # uuid7 was only added to the stdlib in 3.14
        if sys.version_info >= (3, 14):
            invalid_code_to_check.append("import uuid\nuuid.uuid7()")  # type: ignore[reportUnreachable]

        # clock_gettime is not available on every platform (e.g. Windows)
        if hasattr(time, "clock_gettime"):
            invalid_code_to_check += [
                "import time\ntime.clock_gettime(time.CLOCK_REALTIME)",
                "import time\ntime.clock_gettime_ns(time.CLOCK_REALTIME)",
            ]

        for code in invalid_code_to_check:
            with pytest.raises(WorkflowFailureError) as err:
                await client.execute_workflow(
                    RunCodeWorkflow.run,
                    code,
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
            assert isinstance(err.value.cause, ApplicationError)
            assert err.value.cause.type == "RestrictedWorkflowAccessError"

        # Check unrestricted stuff
        valid_code_to_check = [
            # It is totally ok to reference a callable that you cannot call
            "import os\n_ = os.getcwd",
            # Even in an import
            "import os\nfrom os import getcwd\n_ = os.getcwd",
            # Can reference wildcard callable that cannot be called
            "import glob\n_ = glob.glob",
            # Allowed to call random choice when restrictions are disabled
            "import random\n"
            "import temporalio.workflow\n"
            "with temporalio.workflow.unsafe.sandbox_unrestricted():\n"
            "    random.choice(['foo', 'bar'])",
            # Allowed to import bad module when restrictions are disabled
            "import temporalio.workflow\n"
            "with temporalio.workflow.unsafe.sandbox_unrestricted():\n"
            "    import tests.worker.workflow_sandbox.testmodules.module_calling_invalid",
            # General library allowed calls
            "import datetime\ndatetime.date(2001, 1, 1)",
            "import uuid\nuuid.uuid5(uuid.NAMESPACE_DNS, 'example.com')",
            "from pathlib import Path\n"
            "path = original = Path('root')\n"
            "path /= 'child'\n"
            "assert str(path) == str(Path('root') / 'child')\n"
            "assert str(original) == 'root'",
            # Other imports we want to allow
            "from concurrent.futures import ThreadPoolExecutor",
        ]
        for code in valid_code_to_check:
            await client.execute_workflow(
                RunCodeWorkflow.run,
                code,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

    # Confirm it's easy to unrestrict stuff
    async with new_worker(
        client,
        RunCodeWorkflow,
        sandboxed_invalid_module_members=SandboxRestrictions.invalid_module_members_default.with_child_unrestricted(
            "datetime", "date", "today"
        ),
    ) as worker:
        await client.execute_workflow(
            RunCodeWorkflow.run,
            "import datetime\ndatetime.date.today()",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

    # Confirm it's easy to add restrictions
    async with new_worker(
        client,
        RunCodeWorkflow,
        sandboxed_invalid_module_members=SandboxRestrictions.invalid_module_members_default
        | SandboxMatcher(children={"datetime": SandboxMatcher(use={"date"})}),
    ) as worker:
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                RunCodeWorkflow.run,
                "import datetime\ndatetime.date(2001, 1, 1)",
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert isinstance(err.value.cause, ApplicationError)
        assert err.value.cause.type == "RestrictedWorkflowAccessError"


@workflow.defn
class DateOperatorWorkflow:
    @workflow.run
    async def run(self) -> int:
        return (date(2010, 1, 20) - date(2010, 1, 1)).days


async def test_workflow_sandbox_operator(client: Client):
    # Just confirm a simpler operator works
    async with new_worker(client, DateOperatorWorkflow) as worker:
        assert 19 == await client.execute_workflow(
            DateOperatorWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


global_in_sandbox = workflow.unsafe.in_sandbox()


@workflow.defn
class InSandboxWorkflow:
    def __init__(self) -> None:
        assert global_in_sandbox
        assert workflow.unsafe.in_sandbox()

    @workflow.run
    async def run(self) -> None:
        assert global_in_sandbox
        assert workflow.unsafe.in_sandbox()


async def test_workflow_sandbox_assert(client: Client):
    async with new_worker(client, InSandboxWorkflow) as worker:
        assert not global_in_sandbox
        assert not workflow.unsafe.in_sandbox()
        await client.execute_workflow(
            InSandboxWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


@workflow.defn
class AccessStackWorkflow:
    @workflow.run
    async def run(self) -> str:
        return inspect.stack()[0].function


async def test_workflow_sandbox_access_stack(client: Client):
    async with new_worker(client, AccessStackWorkflow) as worker:
        assert "run" == await client.execute_workflow(
            AccessStackWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


class InstanceCheckEnum(IntEnum):
    FOO = 1
    BAR = 2


@dataclass
class InstanceCheckData:
    some_enum: InstanceCheckEnum


@activity.defn
async def instance_check_activity(param: InstanceCheckData) -> InstanceCheckData:
    assert isinstance(param, InstanceCheckData)
    assert param.some_enum is InstanceCheckEnum.BAR
    return param


@workflow.defn
class InstanceCheckWorkflow:
    @workflow.run
    async def run(self, param: InstanceCheckData) -> InstanceCheckData:
        assert isinstance(param, InstanceCheckData)
        assert param.some_enum is InstanceCheckEnum.BAR
        # Exec child if not a child, otherwise exec activity
        if workflow.info().parent is None:
            return await workflow.execute_child_workflow(
                InstanceCheckWorkflow.run, param
            )
        return await workflow.execute_activity(
            instance_check_activity,
            param,
            schedule_to_close_timeout=timedelta(minutes=1),
        )


async def test_workflow_sandbox_instance_check(client: Client):
    async with new_worker(
        client, InstanceCheckWorkflow, activities=[instance_check_activity]
    ) as worker:
        await client.execute_workflow(
            InstanceCheckWorkflow.run,
            InstanceCheckData(some_enum=InstanceCheckEnum.BAR),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


@workflow.defn
class UseProtoWorkflow:
    @workflow.run
    async def run(self, param: SomeMessage) -> SomeMessage:
        assert isinstance(param, SomeMessage)
        return param


async def test_workflow_sandbox_with_proto(client: Client):
    async with new_worker(client, UseProtoWorkflow) as worker:
        param = SomeMessage()
        param.some_duration.FromTimedelta(timedelta(seconds=123))
        result = await client.execute_workflow(
            UseProtoWorkflow.run,
            param,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert result is not param and result == param


@workflow.defn
class KnownIssuesWorkflow:
    @workflow.run
    async def run(self) -> None:
        # Calling an internally defined method passing in self that is a
        # restricted proxy object will fail
        try:
            dict.items(os.__dict__)
            raise ApplicationError("Expected failure")
        except TypeError:
            pass

        # Using a subclass of a proxied class is unsupported
        try:
            MyZipFile("some/path")
            raise ApplicationError("Expected failure")
        except RuntimeError as err:
            assert "Restriction state not present" in str(err)


async def test_workflow_sandbox_known_issues(client: Client):
    async with new_worker(client, KnownIssuesWorkflow) as worker:
        await client.execute_workflow(
            KnownIssuesWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


@workflow.defn
class BadAsyncioWorkflow:
    @workflow.run
    async def run(self) -> list[str]:
        # Two known bad asyncio task calls, as_completed and wait
        async def return_value(value: str) -> str:
            return value

        ret = []
        for coro in asyncio.as_completed([return_value("val1"), return_value("val2")]):
            ret.append(await coro)
        done, _ = await asyncio.wait(
            [
                asyncio.create_task(return_value("val3")),
                asyncio.create_task(return_value("val4")),
            ]
        )
        for task in done:
            ret.append(await task)
        return ret


async def test_workflow_sandbox_bad_asyncio(client: Client):
    async with new_worker(client, BadAsyncioWorkflow) as worker:
        # We currently warn on this
        with pytest.warns(UserWarning) as warnings:
            result = await client.execute_workflow(
                BadAsyncioWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert "val1" in result[:2]
        assert "val2" in result[:2]
        assert "val3" in result[2:]
        assert "val4" in result[2:]
        assert len(warnings) == 2
        assert "asyncio.as_completed()" in str(warnings[0].message)
        assert "asyncio.wait()" in str(warnings[1].message)


@workflow.defn
class UnsupportedTaskGroupWorkflow:
    @workflow.run
    async def run(self) -> str:
        async with asyncio.TaskGroup() as group:
            task = group.create_task(asyncio.sleep(0, result="done"))
        return task.result()


async def test_workflow_sandbox_task_group_warning(client: Client):
    async with new_worker(client, UnsupportedTaskGroupWorkflow) as worker:
        with pytest.warns(
            UserWarning,
            match=r"asyncio\.TaskGroup\(\) has non-deterministic task cancellation order",
        ) as warnings:
            result = await client.execute_workflow(
                UnsupportedTaskGroupWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert result == "done"
        assert len(warnings) == 1


def new_worker(
    client: Client,
    *workflows: type,
    activities: Sequence[Callable] = [],
    task_queue: str | None = None,
    sandboxed_passthrough_modules: set[str] = set(),
    sandboxed_invalid_module_members: SandboxMatcher | None = None,
) -> Worker:
    restrictions = SandboxRestrictions.default
    if sandboxed_passthrough_modules:
        restrictions = dataclasses.replace(
            restrictions, passthrough_modules=sandboxed_passthrough_modules
        )
    if sandboxed_invalid_module_members:
        restrictions = dataclasses.replace(
            restrictions, invalid_module_members=sandboxed_invalid_module_members
        )
    return Worker(
        client,
        task_queue=task_queue or str(uuid.uuid4()),
        workflows=workflows,
        activities=activities,
        workflow_runner=SandboxedWorkflowRunner(restrictions=restrictions),
    )


class _TestWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        # import in the interceptor to show it will be captured
        # applying this policy should squelch the "after initial workload" warning
        with workflow.unsafe.sandbox_import_notification_policy(
            workflow.SandboxImportNotificationPolicy.WARN_ON_UNINTENTIONAL_PASSTHROUGH
        ):
            import tests.worker.workflow_sandbox.testmodules.lazy_module_interceptor  #  type:ignore[reportUnusedImport] # noqa: F401

        return await super().execute_workflow(input)


class _TestInterceptor(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[_TestWorkflowInboundInterceptor]:
        return _TestWorkflowInboundInterceptor


@workflow.defn
class LazyImportWorkflow:
    @workflow.run
    async def run(self) -> None:
        try:
            import tests.worker.workflow_sandbox.testmodules.lazy_module  #  type:ignore[reportUnusedImport] # noqa: F401
        except UnintentionalPassthroughError as err:
            raise ApplicationError(
                str(err), type="UnintentionalPassthroughError"
            ) from err


async def test_workflow_sandbox_import_default_warnings(client: Client):
    restrictions = dataclasses.replace(
        SandboxRestrictions.default,
        # passthrough this test module to avoid a ton of noisy warnings
        passthrough_modules=SandboxRestrictions.passthrough_modules_default
        | {"tests.worker.workflow_sandbox.test_runner"},
    )

    async with Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[LazyImportWorkflow],
        workflow_runner=SandboxedWorkflowRunner(restrictions),
    ) as worker:
        with pytest.warns() as recorder:
            await client.execute_workflow(
                LazyImportWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

            _assert_expected_warnings(
                recorder,
                {
                    "Module tests.worker.workflow_sandbox.testmodules.lazy_module was imported after initial workflow load.",
                },
            )


async def test_workflow_sandbox_import_workflow_without_warning():
    restrictions = SandboxRestrictions.default.with_import_notification_policy(
        SandboxImportNotificationPolicy.WARN_ON_UNINTENTIONAL_PASSTHROUGH
    )
    with warnings.catch_warnings(record=True) as recorder:
        warnings.simplefilter("always")
        SandboxedWorkflowRunner(restrictions).prepare_workflow(
            workflow._Definition.must_from_class(GlobalStateWorkflow)
        )

    actual_warnings = {str(w.message) for w in recorder}
    assert (
        f"Module {GlobalStateWorkflow.__module__} was not intentionally passed through to the sandbox."
        not in actual_warnings
    )
    assert (
        f"Module {stateful_module.__package__} was not intentionally passed through to the sandbox."
        in actual_warnings
    )


async def test_workflow_sandbox_import_all_warnings(client: Client):
    restrictions = dataclasses.replace(
        SandboxRestrictions.default,
        import_notification_policy=SandboxImportNotificationPolicy.WARN_ON_DYNAMIC_IMPORT
        | SandboxImportNotificationPolicy.WARN_ON_UNINTENTIONAL_PASSTHROUGH,
        # passthrough this test module to avoid a ton of noisy warnings
        passthrough_modules=SandboxRestrictions.passthrough_modules_default
        | {"tests.worker.workflow_sandbox.test_runner"},
    )

    async with Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[LazyImportWorkflow],
        interceptors=[_TestInterceptor()],
        workflow_runner=SandboxedWorkflowRunner(restrictions),
    ) as worker:
        with pytest.warns() as recorder:
            await client.execute_workflow(
                LazyImportWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

            _assert_expected_warnings(
                recorder,
                {
                    "Module tests.worker.workflow_sandbox.testmodules.lazy_module_interceptor was not intentionally passed through to the sandbox.",
                    "Module tests.worker.workflow_sandbox.testmodules.lazy_module was imported after initial workflow load.",
                    "Module tests.worker.workflow_sandbox.testmodules.lazy_module was not intentionally passed through to the sandbox.",
                },
            )


async def test_workflow_sandbox_import_errors(client: Client):
    restrictions = dataclasses.replace(
        SandboxRestrictions.default,
        import_notification_policy=SandboxImportNotificationPolicy.WARN_ON_DYNAMIC_IMPORT
        | SandboxImportNotificationPolicy.RAISE_ON_UNINTENTIONAL_PASSTHROUGH,
        # passthrough this test module to avoid a ton of noisy warnings
        passthrough_modules=SandboxRestrictions.passthrough_modules_default
        | {"tests.worker.workflow_sandbox.test_runner"},
    )

    async with Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[LazyImportWorkflow],
        workflow_runner=SandboxedWorkflowRunner(restrictions),
    ) as worker:
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                LazyImportWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

        assert isinstance(err.value.cause, ApplicationError)
        assert err.value.cause.type == "UnintentionalPassthroughError"
        assert (
            "Module tests.worker.workflow_sandbox.testmodules.lazy_module was not intentionally passed through to the sandbox."
            == err.value.cause.message
        )


@workflow.defn
class SupressWarningsLazyImportWorkflow:
    @workflow.run
    async def run(self) -> None:
        with workflow.unsafe.sandbox_import_notification_policy(
            SandboxImportNotificationPolicy.SILENT
        ):
            try:
                import tests.worker.workflow_sandbox.testmodules.lazy_module  #  type:ignore[reportUnusedImport] # noqa: F401
            except UserWarning:
                raise ApplicationError("No warnings were expected")


async def test_workflow_sandbox_import_suppress_warnings(client: Client):
    restrictions = dataclasses.replace(
        SandboxRestrictions.default,
        # passthrough this test module to avoid a ton of noisy warnings
        passthrough_modules=SandboxRestrictions.passthrough_modules_default
        | {"tests.worker.workflow_sandbox.test_runner"},
    )

    async with Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[SupressWarningsLazyImportWorkflow],
        workflow_runner=SandboxedWorkflowRunner(restrictions),
    ) as worker:
        with warnings.catch_warnings(record=True) as recorder:
            warnings.simplefilter("always")
            await client.execute_workflow(
                SupressWarningsLazyImportWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            assert len(recorder) == 0, "Expected no warnings to be issued"


def _assert_expected_warnings(
    recorder: pytest.WarningsRecorder, expected_warnings: set[str]
):
    actual_warnings = {str(w.message) for w in recorder}
    assert expected_warnings <= actual_warnings


async def test_workflow_sandbox_no_hot_reload_on_disk_change(
    client: Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
):
    import importlib
    import shutil

    pkg_dir = tmp_path / "no_hot_reload_pkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "helper.py").write_text("def val() -> str:\n    return 'helper_v1'\n")
    (pkg_dir / "lazy_helper.py").write_text("def val() -> str:\n    return 'lazy_v1'\n")
    (pkg_dir / "wf.py").write_text(
        "from temporalio import workflow\n"
        "from . import helper\n"
        "@workflow.defn\n"
        "class DiskWorkflow:\n"
        "    def __init__(self) -> None:\n"
        "        self._done = False\n"
        "    @workflow.signal\n"
        "    def finish(self) -> None:\n"
        "        self._done = True\n"
        "    @workflow.run\n"
        "    async def run(self) -> list[str]:\n"
        "        with workflow.unsafe.sandbox_import_notification_policy(\n"
        "            workflow.SandboxImportNotificationPolicy.SILENT\n"
        "        ):\n"
        "            from . import lazy_helper\n"
        "        await workflow.wait_condition(lambda: self._done)\n"
        "        return [helper.val(), lazy_helper.val(), 'wf_v1']\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    try:
        wf_mod = importlib.import_module("no_hot_reload_pkg.wf")
        DiskWorkflow = wf_mod.DiskWorkflow

        # max_cached_workflows=0 forces eviction & sandbox re-import on every WFT
        async with Worker(
            client,
            task_queue=f"tq-{uuid.uuid4()}",
            workflows=[DiskWorkflow],
            max_cached_workflows=0,
        ) as worker:
            # Complete run 1 so lazy_helper is also loaded into the worker's cache
            handle1 = await client.start_workflow(
                DiskWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            await handle1.signal(DiskWorkflow.finish)
            assert ["helper_v1", "lazy_v1", "wf_v1"] == await handle1.result()

            # Start run 2 and wait until its first WFT has been processed & evicted
            handle2 = await client.start_workflow(
                DiskWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            await assert_eq_eventually(
                True,
                lambda: _has_workflow_task_completed(handle2),
            )

            # Overwrite all workflow and helper files on disk and clear __pycache__
            pycache = pkg_dir / "__pycache__"
            if pycache.exists():
                shutil.rmtree(pycache)
            (pkg_dir / "helper.py").write_text(
                "def val() -> str:\n    return 'helper_v2_from_disk'\n"
            )
            (pkg_dir / "lazy_helper.py").write_text(
                "def val() -> str:\n    return 'lazy_v2_from_disk'\n"
            )
            (pkg_dir / "wf.py").write_text(
                "from temporalio import workflow\n"
                "from . import helper\n"
                "@workflow.defn\n"
                "class DiskWorkflow:\n"
                "    def __init__(self) -> None:\n"
                "        self._done = False\n"
                "    @workflow.signal\n"
                "    def finish(self) -> None:\n"
                "        self._done = True\n"
                "    @workflow.run\n"
                "    async def run(self) -> list[str]:\n"
                "        with workflow.unsafe.sandbox_import_notification_policy(\n"
                "            workflow.SandboxImportNotificationPolicy.SILENT\n"
                "        ):\n"
                "            from . import lazy_helper\n"
                "        await workflow.wait_condition(lambda: self._done)\n"
                "        return [helper.val(), lazy_helper.val(), 'wf_v2_from_disk']\n"
            )
            importlib.invalidate_caches()

            # Evicted run 2 replays and completes with v1, not v2_from_disk
            await handle2.signal(DiskWorkflow.finish)
            assert ["helper_v1", "lazy_v1", "wf_v1"] == await handle2.result()

            # New run 3 on the same worker also uses v1
            handle3 = await client.start_workflow(
                DiskWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            await handle3.signal(DiskWorkflow.finish)
            assert ["helper_v1", "lazy_v1", "wf_v1"] == await handle3.result()

            # Even if the package directory is deleted from disk, run 4 succeeds
            shutil.rmtree(pkg_dir)
            importlib.invalidate_caches()
            handle4 = await client.start_workflow(
                DiskWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            await handle4.signal(DiskWorkflow.finish)
            assert ["helper_v1", "lazy_v1", "wf_v1"] == await handle4.result()
    finally:
        for mod_name in list(sys.modules):
            if mod_name == "no_hot_reload_pkg" or mod_name.startswith(
                "no_hot_reload_pkg."
            ):
                sys.modules.pop(mod_name, None)


async def test_workflow_sandbox_separate_workers_isolated_module_cache(
    client: Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
):
    import importlib
    import shutil

    mod_file = tmp_path / "isolated_workers_wf.py"
    mod_file.write_text(
        "from temporalio import workflow\n"
        "@workflow.defn\n"
        "class IsolatedWf:\n"
        "    @workflow.run\n"
        "    async def run(self) -> str:\n"
        "        return 'worker_v1'\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    try:
        mod = importlib.import_module("isolated_workers_wf")
        async with Worker(
            client,
            task_queue=f"tq-{uuid.uuid4()}",
            workflows=[mod.IsolatedWf],
        ) as worker1:
            assert "worker_v1" == await client.execute_workflow(
                mod.IsolatedWf.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker1.task_queue,
            )

            # Update on disk and reload on host, then start a second worker
            pycache = tmp_path / "__pycache__"
            if pycache.exists():
                shutil.rmtree(pycache)
            mod_file.write_text(
                "from temporalio import workflow\n"
                "@workflow.defn\n"
                "class IsolatedWf:\n"
                "    @workflow.run\n"
                "    async def run(self) -> str:\n"
                "        return 'worker_v2_reloaded'\n"
            )
            importlib.invalidate_caches()
            mod = importlib.reload(mod)

            async with Worker(
                client,
                task_queue=f"tq-{uuid.uuid4()}",
                workflows=[mod.IsolatedWf],
            ) as worker2:
                # Worker 2 sees reloaded v2; Worker 1 still runs v1
                assert "worker_v2_reloaded" == await client.execute_workflow(
                    mod.IsolatedWf.run,
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker2.task_queue,
                )
                assert "worker_v1" == await client.execute_workflow(
                    mod.IsolatedWf.run,
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker1.task_queue,
                )
    finally:
        sys.modules.pop("isolated_workers_wf", None)


async def _has_workflow_task_completed(handle: WorkflowHandle) -> bool:
    from temporalio.api.enums.v1 import EventType

    history = await handle.fetch_history()
    return any(
        e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
        for e in history.events
    )


async def test_workflow_sandbox_replayer_no_hot_reload(
    client: Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
):
    import importlib
    import shutil
    from collections.abc import AsyncIterator

    from temporalio.client import WorkflowHistory
    from temporalio.worker import Replayer

    mod_file = tmp_path / "replayer_no_hot_reload_wf.py"
    mod_file.write_text(
        "from temporalio import workflow\n"
        "@workflow.defn\n"
        "class ReplayDiskWf:\n"
        "    @workflow.run\n"
        "    async def run(self) -> str:\n"
        "        return 'replay_v1'\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    try:
        mod = importlib.import_module("replayer_no_hot_reload_wf")
        async with Worker(
            client,
            task_queue=f"tq-{uuid.uuid4()}",
            workflows=[mod.ReplayDiskWf],
        ) as worker:
            h1 = await client.start_workflow(
                mod.ReplayDiskWf.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            assert "replay_v1" == await h1.result()
            h2 = await client.start_workflow(
                mod.ReplayDiskWf.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            assert "replay_v1" == await h2.result()
            hist1 = await h1.fetch_history()
            hist2 = await h2.fetch_history()

        async def histories() -> AsyncIterator[WorkflowHistory]:
            yield hist1
            # Overwrite the workflow file on disk between replayed histories
            pycache = tmp_path / "__pycache__"
            if pycache.exists():
                shutil.rmtree(pycache)
            mod_file.write_text(
                "from datetime import timedelta\n"
                "from temporalio import workflow\n"
                "@workflow.defn\n"
                "class ReplayDiskWf:\n"
                "    @workflow.run\n"
                "    async def run(self) -> str:\n"
                "        await workflow.sleep(timedelta(milliseconds=1))\n"
                "        return 'replay_v2_from_disk'\n"
            )
            importlib.invalidate_caches()
            yield hist2

        replayer = Replayer(workflows=[mod.ReplayDiskWf])
        await replayer.replay_workflows(histories())
    finally:
        sys.modules.pop("replayer_no_hot_reload_wf", None)
