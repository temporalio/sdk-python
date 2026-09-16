# OpenAI Agents SDK integration

The OpenAI Agents integration has moved to the independently versioned
[`temporalio-openai-agents`](https://pypi.org/project/temporalio-openai-agents/)
package. Its canonical Python API is `temporalio.openai_agents`.

The complete integration guide now lives in the
[`temporalio-openai-agents` README](https://github.com/temporalio/ai-integrations/tree/main/python/openai_agents#readme).

## Migrating to the standalone package

Remove the `openai-agents` extra from the existing Temporal dependency and add
the standalone package:

```toml
# Before
dependencies = [
    "temporalio[openai-agents,otel,pydantic]",
]

# After
dependencies = [
    "temporalio[otel,pydantic]",
    "temporalio-openai-agents",
]
```

Apply the same transformation regardless of how many extras are installed:
delete `openai-agents` from the bracketed list and remove the brackets if no
extras remain. Then install the standalone package directly:

```bash
uv add temporalio-openai-agents
```

Change application imports to the standalone package:

```python
from temporalio.openai_agents import OpenAIAgentsPlugin
```

The new import path selects the standalone implementation instead of the
SDK-bundled `temporalio.contrib.openai_agents` implementation.
