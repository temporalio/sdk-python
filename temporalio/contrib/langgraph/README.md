# LangGraph integration

The LangGraph integration has moved to the independently versioned
[`temporalio-langgraph`](https://pypi.org/project/temporalio-langgraph/)
package. Its canonical Python API is `temporalio.langgraph`.

The complete integration guide now lives in the
[`temporalio-langgraph` README](https://github.com/temporalio/ai-integrations/tree/main/python/langgraph#readme).

## Migrating to the standalone package

Remove the `langgraph` extra from the existing Temporal dependency:

```toml
# Before
dependencies = [
    "temporalio[langgraph,opentelemetry,pydantic]",
]

# After
dependencies = [
    "temporalio[opentelemetry,pydantic]",
]
```

Remove the brackets if no extras remain. Then install the standalone package directly:

```bash
uv add temporalio-langgraph
```

Change application imports to the standalone package:

```python
from temporalio.langgraph import LangGraphPlugin
```

The new import path selects the standalone implementation instead of the
SDK-bundled `temporalio.contrib.langgraph` implementation.
