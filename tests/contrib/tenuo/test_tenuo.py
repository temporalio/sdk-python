"""Tests for temporalio.contrib.tenuo."""

import inspect
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

import tenuo
from tenuo.temporal import (
    EnvKeyResolver,
    TenuoPluginConfig,
    execute_workflow_authorized,
    start_workflow_authorized,
    tenuo_execute_activity,
)
from tenuo.temporal._interceptors import _TenuoWorkflowOutboundInterceptor

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.tenuo import TenuoPlugin
from temporalio.contrib.tenuo._plugin import ensure_tenuo_workflow_runner
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def signing_key():
    """Generate an ephemeral signing key for tests."""
    return tenuo.SigningKey.generate()


@pytest.fixture
def config(signing_key):
    """Minimal TenuoPluginConfig with an EnvKeyResolver."""
    resolver = EnvKeyResolver()
    resolver._key_cache["test-key"] = signing_key
    return TenuoPluginConfig(
        key_resolver=resolver,
        trusted_roots=[signing_key.public_key],
    )


# ---------------------------------------------------------------------------
# Plugin construction
# ---------------------------------------------------------------------------


class TestPluginConstruction:
    """TenuoPlugin initializes correctly as a SimplePlugin."""

    def test_creates_client_interceptor(self, config):
        """Plugin creates a client interceptor."""
        plugin = TenuoPlugin(config)
        assert plugin.client_interceptor is not None

    def test_accepts_external_client_interceptor(self, config):
        """Plugin accepts an externally provided client interceptor."""
        from tenuo.temporal import TenuoClientInterceptor

        ext = TenuoClientInterceptor()
        plugin = TenuoPlugin(config, client_interceptor=ext)
        assert plugin.client_interceptor is ext

    def test_duplicate_worker_flag(self, config):
        """Plugin tracks whether it has been configured for a worker."""
        plugin = TenuoPlugin(config)
        assert plugin._tenuo_worker_configured is False

    def test_duplicate_registration_raises(self, config):
        """Using the same plugin for two configure_worker calls raises RuntimeError."""
        plugin = TenuoPlugin(config)
        worker_config: dict = {"activities": []}
        plugin.configure_worker(worker_config)
        assert plugin._tenuo_worker_configured is True

        with pytest.raises(RuntimeError, match="Duplicate Tenuo plugin"):
            plugin.configure_worker({"activities": []})


# ---------------------------------------------------------------------------
# Sandbox passthrough
# ---------------------------------------------------------------------------


class TestSandboxPassthrough:
    """_ensure_tenuo_workflow_runner adds tenuo/tenuo_core passthrough."""

    def test_creates_sandboxed_runner_from_none(self):
        """When no runner exists, creates one with passthrough."""
        runner = ensure_tenuo_workflow_runner(None)
        assert isinstance(runner, SandboxedWorkflowRunner)

    def test_adds_passthrough_to_existing_sandboxed_runner(self):
        """Adds passthrough modules to an existing SandboxedWorkflowRunner."""
        existing = SandboxedWorkflowRunner(restrictions=SandboxRestrictions.default)
        runner = ensure_tenuo_workflow_runner(existing)
        assert isinstance(runner, SandboxedWorkflowRunner)

    def test_returns_unsandboxed_runner_unchanged(self):
        """Non-sandboxed runners are returned as-is."""
        mock_runner = MagicMock()
        result = ensure_tenuo_workflow_runner(mock_runner)
        assert result is mock_runner


# ---------------------------------------------------------------------------
# Replay safety — determinism properties
# ---------------------------------------------------------------------------


class TestReplaySafety:
    """Workflow interceptor code is deterministic and safe for replay."""

    def test_uses_workflow_now_not_time_time(self):
        """Outbound interceptor uses workflow.now() for PoP timestamps."""
        source = inspect.getsource(_TenuoWorkflowOutboundInterceptor)
        assert "workflow.now()" in source
        assert "time.time()" not in source

    def test_uses_workflow_now_not_datetime_now(self):
        """Outbound interceptor does not call datetime.now()."""
        source = inspect.getsource(_TenuoWorkflowOutboundInterceptor)
        assert "datetime.now()" not in source

    def test_no_non_deterministic_calls(self):
        """No non-deterministic stdlib calls in the workflow interceptor."""
        source = inspect.getsource(_TenuoWorkflowOutboundInterceptor)
        forbidden = ["os.urandom", "random.random", "random.randint", "uuid4()"]
        violations = [p for p in forbidden if p in source]
        assert not violations, f"Non-deterministic calls found: {violations}"

    def test_no_time_sleep(self):
        """Interceptor must not call time.sleep() — blocks the event loop."""
        source = inspect.getsource(_TenuoWorkflowOutboundInterceptor)
        assert "time.sleep" not in source, (
            "time.sleep() in workflow interceptor blocks the event loop "
            "and breaks Temporal replay."
        )

    def test_no_threading_in_interceptor(self):
        """Interceptor must not spawn threads — non-deterministic under replay."""
        source = inspect.getsource(_TenuoWorkflowOutboundInterceptor)
        forbidden = ["threading.Thread(", "Thread("]
        violations = [p for p in forbidden if p in source]
        assert not violations, (
            f"Threading calls found in workflow interceptor: {violations}. "
            "Spawning threads is non-deterministic under Temporal replay."
        )

    def test_pop_deterministic_with_fixed_timestamp(self, signing_key):
        """Same inputs produce the same PoP signature."""
        warrant = (
            tenuo.Warrant.mint_builder()
            .tools(["read_file"])
            .ttl(3600)
            .mint(signing_key)
        )
        ts = 1704110400
        sig1 = warrant.sign(signing_key, "read_file", {"path": "/tmp"}, ts)
        sig2 = warrant.sign(signing_key, "read_file", {"path": "/tmp"}, ts)
        assert sig1 == sig2

    def test_pop_differs_with_different_timestamps(self, signing_key):
        """Different timestamps produce different PoP signatures."""
        warrant = (
            tenuo.Warrant.mint_builder()
            .tools(["read_file"])
            .ttl(3600)
            .mint(signing_key)
        )
        sig1 = warrant.sign(signing_key, "read_file", {"path": "/tmp"}, 1704110400)
        sig2 = warrant.sign(signing_key, "read_file", {"path": "/tmp"}, 1704110460)
        assert sig1 != sig2


