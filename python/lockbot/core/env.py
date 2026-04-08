"""Environment variable parsing utilities."""

import os


def get_boolean_env(var_name, default_value="False"):
    """
    Get a boolean environment variable with the given name and optional default value.
    """
    true_values = {"true", "1"}
    return os.environ.get(var_name, default_value).strip().lower() in true_values
