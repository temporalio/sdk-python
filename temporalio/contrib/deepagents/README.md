# Deep Agents integration

The Deep Agents integration has moved to the independently versioned
[`temporalio-deepagents`](https://pypi.org/project/temporalio-deepagents/)
package. Its canonical Python API is `temporalio.deepagents`.

The complete integration guide now lives in the
[`temporalio-deepagents` README](https://github.com/temporalio/ai-integrations/tree/main/python/deepagents#readme).

## Migrating to the standalone package

Remove the `deepagents` extra from the existing Temporal dependency:

```toml
# Before
dependencies = [
    "temporalio[deepagents,opentelemetry,pydantic]",
]

# After
dependencies = [
    "temporalio[opentelemetry,pydantic]",
]
```

Remove the brackets if no extras remain. Then install the standalone package directly:

```bash
uv add temporalio-deepagents
```

Change application imports to the standalone package:

```python
from temporalio.deepagents import DeepAgentsPlugin
```

The new import path selects the standalone implementation instead of the
SDK-bundled `temporalio.contrib.deepagents` implementation.
