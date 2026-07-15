"""Internal shared machinery for the LangChain-family plugins.

This package is shared infrastructure for ``temporalio.contrib.langgraph``,
``temporalio.contrib.langsmith``, and ``temporalio.contrib.deepagents``. It is
NOT a public API: names, modules, and behavior may change without notice.

Import discipline: this ``__init__`` performs no imports, and submodules defer
every third-party import into function bodies, so the package is importable
with none of the LangChain-family distributions installed.
"""
