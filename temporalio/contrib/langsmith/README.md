# LangSmith integration

The LangSmith integration has moved to the independently versioned
[`temporalio-langsmith`](https://pypi.org/project/temporalio-langsmith/)
package. Its canonical Python API is `temporalio.langsmith`.

The complete integration guide now lives in the
[`temporalio-langsmith` README](https://github.com/temporalio/ai-integrations/tree/main/python/langsmith#readme).

## Migrating to the standalone package

Remove the `langsmith` extra from the existing Temporal dependency:

```toml
# Before
dependencies = [
    "temporalio[langsmith,opentelemetry,pydantic]",
]

# After
dependencies = [
    "temporalio[opentelemetry,pydantic]",
]
```

Remove the brackets if no extras remain. Then install the standalone package directly:

```bash
uv add temporalio-langsmith
```

Change application imports to the standalone package:

```python
from temporalio.langsmith import LangSmithPlugin
```

The new import path selects the standalone implementation instead of the
SDK-bundled `temporalio.contrib.langsmith` implementation.
