"""Errors of the backup package. Their text names a file at most, never its content."""


class RestoreRefused(Exception):
    """A restore the package refuses. `status` is the HTTP status: 413 or 422."""

    def __init__(self, status: int, code: str, message: str) -> None:
        """Describe the refusal: HTTP status, a stable code and a message safe to show."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
