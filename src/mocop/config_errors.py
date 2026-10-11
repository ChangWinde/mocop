class ConfigError(ValueError):
    """Raised when monitor configuration is invalid."""

    code = "INVALID_CONFIG"
