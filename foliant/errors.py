class FoliantError(Exception):
    """Base for all protocol rejections. The message is the reason a transaction fails."""


class PolicyViolation(FoliantError):
    """A policy check failed. `code` is the stable reason code from the spending-policy
    specification (docs/spec/spending-policy.md §3); the message is for humans."""

    def __init__(self, message: str, code: str = "policy_violation"):
        super().__init__(message)
        self.code = code


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