# ---------------------------------------------------------------------------
# EnvKeyResolver sandbox safety
# ---------------------------------------------------------------------------


class TestEnvKeyResolverSandbox:
    """EnvKeyResolver uses cached keys inside the workflow sandbox."""

    def test_resolve_sync_uses_cache(self, signing_key):
        """resolve_sync returns cached key without os.environ access."""
        resolver = EnvKeyResolver()
        resolver._key_cache["cached-key"] = signing_key

        with patch.dict("os.environ", {}, clear=True):
            resolved = resolver.resolve_sync("cached-key")

        assert resolved is not None

    def test_preload_all_caches_env_keys(self, signing_key, monkeypatch):
        """preload_all() reads TENUO_KEY_* vars into cache."""
        import base64

        key_b64 = base64.b64encode(signing_key.secret_key_bytes()).decode()
        monkeypatch.setenv("TENUO_KEY_mykey", key_b64)

        resolver = EnvKeyResolver()
        resolver.preload_all()

        assert "mykey" in resolver._key_cache


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------


class TestReExports:
    """Public API is accessible from temporalio.contrib.tenuo."""

    def test_plugin_importable(self):
        """TenuoPlugin is importable from contrib."""
        from temporalio.contrib.tenuo import TenuoPlugin as P

        assert P is not None

    def test_plugin_name_importable(self):
        """TENUO_PLUGIN_NAME is importable from contrib."""
        from temporalio.contrib.tenuo import TENUO_PLUGIN_NAME

        assert TENUO_PLUGIN_NAME == "tenuo.TenuoTemporalPlugin"

    def test_ensure_runner_importable(self):
        """ensure_tenuo_workflow_runner is importable from contrib."""
        from temporalio.contrib.tenuo import ensure_tenuo_workflow_runner

        assert ensure_tenuo_workflow_runner is not None

    def test_no_tenuo_temporal_reexports(self):
        """Contrib does not re-export tenuo.temporal types."""
        import temporalio.contrib.tenuo as mod

        assert not hasattr(mod, "TenuoPluginConfig")
        assert not hasattr(mod, "EnvKeyResolver")
        assert not hasattr(mod, "execute_workflow_authorized")
        assert not hasattr(mod, "tenuo_execute_activity")


# ---------------------------------------------------------------------------
# Live integration — full warrant → PoP → authorize → reject flow
# ---------------------------------------------------------------------------


@activity.defn
async def lookup_customer(customer_id: str) -> dict:
    """Fetch customer record — authorized by warrant."""
    return {"id": customer_id, "name": "Alice", "plan": "pro"}


@activity.defn
async def search_knowledge_base(query: str, department: str) -> str:
    """Search internal docs — authorized by warrant."""
    return f"docs for '{query}' in {department}"


@activity.defn
async def call_llm(prompt: str, model: str, max_tokens: int) -> str:
    """Call an LLM — warrant pins the model."""
    return f"LLM response (model={model})"


@activity.defn
async def send_response(channel: str, ticket_id: str, message: str) -> str:
    """Send a customer response — warrant restricts channels."""
    return f"sent via {channel}: {ticket_id}"


@activity.defn
async def drop_customer_table(confirm: str) -> str:
    """Destructive activity NOT covered by the warrant."""
    return "table dropped"


@workflow.defn
class SupportTriageWorkflow:
    """Agent workflow: look up customer, search docs, respond via LLM."""

    @workflow.run
    async def run(self, customer_id: str) -> str:
        """Run the workflow."""
        customer = await tenuo_execute_activity(
            lookup_customer,
            args=[customer_id],
            start_to_close_timeout=timedelta(seconds=10),
        )
        return f"Helped {customer['name']}"


