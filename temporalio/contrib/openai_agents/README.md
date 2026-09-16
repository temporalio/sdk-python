# OpenAI Agents SDK integration

The OpenAI Agents integration has moved to the independently versioned
[`temporalio-openai-agents`](https://pypi.org/project/temporalio-openai-agents/)
package. Its canonical Python API is `temporalio.openai_agents`.

The complete integration guide now lives in the
[`temporalio-openai-agents` README](https://github.com/temporalio/ai-integrations/tree/main/python/openai_agents#readme).

## Migrating to the standalone package

Remove only the `openai-agents` extra from the existing Temporal dependency,
preserve every other Temporal extra and the existing Temporal version
constraint, and add the standalone package:

```toml
# Before
dependencies = [
    "temporalio[openai-agents,otel,pydantic]>=1.33.0",
]

# After
dependencies = [
    "temporalio[otel,pydantic]>=1.33.0",
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

Change application imports to the standalone package:

```python
from temporalio.openai_agents import OpenAIAgentsPlugin
```

The standalone package can coexist with Temporal 1.33 because it installs at
`temporalio.openai_agents`, which does not overlap the SDK's bundled
`temporalio.contrib.openai_agents` implementation. The new import path selects
the standalone implementation on both Temporal 1.33 and 1.34 or later.
