"""Simple tools for testing."""

from temporalio import activity
from langchain_core.tools import BaseTool


class CapitalizeTool(BaseTool):
    """Simple tool for capitalizing text."""

    name: str = "capitalize"
    description: str = "Capitalize the input as title case"

    def _run(self, input: str) -> str:
        """Capitalize the input text."""
        if not activity.in_activity():
            raise RuntimeError("Not in activity")
        return input.title()
