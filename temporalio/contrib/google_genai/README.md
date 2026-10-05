# Google GenAI integration

The Google GenAI integration has moved to the independently versioned
[`temporalio-google-genai`](https://pypi.org/project/temporalio-google-genai/)
package. Its canonical Python API is `temporalio.google_genai`.

The complete integration guide now lives in the
[`temporalio-google-genai` README](https://github.com/temporalio/ai-integrations/tree/main/python/google_genai#readme).

## Migrating to the standalone package

Remove the `google-genai` extra from the existing Temporal dependency:

```toml
# Before
dependencies = [
    "temporalio[google-genai,opentelemetry,pydantic]",
]

# After
dependencies = [
    "temporalio[opentelemetry,pydantic]",
]
```

Remove the brackets if no extras remain. Then install the standalone package directly:

```bash
uv add temporalio-google-genai
```

Change application imports to the standalone package:

```python
from temporalio.google_genai import GoogleGenAIPlugin
```

The new import path selects the standalone implementation instead of the
SDK-bundled `temporalio.contrib.google_genai` implementation.
