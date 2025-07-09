"""Test for fixing AsyncRootListenersTracer serialization issues."""

import pytest
from datetime import timedelta
from unittest.mock import MagicMock

from temporalio.contrib.langchain import model_as_activity
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from langchain_openai import ChatOpenAI
    from langchain_core.callbacks import AsyncCallbackManager, BaseCallbackHandler


class MockAsyncRootListenersTracer(BaseCallbackHandler):
    """Mock tracer that simulates AsyncRootListenersTracer serialization issues."""

    def __init__(self):
        # Add some non-serializable attributes to simulate the real tracer
        self._internal_state = MagicMock()  # This would be non-serializable

    def __class_getitem__(cls, item):
        # This can cause serialization issues
        raise TypeError("Cannot serialize this tracer")


class SerializableCallbackHandler(BaseCallbackHandler):
    """A callback handler that should be serializable."""

    def __init__(self):
        self.events = []

    def on_llm_start(self, serialized, prompts, **kwargs):
        self.events.append("llm_start")


class TestTracerSerializationFix:
    """Test that non-serializable tracers are filtered out properly."""

    def test_split_callbacks_sends_no_callbacks_to_activities(self):
        """Test that _split_callbacks sends no callbacks to activities to avoid serialization issues."""

        # Create the temporal model proxy
        llm = model_as_activity(
            ChatOpenAI(model="gpt-3.5-turbo"),
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
        )

        # Create a mix of serializable and non-serializable callbacks
        good_callback = SerializableCallbackHandler()
        bad_callback = MockAsyncRootListenersTracer()

        # Set the bad callback's class name to match the real problematic one
        bad_callback.__class__.__name__ = "AsyncRootListenersTracer"
        bad_callback.__class__.__module__ = "langchain_core.tracers.root_listeners"

        callbacks = [good_callback, bad_callback]

        # Test with list of callbacks
        config = {"callbacks": callbacks}
        activity_callbacks, workflow_callbacks = llm._split_callbacks(config)

        # Debug: print what we got
        print(f"Original callbacks: {len(callbacks)}")
        print(f"Activity callbacks: {len(activity_callbacks)}")
        print(f"Workflow callbacks: {len(workflow_callbacks)}")

        # Should send NO callbacks to activities to avoid serialization issues
        assert len(activity_callbacks) == 0
        # All callbacks should become workflow callbacks
        assert len(workflow_callbacks) == len(callbacks)

    def test_split_callbacks_with_tracer_modules_sends_none_to_activities(self):
        """Test that _split_callbacks sends no callbacks to activities regardless of their module."""

        # Create the temporal model proxy
        llm = model_as_activity(
            ChatOpenAI(model="gpt-3.5-turbo"),
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
        )

        # Create callbacks
        good_callback = SerializableCallbackHandler()
        bad_callback = SerializableCallbackHandler()

        # Make the bad callback look like it's from a tracer module
        bad_callback.__class__.__module__ = "langchain_core.tracers.some_tracer_module"

        callbacks = [good_callback, bad_callback]

        # Test _split_callbacks
        config = {"callbacks": callbacks}
        activity_callbacks, workflow_callbacks = llm._split_callbacks(config)

        # Should send no callbacks to activities regardless of their type
        assert len(activity_callbacks) == 0
        assert len(workflow_callbacks) == len(callbacks)

    def test_split_callbacks_with_callback_manager_containing_tracers(self):
        """Test that _split_callbacks properly handles callback managers with tracers."""

        # Create the temporal model proxy
        llm = model_as_activity(
            ChatOpenAI(model="gpt-3.5-turbo"),
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
        )

        # Create a callback manager with mixed handlers
        good_callback = SerializableCallbackHandler()
        bad_callback = MockAsyncRootListenersTracer()
        bad_callback.__class__.__name__ = "AsyncRootListenersTracer"
        bad_callback.__class__.__module__ = "langchain_core.tracers.root_listeners"

        # Create callback manager
        callback_manager = AsyncCallbackManager([good_callback, bad_callback])

        # Test with callback manager in config
        config = {"callbacks": callback_manager}
        activity_callbacks, workflow_callbacks = llm._split_callbacks(config)

        # Should send no callbacks to activities to avoid serialization issues
        assert len(activity_callbacks) == 0
        # All callbacks should become workflow callbacks
        assert (
            len(workflow_callbacks) == 2
        )  # Original workflow callbacks + extracted handlers

    def test_model_call_input_creation_with_no_activity_callbacks(self):
        """Test that ModelCallInput can be created with no activity callbacks."""

        # Create the temporal model proxy
        llm = model_as_activity(
            ChatOpenAI(model="gpt-3.5-turbo"),
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
        )

        # Create a callback manager with problematic tracer
        good_callback = SerializableCallbackHandler()
        bad_callback = MockAsyncRootListenersTracer()
        bad_callback.__class__.__name__ = "AsyncRootListenersTracer"

        callback_manager = AsyncCallbackManager([good_callback, bad_callback])
        config = {"callbacks": callback_manager}

        # Split callbacks
        activity_callbacks, workflow_callbacks = llm._split_callbacks(config)

        # Try to create ModelCallInput with no activity callbacks
        from temporalio.contrib.langchain._simple_wrappers import ModelCallInput

        try:
            activity_input = ModelCallInput(
                model_data=llm._model,  # Use the model directly, not model_dump()
                model_type=f"{type(llm._model).__module__}.{type(llm._model).__qualname__}",
                method_name="ainvoke",
                args=[("human", "test")],
                kwargs={},
                activity_callbacks=activity_callbacks,  # Should be empty
            )

            # Should succeed with no activity callbacks
            assert len(activity_input.activity_callbacks) == 0

            # Try to serialize the entire input
            from temporalio.contrib.pydantic import PydanticPayloadConverter

            converter = PydanticPayloadConverter()
            payload = converter.to_payload(activity_input)
            assert payload is not None

        except Exception as e:
            pytest.fail(
                f"ModelCallInput creation should work with no activity callbacks: {e}"
            )

    def test_no_activity_callbacks_avoids_serialization_issues(self):
        """Test that sending no callbacks to activities avoids serialization issues."""

        # Create a callback that will fail during the serialization test
        class NonSerializableCallback(BaseCallbackHandler):
            def __init__(self):
                # This will cause Pydantic serialization to fail
                self.non_serializable_attr = (
                    lambda x: x
                )  # functions can't be serialized

        # Create the temporal model proxy
        llm = model_as_activity(
            ChatOpenAI(model="gpt-3.5-turbo"),
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
        )

        good_callback = SerializableCallbackHandler()
        bad_callback = NonSerializableCallback()

        callbacks = [good_callback, bad_callback]

        # Test that _split_callbacks sends no callbacks to activities
        config = {"callbacks": callbacks}
        activity_callbacks, workflow_callbacks = llm._split_callbacks(config)

        # Should send no callbacks to activities, avoiding serialization issues
        assert len(activity_callbacks) == 0
        assert len(workflow_callbacks) == len(callbacks)
