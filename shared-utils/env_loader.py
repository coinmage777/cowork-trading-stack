"""
Environment variable loader for config substitution.
Resolves ${ENV_VAR} references in nested config dictionaries.
"""

import os
import re


def resolve_env_vars(obj):
    """
    Recursively resolve ${ENV_VAR} references in config values.

    Supports:
    - Strings: "${VAR_NAME}" → value from os.environ
    - Dicts: recursively resolve all values
    - Lists: recursively resolve all items
    - Other types: return as-is

    Args:
        obj: Config object (dict, list, str, or other)

    Returns:
        Config object with all ${ENV_VAR} references replaced

    Raises:
        ValueError: If a referenced environment variable is not set
    """
    if isinstance(obj, str):
        # Match ${VAR_NAME} pattern
        pattern = re.compile(r'\$\{(\w+)\}')

        def replacer(match):
            key = match.group(1)
            val = os.environ.get(key)
            if val is None:
                raise ValueError(
                    f"Environment variable '{key}' not set. "
                    f"Check your .env file or export it: export {key}=value"
                )
            return val

        return pattern.sub(replacer, obj)
    elif isinstance(obj, dict):
        return {k: resolve_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [resolve_env_vars(item) for item in obj]

    # Return other types as-is (int, float, bool, None, etc.)
    return obj
