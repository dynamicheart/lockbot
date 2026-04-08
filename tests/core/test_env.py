import os

from lockbot.core.env import get_boolean_env


def set_env_var(key, value):
    os.environ[key] = value


def clear_env_var(key):
    if key in os.environ:
        del os.environ[key]


def test_get_boolean_env_true():
    set_env_var("TEST_VAR", "true")
    result = get_boolean_env("TEST_VAR", "False")
    assert result is True
    clear_env_var("TEST_VAR")


def test_get_boolean_env_one():
    set_env_var("TEST_VAR", "1")
    result = get_boolean_env("TEST_VAR", "False")
    assert result is True
    clear_env_var("TEST_VAR")


def test_get_boolean_env_false():
    set_env_var("TEST_VAR", "false")
    result = get_boolean_env("TEST_VAR", "True")
    assert result is False
    clear_env_var("TEST_VAR")


def test_get_boolean_env_zero():
    set_env_var("TEST_VAR", "0")
    result = get_boolean_env("TEST_VAR", "True")
    assert result is False
    clear_env_var("TEST_VAR")


def test_get_boolean_env_empty():
    set_env_var("TEST_VAR", "")
    result = get_boolean_env("TEST_VAR", "True")
    assert result is False
    clear_env_var("TEST_VAR")


def test_get_boolean_env_not_found():
    result = get_boolean_env("NON_EXISTENT_VAR", "True")
    assert result is True
