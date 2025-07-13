"""Test for callback manager handling in TemporalModelProxy."""

from datetime import timedelta

from temporalio.contrib.langchain import model_as_activity
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from langchain_openai import ChatOpenAI
    from langchain_core.callbacks import AsyncCallbackManager, BaseCallbackHandler


class TestCallbackManagerHandling:
    """Test that TemporalModelProxy properly handles callback managers."""

    def test_split_callbacks_handles_callback_manager(self):
        """Test that _split_callbacks properly handles AsyncCallbackManager."""

        # Create a test callback handler
        class TestHandler(BaseCallbackHandler):
            def __init__(self):
                self.events = []

            def on_llm_start(self, serialized, prompts, **kwargs):
                self.events.append("llm_start")

        # Create the temporal model proxy
        llm = model_as_activity(
            ChatOpenAI(model="gpt-3.5-turbo"),
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
        )

        # Create a callback manager like LangChain agents do
        handler = TestHandler()
        callback_manager = AsyncCallbackManager([handler])

        # Test with callback manager
        config = {"callbacks": callback_manager}
        activity_callbacks, workflow_callbacks = llm._split_callbacks(config)

        # Should send no callbacks to activities to avoid serialization issues
        assert isinstance(activity_callbacks, list)
        assert len(activity_callbacks) == 0
        # All callbacks should become workflow callbacks
        assert len(workflow_callbacks) == 1
        assert workflow_callbacks[0] is handler

    def test_split_callbacks_handles_callback_list(self):
        """Test that _split_callbacks properly handles list of callbacks."""

        # Create a test callback handler
        class TestHandler(BaseCallbackHandler):
            def __init__(self):
                self.events = []

            def on_llm_start(self, serialized, prompts, **kwargs):
                self.events.append("llm_start")

        # Create the temporal model proxy
        llm = model_as_activity(
            ChatOpenAI(model="gpt-3.5-turbo"),
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
        )

        # Test with list of callbacks
        handler = TestHandler()
        config = {"callbacks": [handler]}
        activity_callbacks, workflow_callbacks = llm._split_callbacks(config)

        # Should send no callbacks to activities to avoid serialization issues
        assert isinstance(activity_callbacks, list)
        assert len(activity_callbacks) == 0
        # All callbacks should become workflow callbacks
        assert len(workflow_callbacks) == 1
        assert workflow_callbacks[0] is handler

    def test_split_callbacks_handles_single_callback(self):
        """Test that _split_callbacks properly handles single callback."""

        # Create a test callback handler
        class TestHandler(BaseCallbackHandler):
            def __init__(self):
                self.events = []

            def on_llm_start(self, serialized, prompts, **kwargs):
                self.events.append("llm_start")

        # Create the temporal model proxy
        llm = model_as_activity(
            ChatOpenAI(model="gpt-3.5-turbo"),
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
        )

        # Test with single callback
        handler = TestHandler()
        config = {"callbacks": handler}
        activity_callbacks, workflow_callbacks = llm._split_callbacks(config)

        # Should send no callbacks to activities to avoid serialization issues
        assert isinstance(activity_callbacks, list)
        assert len(activity_callbacks) == 0
        # All callbacks should become workflow callbacks
        assert len(workflow_callbacks) == 1
        assert workflow_callbacks[0] is handler

    def test_model_call_input_creation_with_callback_manager(self):
        """Test that ModelCallInput can be created with callback manager."""

        # Create a test callback handler
        class TestHandler(BaseCallbackHandler):
            def __init__(self):
                self.events = []

            def on_llm_start(self, serialized, prompts, **kwargs):
                self.events.append("llm_start")

        # Create the temporal model proxy
        llm = model_as_activity(
            ChatOpenAI(model="gpt-3.5-turbo"),
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
        )

        # Create a callback manager like LangChain agents do
        handler = TestHandler()
        callback_manager = AsyncCallbackManager([handler])

        # Test with callback manager in config
        config = {"callbacks": callback_manager}
        activity_callbacks, workflow_callbacks = llm._split_callbacks(config)

        # This should work without errors
        from temporalio.contrib.langchain._simple_wrappers import ModelCallInput

        activity_input = ModelCallInput(
            model_data=llm._model,  # Use the model directly, not model_dump()
            model_type=f"{type(llm._model).__module__}.{type(llm._model).__qualname__}",
            method_name="ainvoke",
            args=[("human", "test")],
            kwargs={},
            activity_callbacks=activity_callbacks,  # This should be empty to avoid serialization issues
        )

        # Should succeed with no activity callbacks
        assert activity_input.activity_callbacks == activity_callbacks
        assert isinstance(activity_input.activity_callbacks, list)
        assert len(activity_input.activity_callbacks) == 0
