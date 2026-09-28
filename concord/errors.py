class ConcordError(Exception):
    """Base for all protocol rejections. The message is the reason a transaction fails."""


class PolicyViolation(ConcordError):
    pass


class InvalidSignatureError(ConcordError):
    pass


class InvalidUpdate(ConcordError):
    pass


class InsufficientFunds(ConcordError):
    pass


class NotFound(ConcordError):
    pass


class Unauthorized(ConcordError):
    pass
