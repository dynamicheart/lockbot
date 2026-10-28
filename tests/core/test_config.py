"""Per-instance configuration validation and isolation."""

import pytest
from lockbot.core.config import Config, ConfigValidationError


@pytest.mark.parametrize("kind", ["NODE", "QUEUE"])
def test_node_list_is_normalized(kind):
    cfg = Config({"BOT_TYPE": kind, "CLUSTER_CONFIGS": ["n1", "n2"]})
    cfg.validate_config()
    assert cfg.get_val("CLUSTER_CONFIGS") == {"n1": "n1", "n2": "n2"}


@pytest.mark.parametrize("kind", ["DEVICE", "QUEUE_DEVICE"])
def test_device_config(kind):
    cfg = Config({"BOT_TYPE": kind, "CLUSTER_CONFIGS": {"n1": ["GPU", "GPU"]}})
    cfg.validate_config()
    assert cfg.get_val("BOT_TYPE") == kind


@pytest.mark.parametrize(
    "values",
    [
        {"BOT_TYPE": "INVALID"},
        {"BOT_TYPE": "NODE", "CLUSTER_CONFIGS": {"n1": ["GPU"]}},
        {"BOT_TYPE": "NODE", "CLUSTER_CONFIGS": 12},
        {"BOT_TYPE": "QUEUE", "CLUSTER_CONFIGS": "invalid"},
        {"BOT_TYPE": "DEVICE", "CLUSTER_CONFIGS": {"n1": "GPU"}},
        {"BOT_TYPE": "DEVICE", "CLUSTER_CONFIGS": "invalid"},
    ],
)
def test_invalid_config(values):
    with pytest.raises(ConfigValidationError):
        Config(values).validate_config()


def test_defaults_are_not_shared_or_loaded_from_global_environment(monkeypatch):
    monkeypatch.setenv("TOKEN", "unrelated-service-secret")
    one, two = Config(), Config()
    one.get_val("DEFAULT_USER_INFO")["user_id"] = "changed"
    one.get_val("ALLOWED_GROUP_IDS").append("private-group")
    assert two.get_val("DEFAULT_USER_INFO")["user_id"] == "xxx"
    assert two.get_val("ALLOWED_GROUP_IDS") == []
    assert two.get_val("TOKEN") == ""
    assert not hasattr(Config, "load_from_file")
    assert not hasattr(Config, "load_from_env")
