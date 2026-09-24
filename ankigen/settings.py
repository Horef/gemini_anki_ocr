"""Configuration lookup: environment variables first, then the local config.py."""
import os


def setting(name, default=None):
    value = os.environ.get(name)
    if value:
        return value
    try:
        import config
    except ImportError:
        return default
    value = getattr(config, name, None)
    return default if value in (None, '') else value
