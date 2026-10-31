"""Only deliberate business errors may be shown to the API client."""


class BusinessError(ValueError):
    """A validation failure with a message safe to return to the client."""
