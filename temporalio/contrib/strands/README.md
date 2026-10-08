# Strands Agents integration

The Strands Agents integration has moved to the independently versioned
[`temporalio-strands-agents`](https://pypi.org/project/temporalio-strands-agents/)
package. Its canonical Python API is `temporalio.strands_agents`.

The complete integration guide now lives in the
[`temporalio-strands-agents` README](https://github.com/temporalio/ai-integrations/tree/main/python/strands_agents#readme).

## Migrating to the standalone package

Remove the `strands-agents` extra from the existing Temporal dependency:

```toml
# Before
dependencies = [
    "temporalio[strands-agents,opentelemetry,pydantic]",
]

# After
dependencies = [
    "temporalio[opentelemetry,pydantic]",
]
```

Remove the brackets if no extras remain. Then install the standalone package directly:

```bash
uv add temporalio-strands-agents
```

Change application imports to the standalone package:

```python
from temporalio.strands_agents import StrandsPlugin
```

The new import path selects the standalone implementation instead of the
SDK-bundled `temporalio.contrib.strands` implementation.
