from typing import Any, Dict
from temporalio import workflow
from temporalio.contrib.langchain import (
    workflow as lc_workflow,
    model_as_activity,
    tool_as_activity,
)
from .simple_activities import (
    char_count_activity,
    simple_weather_check,
    simple_calculation,
    simple_test_activity,
    unique_word_count_activity,
    uppercase_activity,
    greet_activity,
    word_count_activity,
)
from datetime import timedelta

with workflow.unsafe.imports_passed_through():
    from .mocks import DummyChatOpenAI
    from langchain_core.tools import tool
    from .simple_tools import CapitalizeTool


# Simple workflow that doesn't use LangChain imports at class definition time
@workflow.defn(failure_exception_types=[Exception])
class SimpleActivityTestWorkflow:
    """Simple workflow for testing activity execution without LangChain imports."""

    @workflow.run
    async def run(self, test_message: str) -> Dict[str, Any]:
        # Execute our test activity
        result = await workflow.execute_activity(
            simple_test_activity,
            test_message,
            start_to_close_timeout=timedelta(seconds=5),
        )
        return result


@workflow.defn(failure_exception_types=[Exception])
class ActivityAsToolTestWorkflow:
    """Test workflow for activity-as-tool functionality."""

    @workflow.run
    async def run(self, test_city: str) -> Dict[str, Any]:
        # Convert Temporal activities to tool specifications
        weather_tool = lc_workflow.activity_as_tool(
            simple_weather_check, start_to_close_timeout=timedelta(seconds=5)
        )

        calc_tool = lc_workflow.activity_as_tool(
            simple_calculation, start_to_close_timeout=timedelta(seconds=5)
        )

        # Execute the tools (simulating LangChain agent usage)
        # weather_result = await weather_tool["execute"](city=test_city)
        # calc_result = await calc_tool["execute"](x=10.0, y=5.0)
        weather_result = await weather_tool.invoke(city=test_city)
        calc_result = await calc_tool.invoke(x=10.0, y=5.0)

        return {
            "weather_tool_name": weather_tool["name"],
            "weather_result": weather_result,
            "calc_tool_name": calc_tool["name"],
            "calc_result": calc_result,
            "tools_tested": 2,
        }


@workflow.defn(failure_exception_types=[Exception])
class InvokeModelWorkflow:
    """Workflow that uses Temporal-wrapped LLM (should succeed)."""

    @workflow.run
    async def run(self, query: str) -> str:
        llm = model_as_activity(
            DummyChatOpenAI(model="gpt-4o"),
            start_to_close_timeout=timedelta(seconds=30),
        )
        result = await llm.ainvoke(query)
        return str(result)


# Add a failure exception type to the workflow to prevent retries. In this case,
# we want to keep the exception type narrow because we are looking for a specific
# type of failure.
@workflow.defn(failure_exception_types=[RuntimeError])
class UnwrappedInvokeModelWorkflow:
    """Workflow that calls LLM directly (should fail to invoke unwrapped model)."""

    @workflow.run
    async def run(self, query: str) -> str:
        try:
            llm = DummyChatOpenAI()
            result = await llm.ainvoke([("human", query)])
            return result
        except Exception as e:
            raise e


# TODO: support sandboxed=True
@workflow.defn(failure_exception_types=[Exception], sandboxed=False)
class ChainOfFunctionToolsWorkflow:
    """Workflow that chains function tools together."""

    @workflow.run
    async def run(self, query: str) -> str:
        @tool
        async def uppercase_tool(input: str) -> str:
            """Convert text to uppercase."""
            return input.upper()

        @tool
        async def greet_tool(input: str) -> str:
            """Greet someone with a hello message."""
            return f"Hello, {input}!"

        chained_tool = uppercase_tool | greet_tool
        result = await chained_tool.ainvoke(input=query)
        return result


@workflow.defn(failure_exception_types=[Exception])
class ChainOfActivitiesInCodeWorkflow:
    """Workflow that chains activities together."""

    @workflow.run
    async def run(self, query: str) -> str:
        uppercase_tool = lc_workflow.activity_as_tool(
            uppercase_activity,
            start_to_close_timeout=timedelta(seconds=5),
        )
        greet_tool = lc_workflow.activity_as_tool(
            greet_activity,
            start_to_close_timeout=timedelta(seconds=5),
        )
        result1 = await uppercase_tool.invoke(text=query)
        result2 = await greet_tool.invoke(whom=result1)
        return result2


@workflow.defn(failure_exception_types=[Exception], sandboxed=False)
class ChainOfActivitiesWorkflow:
    """Workflow that chains activities together."""

    @workflow.run
    async def run(self, query: str) -> str:
        uppercase_tool = lc_workflow.activity_as_tool(
            uppercase_activity,
            start_to_close_timeout=timedelta(seconds=5),
        )
        greet_tool = lc_workflow.activity_as_tool(
            greet_activity,
            start_to_close_timeout=timedelta(seconds=5),
        )
        chained_tool = uppercase_tool | greet_tool
        result = await chained_tool.ainvoke(input=query)
        return result


@workflow.defn(failure_exception_types=[Exception], sandboxed=False)
class ParallelChainOfActivitiesWorkflow:
    """Workflow that chains activities together in parallel."""

    @workflow.run
    async def run(self, query: str) -> str:
        word_count = lc_workflow.activity_as_tool(
            word_count_activity,
            start_to_close_timeout=timedelta(seconds=5),
        )
        char_count = lc_workflow.activity_as_tool(
            char_count_activity,
            start_to_close_timeout=timedelta(seconds=5),
        )
        unique_word_count = lc_workflow.activity_as_tool(
            unique_word_count_activity,
            start_to_close_timeout=timedelta(seconds=5),
        )
        uppercase_tool = lc_workflow.activity_as_tool(
            uppercase_activity,
            start_to_close_timeout=timedelta(seconds=5),
        )

        @tool
        async def format_merged_report(
            word_stats: Dict[str, Any],
            char_stats: Dict[str, Any],
            unique_stats: Dict[str, Any],
        ) -> str:
            """Format the merged report."""
            return (
                "Parallel Text Analysis Report:\n"
                f"- Total Words: {word_stats['word_count']}\n"
                f"- Total Characters: {char_stats['char_count']}\n"
                f"- Unique Words: {unique_stats['unique_words']}"
            )

        chained_tool = (
            uppercase_tool
            | {
                "word_stats": word_count,
                "char_stats": char_count,
                "unique_stats": unique_word_count,
            }
            | format_merged_report
        )
        result = await chained_tool.ainvoke(input=query)
        return result


@workflow.defn(failure_exception_types=[Exception], sandboxed=False)
class LlmToolChainWorkflow:
    """Workflow that chains LLM tools together."""

    @workflow.run
    async def run(self, query: str) -> str:
        llm = model_as_activity(
            DummyChatOpenAI(model="gpt-4o"),
            start_to_close_timeout=timedelta(seconds=30),
        )
        uppercase = lc_workflow.activity_as_tool(
            uppercase_activity,
            start_to_close_timeout=timedelta(seconds=5),
        )

        chain = llm | uppercase
        result = await chain.ainvoke(input=query)
        return result


@workflow.defn(failure_exception_types=[Exception], sandboxed=False)
class ToolAsActivityWorkflow:
    """Workflow that chains tool as activity together."""

    @workflow.run
    async def run(self, query: str) -> str:
        capitalize_tool = tool_as_activity(
            CapitalizeTool(),
            start_to_close_timeout=timedelta(seconds=5),
        )
        result = await capitalize_tool.ainvoke(input=query)
        return result
