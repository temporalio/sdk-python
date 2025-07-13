"""Test the exact user scenario to debug the Generation error."""

import os
import pytest
from datetime import timedelta
import json

from temporalio import workflow
from temporalio.client import Client
from tests.helpers import new_worker
from temporalio.contrib.langchain import (
    model_as_activity,
    tool_as_activity,
    get_wrapper_activities,
)

from typing import Dict, Any

with workflow.unsafe.imports_passed_through():
    from langchain.agents import AgentExecutor, create_structured_chat_agent
    from langchain_core.chat_history import BaseChatMessageHistory
    from langchain_core.messages import BaseMessage
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
    from langchain_core.runnables.history import RunnableWithMessageHistory
    from langchain_core.tools import BaseTool
    from pydantic import BaseModel, Field
    from langchain_openai import ChatOpenAI


# Skip all tests in this module if integration testing is not enabled
pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY environment variable not set",
)


# Global tracking for search calls
search_call_tracker = {"count": 0, "last_query": ""}


class MockTavilySearch(BaseTool):
    """Mock of TavilySearchResults that replicates the real implementation precisely.

    Matches the interface from langchain_tavily.tavily_search.TavilySearchResults
    """

    name: str = "tavily_search_results_json"
    description: str = (
        "A search engine optimized for comprehensive, accurate, and trusted results. "
        "Useful for when you need to answer questions about current events. "
        "Input should be a search query."
    )

    # Match the real TavilySearchResults constructor parameters
    max_results: int = 3
    search_depth: str = "basic"  # "basic" or "advanced"
    include_domains: list = []
    exclude_domains: list = []
    include_images: bool = False
    include_raw_content: bool = False
    include_answer: bool = False

    def _run(
        self,
        query: str,
        *,
        include_domains: list = None,
        exclude_domains: list = None,
        search_depth: str = None,
        include_images: bool = None,
        include_answer: bool = None,
        include_raw_content: bool = None,
        **kwargs,
    ) -> str:
        """Run the search synchronously.

        Matches the TavilySearchResults._run method signature exactly.
        """
        return self._execute_search(
            query=query,
            include_domains=include_domains or self.include_domains,
            exclude_domains=exclude_domains or self.exclude_domains,
            search_depth=search_depth or self.search_depth,
            include_images=include_images
            if include_images is not None
            else self.include_images,
            include_answer=include_answer
            if include_answer is not None
            else self.include_answer,
            include_raw_content=include_raw_content
            if include_raw_content is not None
            else self.include_raw_content,
        )

    async def _arun(self, **kwargs) -> str:
        """Run the search asynchronously.

        Precisely matches TavilySearchResults._arun behavior while handling
        the Temporal wrapper's argument passing formats:

        1. Positional args: {'args': ['query_string']}
        2. Keyword args: {'kwargs': {'query': '...', 'include_domains': [...]}}
        3. Direct usage: {'query': '...', 'include_domains': [...]}
        """
        # Handle different Temporal wrapper formats
        if "args" in kwargs and kwargs["args"]:
            # LangChain agents often pass query as positional argument
            query = kwargs["args"][0]
            include_domains = None
            exclude_domains = None
            search_depth = None
            include_images = None
            include_answer = None
            include_raw_content = None
        elif "kwargs" in kwargs and isinstance(kwargs["kwargs"], dict):
            # Temporal wrapper with nested kwargs
            actual_kwargs = kwargs["kwargs"]
            query = actual_kwargs.get("query", "general search")
            include_domains = actual_kwargs.get("include_domains")
            exclude_domains = actual_kwargs.get("exclude_domains")
            search_depth = actual_kwargs.get("search_depth")
            include_images = actual_kwargs.get("include_images")
            include_answer = actual_kwargs.get("include_answer")
            include_raw_content = actual_kwargs.get("include_raw_content")
        else:
            # Direct LangChain usage
            query = kwargs.get("query", "general search")
            include_domains = kwargs.get("include_domains")
            exclude_domains = kwargs.get("exclude_domains")
            search_depth = kwargs.get("search_depth")
            include_images = kwargs.get("include_images")
            include_answer = kwargs.get("include_answer")
            include_raw_content = kwargs.get("include_raw_content")

        return self._execute_search(
            query=query,
            include_domains=include_domains or self.include_domains,
            exclude_domains=exclude_domains or self.exclude_domains,
            search_depth=search_depth or self.search_depth,
            include_images=include_images
            if include_images is not None
            else self.include_images,
            include_answer=include_answer
            if include_answer is not None
            else self.include_answer,
            include_raw_content=include_raw_content
            if include_raw_content is not None
            else self.include_raw_content,
        )

    def _execute_search(
        self,
        query: str,
        include_domains: list = None,
        exclude_domains: list = None,
        search_depth: str = "basic",
        include_images: bool = False,
        include_answer: bool = False,
        include_raw_content: bool = False,
    ) -> str:
        """Execute search with parameters matching TavilySearchResults behavior."""
        # Track that the search was called
        search_call_tracker["count"] += 1
        search_call_tracker["last_query"] = query

        # Create mock results matching real Tavily response format
        results = []

        # Generate realistic mock search results (limited by max_results)
        result_templates = [
            {
                "title": f"Comprehensive Guide to {query}",
                "url": f"https://example.com/guide-{query.replace(' ', '-').lower()}",
                "content": f"This is a detailed explanation about {query}. According to Douglas Adams' The Hitchhiker's Guide to the Galaxy, the answer is 42. It covers all the important aspects and provides valuable insights, with 42 being the ultimate answer to life, the universe, and everything.",
                "score": 0.95,
            },
            {
                "title": f"Latest Research on {query}",
                "url": f"https://research.example.com/latest-{query.replace(' ', '-').lower()}",
                "content": f"Recent findings and developments related to {query}. This research provides cutting-edge information, confirming that 42 remains the definitive answer to existential questions about life's purpose.",
                "score": 0.89,
            },
            {
                "title": f"FAQ about {query}",
                "url": f"https://faq.example.com/{query.replace(' ', '-').lower()}",
                "content": f"Frequently asked questions and answers about {query}. Clear and concise explanations for common queries. The most common answer remains 42, as calculated by Deep Thought after 7.5 million years of computation.",
                "score": 0.82,
            },
        ]

        # Apply max_results limit
        results = result_templates[: self.max_results]

        # Add raw_content if requested (matches Tavily behavior)
        if include_raw_content:
            for result in results:
                result["raw_content"] = f"Raw content for {result['title']}"

        # Build response structure matching real Tavily format
        response = {
            "query": query,
            "follow_up_questions": None,
            "answer": None,
            "images": [],
            "results": results,
            "response_time": 0.42,  # Mock response time
        }

        # Add answer if requested
        if include_answer:
            response["answer"] = (
                f"Based on the search results for '{query}', the answer is 42, as established by Douglas Adams in The Hitchhiker's Guide to the Galaxy."
            )

        # Add images if requested
        if include_images:
            response["images"] = [
                f"https://example.com/image1-{query.replace(' ', '-').lower()}.jpg",
                f"https://example.com/image2-{query.replace(' ', '-').lower()}.jpg",
            ]

        return json.dumps(response, indent=2)


