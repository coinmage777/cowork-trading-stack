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
    - Strings: "${VAR_NAME}" -> value from os.environ
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


def load_dotenv(path: str = ".env", override: bool = False) -> int:
    """
    Minimal .env loader (no external dep).

    Reads KEY=VALUE lines, skips comments and blank lines.
    Strips surrounding quotes. Returns count of vars loaded.

    Args:
        path: Path to .env file
        override: If True, overwrite existing os.environ entries

    Returns:
        Number of variables loaded into os.environ
    """
    if not os.path.exists(path):
        return 0
    count = 0
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            # Strip matching surrounding quotes
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if not override and key in os.environ:
                continue
            os.environ[key] = val
            count += 1
    return count


def require(*keys: str) -> dict:
    """
    Validate that required env vars are set; return them as a dict.

    Raises:
        ValueError: with the list of missing keys
    """
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")
    return {k: os.environ[k] for k in keys}
