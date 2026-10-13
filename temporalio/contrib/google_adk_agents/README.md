# Google ADK integration

The Google ADK integration has moved to the independently versioned
[`temporalio-google-adk`](https://pypi.org/project/temporalio-google-adk/)
package. Its canonical Python API is `temporalio.google_adk`.

The complete integration guide now lives in the
[`temporalio-google-adk` README](https://github.com/temporalio/ai-integrations/tree/main/python/google_adk#readme).

## Migrating to the standalone package

Remove the `google-adk` extra from the existing Temporal dependency:

```toml
# Before
dependencies = [
    "temporalio[google-adk,opentelemetry,pydantic]",
]

# After
dependencies = [
    "temporalio[opentelemetry,pydantic]",
]
```

Remove the brackets if no extras remain. Then install the standalone package directly:

```bash
uv add temporalio-google-adk
```

Change application imports to the standalone package:

```python
from temporalio.google_adk import GoogleAdkPlugin
```

The new import path selects the standalone implementation instead of the
SDK-bundled `temporalio.contrib.google_adk_agents` implementation.