class InMemoryHistory(BaseChatMessageHistory, BaseModel):
    """Exact copy of user's history implementation."""

    messages: list[BaseMessage] = Field(default_factory=list)

    def add_messages(self, messages: list[BaseMessage]) -> None:
        self.messages.extend(messages)

    def clear(self) -> None:
        self.messages = []

    async def aget_messages(self) -> list[BaseMessage]:
        return self.messages


store = {}


def get_by_session_id(session_id: str) -> BaseChatMessageHistory:
    """Exact copy of user's function."""
    global store
    if session_id not in store:
        store[session_id] = InMemoryHistory()
    return store[session_id]


@workflow.defn(failure_exception_types=[Exception], sandboxed=False)
class ExactUserWorkflow:
    """Replica of the user's SearchWorkflow."""

    @workflow.run
    async def run(self, search_query: str, session_id: str = "123") -> Dict[str, Any]:
        """Exact copy of user's run method."""

        # User's exact system prompt
        system = """Respond to the human as helpfully and accurately as possible. You have access to the following tools:

{tools}

Use a json blob to specify a tool by providing an action key (tool name) and an action_input key (tool input).

Valid "action" values: "Final Answer" or {tool_names}

Provide only ONE action per $JSON_BLOB, as shown:

```
{{
  "action": $TOOL_NAME,
  "action_input": $INPUT
}}
```

Follow this format:

Question: input question to answer
Thought: consider previous and subsequent steps
Action:
```
$JSON_BLOB
```
Observation: action result
... (repeat Thought/Action/Observation N times)
Thought: I know what to respond
Action:
```
{{
  "action": "Final Answer",
  "action_input": "Final response to human"
}}

Begin! Reminder to ALWAYS respond with a valid json blob of a single action. Use tools if necessary. Respond directly if appropriate. Format is Action:```$JSON_BLOB```then Observation"""

        # User's exact human prompt
        human = """

    {input}

    {agent_scratchpad}

    (reminder to respond in a JSON blob no matter what)"""

        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", system),
                MessagesPlaceholder("chat_history", optional=True),
                ("human", human),
            ]
        )

        # User's exact LLM setup
        llm = model_as_activity(
            ChatOpenAI(
                model="gpt-4o",
                api_key=None,  # Avoid serialization issues - DO NOT REMOVE THIS
                temperature=0.1,
            ),
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=30),
        )

        # Tools setup (with mock instead of real Tavily)
        tools = [
            tool_as_activity(
                MockTavilySearch(),
                start_to_close_timeout=timedelta(minutes=2),
                heartbeat_timeout=timedelta(seconds=15),
            )
        ]

        # User's exact agent creation
        agent = create_structured_chat_agent(
            llm=llm,
            tools=tools,
            prompt=prompt,
        )

        # User's exact AgentExecutor
        agent_executor = AgentExecutor(
            agent=agent,
            tools=tools,
            verbose=False,
            handle_parsing_errors=True,
            max_iterations=10,
        )

        # User's exact message history setup
        agent_with_history = RunnableWithMessageHistory(
            agent_executor,
            get_by_session_id,
            input_messages_key="input",
            history_messages_key="chat_history",
        )

        # User's exact execution - this is where the Generation error occurs
        result = await agent_with_history.ainvoke(
            {"input": search_query}, config={"configurable": {"session_id": session_id}}
        )

        return result


@pytest.mark.asyncio
async def test_user_workflow_scenario_generation_fix(temporal_client: Client):
    """Test the exact user scenario to debug the Generation error."""

    # Reset the search call tracker
    search_call_tracker["count"] = 0
    search_call_tracker["last_query"] = ""

    wrapper_activities = get_wrapper_activities()

    async with new_worker(
        temporal_client,
        ExactUserWorkflow,
        activities=wrapper_activities,
    ) as worker:
        result = await temporal_client.execute_workflow(
            ExactUserWorkflow.run,
            "Search for the latest research on what is the meaning of life according to current scientific studies",
            id=f"exact-user-{os.urandom(8).hex()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(minutes=1),
        )

        # Assert that the result contains 42 (from the mock search results)
        assert (
            "42" in result["output"]
        ), f"Expected '42' in result output: {result['output']}"

        # Assert that the search tool was actually invoked
        assert (
            search_call_tracker["count"] > 0
        ), f"Search tool was never invoked. Call count: {search_call_tracker['count']}"
        assert (
            "meaning of life" in search_call_tracker["last_query"].lower()
        ), f"Search query '{search_call_tracker['last_query']}' doesn't contain expected terms"
