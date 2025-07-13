from temporalio import workflow
from temporalio.contrib.langchain import model_as_activity, tool_as_activity
from langchain_openai import ChatOpenAI
from typing import Dict, Any
from temporalio.common import timedelta
from .smoke_activities import magic_function, magic_function_activity
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate
from temporalio.contrib.langchain import workflow as lc_workflow


@workflow.defn(failure_exception_types=[Exception], sandboxed=False)
class SimpleOpenAIWorkflow:
    """Workflow that uses OpenAI through LangChain tools."""

    @workflow.run
    async def run(self, user_prompt: str) -> Dict[str, Any]:
        # Create OpenAI LLM as a Temporal activity
        llm = model_as_activity(
            ChatOpenAI(
                model="gpt-3.5-turbo",
                temperature=0.1,
                max_tokens=150,
                timeout=30,
                # Must set api_key to None to avoid serialization (security + errors)
                api_key=None,
            ),
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
        )
        return await llm.ainvoke(user_prompt)


@workflow.defn(failure_exception_types=[Exception], sandboxed=False)
class ActivityAsToolOpenAIWorkflow:
    """Workflow that uses OpenAI and LangChain tools."""

    @workflow.run
    async def run(self, user_prompt: str) -> Dict[str, Any]:
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", "You are a helpful assistant"),
                ("placeholder", "{chat_history}"),
                ("human", "{input}"),
                ("placeholder", "{agent_scratchpad}"),
            ]
        )
        model = model_as_activity(
            ChatOpenAI(
                model="gpt-4o",
                # Must set api_key to None to avoid serialization (security + errors)
                api_key=None,
            ),
            start_to_close_timeout=timedelta(seconds=5),
        )

        tools = [
            # tool_as_activity(
            #     magic_function, start_to_close_timeout=timedelta(seconds=5)
            # ),
            lc_workflow.activity_as_tool(
                magic_function_activity, start_to_close_timeout=timedelta(seconds=5)
            )
        ]

        agent = create_tool_calling_agent(model, tools, prompt)
        agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=False)

        return await agent_executor.ainvoke({"input": user_prompt})


@workflow.defn(failure_exception_types=[Exception], sandboxed=False)
class ToolAsActivityOpenAIWorkflow:
    """Workflow that uses OpenAI and LangChain tools."""

    @workflow.run
    async def run(self, user_prompt: str) -> Dict[str, Any]:
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", "You are a helpful assistant"),
                ("placeholder", "{chat_history}"),
                ("human", "{input}"),
                ("placeholder", "{agent_scratchpad}"),
            ]
        )
        model = model_as_activity(
            ChatOpenAI(
                model="gpt-4o",
                # Must set api_key to None to avoid serialization (security + errors)
                api_key=None,
            ),
            start_to_close_timeout=timedelta(seconds=5),
        )

        tools = [
            tool_as_activity(
                magic_function, start_to_close_timeout=timedelta(seconds=5)
            ),
        ]

        agent = create_tool_calling_agent(model, tools, prompt)
        agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=False)

        return await agent_executor.ainvoke({"input": user_prompt})
