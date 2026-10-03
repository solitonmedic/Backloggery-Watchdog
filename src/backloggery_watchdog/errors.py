class WatchdogError(Exception):
    """Base error with a safe, non-secret message."""


class ConfigurationError(WatchdogError):
    pass


class AuthenticationError(WatchdogError):
    pass


class UpstreamError(WatchdogError):
    pass
