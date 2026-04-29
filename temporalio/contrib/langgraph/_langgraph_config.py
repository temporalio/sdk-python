"""LangGraph configuration management for Temporal workflows."""

# pyright: reportMissingTypeStubs=false

import dataclasses
from typing import Any

from langchain_core.runnables.config import var_child_runnable_config
from langgraph._internal._constants import (
    CONFIG_KEY_CHECKPOINT_ID,
    CONFIG_KEY_CHECKPOINT_MAP,
    CONFIG_KEY_CHECKPOINT_NS,
    CONFIG_KEY_DURABILITY,
    CONFIG_KEY_RESUMING,
    CONFIG_KEY_RUNTIME,
    CONFIG_KEY_SCRATCHPAD,
    CONFIG_KEY_SEND,
    CONFIG_KEY_TASK_ID,
    CONFIG_KEY_THREAD_ID,
)
from langgraph._internal._scratchpad import PregelScratchpad
from langgraph.graph.state import RunnableConfig
from langgraph.pregel._algo import LazyAtomicCounter
from langgraph.runtime import ExecutionInfo, Runtime


def strip_runnable_config(config: RunnableConfig | None) -> RunnableConfig:
    """Return a serializable subset of a RunnableConfig.

    LangGraph injects the active RunnableConfig into user functions as a
    config kwarg. The full object holds non-serializable things (callbacks,
    checkpointer/store/cache handles, pregel send/read callables) that can't
    cross an activity boundary, so we keep only primitive fields and the
    serializable subset of configurable.
    """
    orig = config or {}
    configurable = orig.get("configurable") or {}

    result: RunnableConfig = {
        "tags": list(orig.get("tags") or []),
        "metadata": dict(orig.get("metadata") or {}),
    }
    if run_name := orig.get("run_name"):
        result["run_name"] = run_name
    if run_id := orig.get("run_id"):
        result["run_id"] = run_id
    if (recursion_limit := orig.get("recursion_limit")) is not None:
        result["recursion_limit"] = recursion_limit

    stripped_configurable: dict[str, Any] = {
        key: configurable[key]
        for key in (
            CONFIG_KEY_CHECKPOINT_NS,
            CONFIG_KEY_CHECKPOINT_ID,
            CONFIG_KEY_CHECKPOINT_MAP,
            CONFIG_KEY_THREAD_ID,
            CONFIG_KEY_TASK_ID,
            CONFIG_KEY_RESUMING,
            CONFIG_KEY_DURABILITY,
        )
        if key in configurable
    }
    if stripped_configurable:
        result["configurable"] = stripped_configurable
    return result


def get_langgraph_config() -> dict[str, Any]:
    """Get the current LangGraph runnable config as a serializable dict."""
    config = var_child_runnable_config.get()
    configurable = (config or {}).get("configurable") or {}
    scratchpad = configurable.get(CONFIG_KEY_SCRATCHPAD)
    runtime = configurable.get(CONFIG_KEY_RUNTIME)
    execution_info = getattr(runtime, "execution_info", None)

    stripped = strip_runnable_config(config)
    return {
        **stripped,
        "configurable": {
            **(stripped.get("configurable") or {}),
            CONFIG_KEY_SCRATCHPAD: {
                "step": getattr(scratchpad, "step", 0),
                "stop": getattr(scratchpad, "stop", 0),
                "resume": list(getattr(scratchpad, "resume", [])),
                "null_resume": scratchpad.get_null_resume() if scratchpad else None,
            },
        },
        "context": getattr(runtime, "context", None),
        "previous": getattr(runtime, "previous", None),
        "execution_info": (
            dataclasses.asdict(execution_info) if execution_info else None
        ),
    }


def set_langgraph_config(config: dict[str, Any]) -> Runtime:
    """Restore a LangGraph runnable config from a serialized dict.

    Returns the reconstructed Runtime so callers can re-inject it into the
    user function's kwargs without needing to know the configurable layout.
    """
    configurable = config.get("configurable") or {}
    scratchpad = configurable.get(CONFIG_KEY_SCRATCHPAD) or {}
    null_resume_box = [scratchpad.get("null_resume")]

    def get_null_resume(consume: bool = False) -> Any:
        val = null_resume_box[0]
        if consume and val is not None:
            null_resume_box[0] = None
        return val

    execution_info_dict = config.get("execution_info")
    runtime = Runtime(
        context=config.get("context"),
        stream_writer=lambda _: None,
        previous=config.get("previous"),
        execution_info=(
            ExecutionInfo(**execution_info_dict) if execution_info_dict else None
        ),
    )

    restored_configurable: dict[str, Any] = {
        key: configurable[key]
        for key in (
            CONFIG_KEY_CHECKPOINT_NS,
            CONFIG_KEY_CHECKPOINT_ID,
            CONFIG_KEY_CHECKPOINT_MAP,
            CONFIG_KEY_THREAD_ID,
            CONFIG_KEY_TASK_ID,
            CONFIG_KEY_RESUMING,
            CONFIG_KEY_DURABILITY,
        )
        if key in configurable
    }
    restored_configurable[CONFIG_KEY_SCRATCHPAD] = PregelScratchpad(
        step=scratchpad.get("step", 0),
        stop=scratchpad.get("stop", 0),
        call_counter=LazyAtomicCounter(),
        interrupt_counter=LazyAtomicCounter(),
        get_null_resume=get_null_resume,
        resume=list(scratchpad.get("resume", [])),
        subgraph_counter=LazyAtomicCounter(),
    )
    restored_configurable[CONFIG_KEY_SEND] = lambda _: None
    restored_configurable[CONFIG_KEY_RUNTIME] = runtime

    runnable_config: RunnableConfig = {"configurable": restored_configurable}
    if tags := config.get("tags"):
        runnable_config["tags"] = tags
    if metadata := config.get("metadata"):
        runnable_config["metadata"] = metadata
    if run_name := config.get("run_name"):
        runnable_config["run_name"] = run_name
    if run_id := config.get("run_id"):
        runnable_config["run_id"] = run_id
    if (recursion_limit := config.get("recursion_limit")) is not None:
        runnable_config["recursion_limit"] = recursion_limit

    var_child_runnable_config.set(runnable_config)
    return runtime
