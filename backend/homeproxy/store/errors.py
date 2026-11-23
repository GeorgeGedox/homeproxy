"""Store errors. Their text and attributes never hold anything from the content of a file.

ruamel's errors print source lines and pydantic's print input values, so either one can show a
secret. The store raises these errors outside the `except` block, which leaves `__context__` and
`__cause__` empty, so a traceback cannot show the original error either.
"""

from homeproxy.models import SafeError


class StoreError(Exception):
    """Base class for errors raised by the store. Safe to log and to return."""


class ParseError(StoreError):
    """A file is not YAML, or its top level is not a mapping."""

    def __init__(
        self,
        file: str,
        line: int | None = None,
        column: int | None = None,
        reason: str = "invalid YAML",
    ) -> None:
        where = "" if line is None else f" at line {line}, column {column}"
        super().__init__(f"cannot parse {file!r}{where}: {reason}")


class ValidationFailed(StoreError):
    """A file does not match its model. `errors` comes from `safe_errors`."""

    def __init__(self, file: str, errors: list[SafeError]) -> None:
        details = "; ".join(
            f"{'.'.join(str(segment) for segment in error['loc'])}: {error['msg']}"
            for error in errors
        )
        super().__init__(f"{file!r} failed validation: {details}")
        self.errors = errors


class SerializeError(StoreError):
    """A tree holds a value that cannot be written as YAML."""

    def __init__(self, file: str) -> None:
        super().__init__(f"cannot serialize {file!r}")


class InvalidRouteName(StoreError):
    """A route name that cannot be a file name. The name itself is not repeated."""

    def __init__(self) -> None:
        super().__init__("invalid route name")


class LockNotHeld(StoreError):
    """A write was attempted with a lock that was released or belongs to another store."""


class MigrationError(StoreError):
    """A file cannot be migrated: its version is unusable, or a migration step failed."""
