# Tenuo Authorization for Temporal

## Introduction

Temporal workflows are a natural fit for AI agents: the workflow is the
reasoning loop, activities are the tools it calls, and child workflows are
sub-agents it delegates to. Tenuo adds cryptographic authorization to this
model so that every agent — and every sub-agent — can only do what its
warrant allows.

```
Control Plane
  │  mints warrant: [lookup_customer, search_kb, call_llm, send_response, escalate]
  │
  ▼
Triage Agent (Workflow, 1-hour warrant, 5 tools)
  ├── lookup_customer activity   ← CRM read
  ├── call_llm activity          ← LLM decides next step
  ├── send_response activity     ← restricted to email + chat
  │
  └──▶ Research Agent (Child Workflow, 60s warrant, 2 tools)
         ├── call_llm activity   ← pinned to gpt-4o-mini
         └── search_kb activity  ← scoped to "support" department
```

**What Tenuo adds to Temporal:**

- **Warrant-scoped tool dispatch.** Each agent workflow carries a signed warrant
  specifying which activities (tools) it can call and with what argument constraints.
- **Delegation with monotonic attenuation.** Sub-agent workflows receive narrower
  warrants — capabilities can only shrink, never expand. A research agent cannot
  call `send_response` even if the parent can.
- **Proof-of-Possession (PoP).** Every tool call is cryptographically signed,
  proving the warrant holder initiated it.
- **Zero-trust by default.** Tools reject calls unless all arguments are explicitly
  declared in the warrant.

## Quick Start

### Installation

```bash
pip install 'temporalio[tenuo]'
```

Or if you already have `temporalio` installed:

```bash
pip install tenuo
```

### Multi-Agent Support System

A triage agent that looks up customer context, delegates research to a
sub-agent, and responds — each agent scoped to only the tools it needs.

**Activities (tools):**

```python
from temporalio import activity

@activity.defn
async def lookup_customer(customer_id: str) -> dict:
    """CRM lookup."""
    ...

@activity.defn
async def search_knowledge_base(query: str, department: str) -> list[dict]:
    """Semantic search over internal docs, scoped to a department."""
    ...

@activity.defn
async def call_llm(prompt: str, model: str, max_tokens: int) -> str:
    """LLM inference — warrant pins the model and token budget."""
    ...

@activity.defn
async def send_response(channel: str, ticket_id: str, message: str) -> str:
    """Send a customer response via the specified channel."""
    ...

@activity.defn
async def escalate_to_human(ticket_id: str, reason: str, priority: str) -> str:
    """Route the ticket to a human agent."""
    ...
```

**Research sub-agent (child workflow):**

```python
from datetime import timedelta
from temporalio import workflow
from tenuo.temporal import tenuo_execute_activity

from .activities import search_knowledge_base, call_llm

@workflow.defn
class ResearchAgent:
    """Sub-agent that can only search docs and call an LLM — nothing else."""

    @workflow.run
    async def run(self, question: str) -> str:
        docs = await tenuo_execute_activity(
            search_knowledge_base,
            args=[question, "support"],
            start_to_close_timeout=timedelta(seconds=15),
        )
        return await tenuo_execute_activity(
            call_llm,
            args=[f"Summarize: {docs}", "gpt-4o-mini", 512],
            start_to_close_timeout=timedelta(seconds=30),
        )
```

**Triage agent (parent workflow) — delegates to the research sub-agent:**

```python
from datetime import timedelta

from temporalio import workflow
from tenuo import Exact, AnyOf
from tenuo.temporal import tenuo_execute_activity, tenuo_execute_child_workflow

from .activities import lookup_customer, call_llm, send_response
from .research_agent import ResearchAgent

@workflow.defn
class TriageAgent:
    @workflow.run
    async def run(self, ticket_id: str, customer_id: str, question: str) -> str:
        customer = await tenuo_execute_activity(
            lookup_customer,
            args=[customer_id],
            start_to_close_timeout=timedelta(seconds=10),
        )

        # Delegate to research sub-agent with a narrower warrant:
        # only search_knowledge_base + call_llm, 60-second TTL.
        summary = await tenuo_execute_child_workflow(
            ResearchAgent.run,
            args=[question],
            tools=["search_knowledge_base", "call_llm"],
            constraints={
                "call_llm": {"model": Exact("gpt-4o-mini")},
                "search_knowledge_base": {"department": AnyOf(["support"])},
            },
            ttl_seconds=60,
        )

        answer = await tenuo_execute_activity(
            call_llm,
            args=[
                f"Customer: {customer['name']}\nResearch: {summary}\nQuestion: {question}",
                "gpt-4o-mini",
                512,
            ],
            start_to_close_timeout=timedelta(seconds=30),
        )

        return await tenuo_execute_activity(
            send_response,
            args=["email", ticket_id, answer],
            start_to_close_timeout=timedelta(seconds=10),
        )
```

