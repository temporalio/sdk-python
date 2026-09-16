# OpenAI Agents SDK integration

The OpenAI Agents integration has moved to the independently versioned
[`temporalio-openai-agents`](https://pypi.org/project/temporalio-openai-agents/)
package. Its canonical Python API is `temporalio.openai_agents`.

The complete integration guide now lives in the
[`temporalio-openai-agents` README](https://github.com/temporalio/ai-integrations/tree/main/python/openai_agents#readme).

## Migrating to Temporal 1.34

Existing users can keep their complete Temporal extras list, including
`openai-agents`. Update the Temporal SDK version to 1.34.0 or later:

```toml
# Before
dependencies = [
    "temporalio[openai-agents,otel,pydantic]>=1.33.0",
]

# After
dependencies = [
    "temporalio[openai-agents,otel,pydantic]>=1.34.0",
]
```

Starting with Temporal 1.34.0, the `openai-agents` extra installs the
standalone distribution. Existing public imports continue to work through
compatibility modules:

```python
from temporalio.contrib.openai_agents import OpenAIAgentsPlugin
```

Applications can migrate independently to the canonical import:

```python
from temporalio.openai_agents import OpenAIAgentsPlugin
```

New applications may depend on the standalone package directly instead of
using the forwarding extra:

```bash
uv add temporalio-openai-agents
```

The standalone package can coexist with Temporal 1.33 because it installs at
`temporalio.openai_agents`, which does not overlap the SDK's bundled
`temporalio.contrib.openai_agents` implementation. On Temporal 1.33, use the
new canonical import to select the standalone implementation.
