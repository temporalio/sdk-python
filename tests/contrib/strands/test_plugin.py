import typing

import pytest
from botocore.config import Config as BotocoreConfig
from strands.models import Model
from strands.models.bedrock import DEFAULT_READ_TIMEOUT

import temporalio.contrib.strands._plugin as plugin_module
from temporalio.contrib.strands import StrandsPlugin
from temporalio.contrib.strands._model_activity import ModelActivity
from tests.contrib.strands.mock_model import MockModel


def test_default_bedrock_model_disables_botocore_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_configs: list[BotocoreConfig] = []
    model = MockModel([])

    def bedrock_model(*, boto_client_config: BotocoreConfig) -> Model:
        captured_configs.append(boto_client_config)
        return model

    monkeypatch.setattr(plugin_module, "BedrockModel", bedrock_model)

    plugin = StrandsPlugin()
    activities = plugin.activities
    assert isinstance(activities, list)
    model_activity = typing.cast(
        ModelActivity,
        typing.cast(typing.Any, activities[0]).__self__,
    )

    assert model_activity._get_model(None) is model
    assert len(captured_configs) == 1
    assert getattr(captured_configs[0], "read_timeout") == DEFAULT_READ_TIMEOUT
    assert getattr(captured_configs[0], "retries") == {"max_attempts": 0}
