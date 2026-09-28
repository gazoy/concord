class FoliantError(Exception):
    """Base for all protocol rejections. The message is the reason a transaction fails."""


class PolicyViolation(FoliantError):
    pass


class InvalidSignatureError(FoliantError):
    pass


class InvalidUpdate(FoliantError):
    pass


class InsufficientFunds(FoliantError):
    pass


class NotFound(FoliantError):
    pass


class Unauthorized(FoliantError):
    pass