> **Note:** The plugin configures sandbox passthrough for `tenuo` and `tenuo_core`
> automatically. You do **not** need `workflow.unsafe.imports_passed_through()`.

**Worker setup:**

```python
import asyncio
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.contrib.tenuo import TenuoPlugin

from tenuo import SigningKey, Warrant, Exact, AnyOf, Wildcard
from tenuo.temporal import TenuoPluginConfig, EnvKeyResolver, execute_workflow_authorized

from .activities import (
    lookup_customer, search_knowledge_base,
    call_llm, send_response, escalate_to_human,
)
from .triage_agent import TriageAgent
from .research_agent import ResearchAgent

async def main():
    # In production, load the key from env/vault instead of generating each time
    signing_key = SigningKey.generate()

    plugin = TenuoPlugin(TenuoPluginConfig(
        key_resolver=EnvKeyResolver(),
        trusted_roots=[signing_key.public_key],
    ))

    client = await Client.connect("localhost:7233", plugins=[plugin])

    async with Worker(
        client,
        task_queue="support-agents",
        workflows=[TriageAgent, ResearchAgent],
        activities=[
            lookup_customer, search_knowledge_base,
            call_llm, send_response, escalate_to_human,
        ],
    ):
        # Mint a warrant for the triage agent — all five tools
        warrant = (
            Warrant.mint_builder()
            .capability("lookup_customer", customer_id=Wildcard())
            .capability("search_knowledge_base",
                query=Wildcard(),
                department=AnyOf(["support", "billing"]),
            )
            .capability("call_llm",
                prompt=Wildcard(),
                model=Exact("gpt-4o-mini"),
                max_tokens=Wildcard(),
            )
            .capability("send_response",
                channel=AnyOf(["email", "chat"]),
                ticket_id=Wildcard(),
                message=Wildcard(),
            )
            .capability("escalate_to_human",
                ticket_id=Wildcard(),
                reason=Wildcard(),
                priority=AnyOf(["low", "medium"]),
            )
            .holder(signing_key.public_key)
            .ttl(3600)
            .mint(signing_key)
        )

        result = await execute_workflow_authorized(
            client,
            TriageAgent.run,
            args=["TICKET-42", "cust-1001", "How do I reset my password?"],
            warrant=warrant,
            key_id="default",
            id="triage-42",
            task_queue="support-agents",
        )
        print(result)

asyncio.run(main())
```

### What Happens at Each Level

| Agent | Warrant scope | What it **can** do | What it **cannot** do |
|-------|--------------|--------------------|-----------------------|
| Triage Agent | 5 tools, 1-hour TTL | All tools, delegates to sub-agents | Call tools not in warrant |
| Research Agent | 2 tools, 60s TTL | `search_knowledge_base` (support only), `call_llm` (gpt-4o-mini) | `send_response`, `escalate_to_human`, `lookup_customer` |

The research agent's warrant is automatically attenuated from the parent — it
**cannot** widen scope, switch to a more expensive model, or access billing
department docs.

## How It Works

### Plugin Setup

`TenuoPlugin` is a Temporal `SimplePlugin` that automatically configures:

1. **Client interceptor:** Injects warrant headers into workflow start requests.
2. **Worker interceptors:** Signs tool calls with PoP (outbound) and verifies authorization (inbound).
3. **Sandbox passthrough:** Adds `tenuo` and `tenuo_core` to the workflow sandbox passthrough list so the native extension loads correctly.

```python
from temporalio.contrib.tenuo import TenuoPlugin
from tenuo.temporal import TenuoPluginConfig, EnvKeyResolver

plugin = TenuoPlugin(TenuoPluginConfig(
    key_resolver=EnvKeyResolver(),
    trusted_roots=[issuer_pubkey],
))

client = await Client.connect("localhost:7233", plugins=[plugin])
```

Register the plugin on `Client.connect(plugins=[...])` only. Workers built from that client inherit the plugin automatically — do not pass it again to `Worker(plugins=[...])`.

### Constraint Types

