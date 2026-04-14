from typing import Any

from langchain_core.runnables.config import var_child_runnable_config
from langgraph._internal._constants import (
    CONFIG_KEY_CHECKPOINT_NS,
    CONFIG_KEY_SCRATCHPAD,
    CONFIG_KEY_SEND,
)
from langgraph.graph.state import RunnableConfig
from langgraph.pregel._algo import LazyAtomicCounter, PregelScratchpad


def get_langgraph_config() -> dict[str, Any]:
    config = var_child_runnable_config.get() or {}
    configurable = config.get("configurable") or {}
    scratchpad = configurable.get(CONFIG_KEY_SCRATCHPAD)

    return {
        "configurable": {
            CONFIG_KEY_CHECKPOINT_NS: configurable.get(CONFIG_KEY_CHECKPOINT_NS),
            CONFIG_KEY_SCRATCHPAD: {
                "step": getattr(scratchpad, "step", 0),
                "stop": getattr(scratchpad, "stop", 0),
                "resume": list(getattr(scratchpad, "resume", [])),
                "null_resume": scratchpad.get_null_resume() if scratchpad else None,
            },
        }
    }


def set_langgraph_config(config: dict[str, Any]) -> None:
    configurable = config.get("configurable") or {}
    scratchpad = configurable.get(CONFIG_KEY_SCRATCHPAD) or {}
    null_resume_box = [scratchpad.get("null_resume")]

    def get_null_resume(consume: bool = False) -> Any:
        val = null_resume_box[0]
        if consume and val is not None:
            null_resume_box[0] = None
        return val

    var_child_runnable_config.set(
        RunnableConfig(
            {
                "configurable": {
                    CONFIG_KEY_CHECKPOINT_NS: configurable.get(
                        CONFIG_KEY_CHECKPOINT_NS
                    ),
                    CONFIG_KEY_SCRATCHPAD: PregelScratchpad(
                        step=scratchpad.get("step", 0),
                        stop=scratchpad.get("stop", 0),
                        call_counter=LazyAtomicCounter(),
                        interrupt_counter=LazyAtomicCounter(),
                        get_null_resume=get_null_resume,
                        resume=list(scratchpad.get("resume", [])),
                        subgraph_counter=LazyAtomicCounter(),
                    ),
                    CONFIG_KEY_SEND: lambda _: None,
                },
            }
        )
    )