@workflow.defn
class UnauthorizedDropWorkflow:
    """Workflow that attempts to call an activity NOT in the warrant."""

    @workflow.run
    async def run(self, confirm: str) -> str:
        """Run the workflow."""
        return await tenuo_execute_activity(
            drop_customer_table,
            args=[confirm],
            start_to_close_timeout=timedelta(seconds=10),
        )


ALL_ACTIVITIES = [
    lookup_customer,
    search_knowledge_base,
    call_llm,
    send_response,
    drop_customer_table,
]


def _make_plugin_and_warrant(signing_key):
    """Create a plugin and warrant scoped to the support triage tools."""
    resolver = EnvKeyResolver()
    resolver._key_cache["test-key"] = signing_key

    config = TenuoPluginConfig(
        key_resolver=resolver,
        trusted_roots=[signing_key.public_key],
    )
    plugin = TenuoPlugin(config)

    warrant = (
        tenuo.Warrant.mint_builder()
        .capability("lookup_customer")
        .capability("search_knowledge_base")
        .capability("call_llm")
        .capability("send_response")
        .holder(signing_key.public_key)
        .ttl(3600)
        .mint(signing_key)
    )
    return plugin, warrant


@pytest.mark.timeout(120)
async def test_authorized_activity_succeeds():
    """Warrant covers the activity — support triage succeeds with PoP."""
    signing_key = tenuo.SigningKey.generate()
    plugin, warrant = _make_plugin_and_warrant(signing_key)

    async with await WorkflowEnvironment.start_local() as env:
        client = Client(
            env.client.service_client,
            namespace=env.client.namespace,
            data_converter=env.client.data_converter,
            plugins=[plugin],
        )

        async with Worker(
            client,
            task_queue="triage-test-queue",
            workflows=[SupportTriageWorkflow],
            activities=ALL_ACTIVITIES,
        ):
            result = await execute_workflow_authorized(
                client=client,
                workflow_run_fn=SupportTriageWorkflow.run,
                args=["cust-1001"],
                warrant=warrant,
                key_id="test-key",
                workflow_id="triage-wf-1",
                task_queue="triage-test-queue",
            )
            assert result == "Helped Alice"


@pytest.mark.timeout(120)
async def test_start_workflow_authorized():
    """start_workflow_authorized starts the workflow and returns a handle."""
    signing_key = tenuo.SigningKey.generate()
    plugin, warrant = _make_plugin_and_warrant(signing_key)

    async with await WorkflowEnvironment.start_local() as env:
        client = Client(
            env.client.service_client,
            namespace=env.client.namespace,
            data_converter=env.client.data_converter,
            plugins=[plugin],
        )

        async with Worker(
            client,
            task_queue="triage-start-queue",
            workflows=[SupportTriageWorkflow],
            activities=ALL_ACTIVITIES,
        ):
            handle = await start_workflow_authorized(
                client=client,
                workflow_run_fn=SupportTriageWorkflow.run,
                args=["cust-1001"],
                warrant=warrant,
                key_id="test-key",
                workflow_id="triage-start-wf-1",
                task_queue="triage-start-queue",
            )
            result = await handle.result()
            assert result == "Helped Alice"


@pytest.mark.timeout(120)
async def test_unauthorized_activity_is_non_retryable():
    """Warrant does NOT cover drop_customer_table — non-retryable ApplicationError.

    Authorization failures must be non-retryable so Temporal doesn't retry the
    workflow task forever.  This test verifies the full chain: the outbound
    interceptor detects that ``drop_customer_table`` is not in the warrant,
    wraps the error as ``ApplicationError(non_retryable=True)``, and the
    workflow surfaces it as a ``WorkflowFailureError`` whose cause is an
    ``ApplicationError`` with the non-retryable flag set.
    """
    signing_key = tenuo.SigningKey.generate()
    plugin, warrant = _make_plugin_and_warrant(signing_key)

    async with await WorkflowEnvironment.start_local() as env:
        client = Client(
            env.client.service_client,
            namespace=env.client.namespace,
            data_converter=env.client.data_converter,
            plugins=[plugin],
        )

        async with Worker(
            client,
            task_queue="triage-deny-queue",
            workflows=[UnauthorizedDropWorkflow],
            activities=ALL_ACTIVITIES,
        ):
            from temporalio.client import WorkflowFailureError
            from temporalio.exceptions import ApplicationError

            with pytest.raises(WorkflowFailureError) as exc_info:
                await execute_workflow_authorized(
                    client=client,
                    workflow_run_fn=UnauthorizedDropWorkflow.run,
                    args=["yes"],
                    warrant=warrant,
                    key_id="test-key",
                    workflow_id="triage-deny-wf-1",
                    task_queue="triage-deny-queue",
                )

            cause = exc_info.value.cause
            assert isinstance(cause, ApplicationError), (
                f"Expected ApplicationError, got {type(cause).__name__}"
            )
            assert cause.non_retryable, (
                "Authorization failures must be non-retryable to prevent "
                "Temporal from retrying the workflow task indefinitely"
            )