Warrants use a **closed-world (zero-trust) model** — every activity parameter must
be declared, even if unconstrained. Available constraint types:

| Constraint | What it enforces | Example |
|------------|-----------------|---------|
| `Wildcard()` | Any value (parameter declared but unconstrained) | `prompt=Wildcard()` |
| `Exact(value)` | Must match exactly | `model=Exact("gpt-4o-mini")` |
| `AnyOf([...])` | Must be one of the listed values | `priority=AnyOf(["low", "medium"])` |
| `Subpath(prefix)` | String must start with prefix (path scoping) | `path=Subpath("/data/tenant-a")` |
| `Cidr(network)` | IP must be within a CIDR block (IPv4/IPv6) | `source_ip=Cidr("10.0.0.0/8")` |
| `UrlSafe(...)` | URL with allowed schemes, domains, no private IPs | `url=UrlSafe(allow_domains=["api.stripe.com"])` |
| `Range(min, max)` | Numeric bounds | `max_tokens=Range(1, 1024)` |

See the [Tenuo documentation](https://tenuo.ai/docs) for the full list.

### Key Resolution

`EnvKeyResolver` loads signing keys from `TENUO_KEY_<key_id>` environment variables. Keys are base64 or hex-encoded:

```bash
export TENUO_KEY_default=$(python -c "from tenuo import SigningKey; print(SigningKey.generate().to_base64())")
```

The plugin automatically calls `preload_all()` at worker startup, caching all keys so that `resolve_sync()` never touches `os.environ` inside the workflow sandbox.

For production, use `VaultKeyResolver`, `AWSSecretsManagerKeyResolver`, or `GCPSecretManagerKeyResolver` from `tenuo.temporal`.

### Activity Summaries

The outbound interceptor automatically sets a human-readable summary on every tool dispatch, visible in the Temporal Web UI:

```text
[tenuo.TenuoTemporalPlugin] call_llm
[tenuo.TenuoTemporalPlugin] lookup_customer: triage cust-1001
```

If you pass a `summary` to `tenuo_execute_activity`, it is preserved and prefixed with the plugin ID and tool name.

### Replay Safety

The integration is designed for Temporal replay determinism:

- **PoP timestamps** use `workflow.now()`, not wall-clock time.
- **PoP signatures** are deterministic: same inputs always produce the same output.
- **No non-deterministic calls** (`os.urandom`, `random`, `uuid4`) in the workflow interceptor code path.
- **Key resolution** uses pre-cached keys inside the sandbox, never `os.environ`.

## What the Plugin Handles

| Feature | How |
|---------|-----|
| Sandbox passthrough | Adds `tenuo` and `tenuo_core` to workflow runner passthrough — automatic |
| Client interceptor | Creates `TenuoClientInterceptor` for warrant header injection — automatic |
| Worker interceptors | Registers outbound PoP signer and inbound authorization verifier — automatic |
| Activity auto-discovery | Populates `activity_fns` from the worker's activity list — automatic |
| Key preloading | Calls `preload_all()` on `EnvKeyResolver` at startup — automatic |
| Activity summaries | Prefixes activity summaries with `[tenuo.TenuoTemporalPlugin]` in Web UI — automatic |
| Replay safety | PoP signing uses `workflow.now()`; verified by record-and-replay tests |

All other features (`execute_workflow_authorized`, `tenuo_execute_activity`, delegation,
`AuthorizedWorkflow`, key resolvers, etc.) live in the `tenuo` package and are imported
from `tenuo.temporal`.

## Production

For local development, warrants and keys are created inline (as shown above).
In production, [Tenuo Cloud](https://cloud.tenuo.ai) provides a managed control
plane for warrant issuance, key rotation, audit logging, and policy management.
Workers connect via a connect token — no changes to workflow or activity code:

```bash
export TENUO_CONNECT_TOKEN="tcp_..."
```

Self-hosted deployments can use `VaultKeyResolver`, `AWSSecretsManagerKeyResolver`,
or `GCPSecretManagerKeyResolver` from `tenuo.temporal` for key management without
a managed control plane.

## Further Reading

- [Tenuo documentation](https://tenuo.ai/docs)
- [Tenuo Cloud](https://cloud.tenuo.ai) — managed control plane for warrant issuance, key rotation, and audit
- [Tenuo Temporal guide](https://tenuo.ai/docs/temporal)
- [Tenuo Temporal reference](https://tenuo.ai/docs/temporal-reference)
- [Tenuo GitHub](https://github.com/tenuo-ai/tenuo)
