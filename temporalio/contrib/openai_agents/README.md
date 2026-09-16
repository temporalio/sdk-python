# OpenAI Agents SDK integration

The OpenAI Agents integration has moved to the independently versioned
[`temporalio-openai-agents`](https://pypi.org/project/temporalio-openai-agents/)
package. Its canonical Python API is `temporalio.openai_agents`.

The complete integration guide now lives in the
[`temporalio-openai-agents` README](https://github.com/temporalio/ai-integrations/tree/main/python/openai_agents#readme).

## Migrating to Temporal 1.34

Remove only the `openai-agents` extra from the existing Temporal dependency,
preserve every other Temporal extra, update the Temporal SDK to 1.34.0 or
later, and add the standalone package:

```toml
# Before
dependencies = [
    "temporalio[openai-agents,otel,pydantic]>=1.33.0",
]

# After
dependencies = [
    "temporalio[otel,pydantic]>=1.34.0",
    "temporalio-openai-agents",
]
```

Apply the same transformation regardless of how many extras are installed:
delete only `openai-agents` from the bracketed list, preserve all other extras,
and remove the brackets if no extras remain. Then install the standalone
package directly:

```bash
uv add temporalio-openai-agents
```

Existing public imports continue to work through compatibility modules:

```python
from temporalio.contrib.openai_agents import OpenAIAgentsPlugin
```

Applications can migrate independently to the canonical import:

```python
from temporalio.openai_agents import OpenAIAgentsPlugin
```

The standalone package can coexist with Temporal 1.33 because it installs at
`temporalio.openai_agents`, which does not overlap the SDK's bundled
`temporalio.contrib.openai_agents` implementation. On Temporal 1.33, use the
new canonical import to select the standalone implementation.
