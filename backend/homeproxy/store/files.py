"""Typed access to the files under the data directory.

Reads need no lock, because writes are atomic. A read-modify-write holds `Store.lock()` from the
read to the write, so a second writer waits and no update is lost.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import TypeAdapter, ValidationError
from ruamel.yaml.comments import CommentedMap

from homeproxy.models import Config, Route, Secrets, parse_route, safe_errors
from homeproxy.models.fields import Name
from homeproxy.store.audit import AuditLog
from homeproxy.store.errors import InvalidRouteName, ValidationFailed
from homeproxy.store.lock import HeldLock, file_lock
from homeproxy.store.secrets import register_model_secrets
from homeproxy.store.tree import load_tree, write_tree
from homeproxy.util.atomic import ensure_dir

_ROUTE_NAME: TypeAdapter[str] = TypeAdapter(Name)


def _checked[T](file: str, validate: Callable[[object], T], tree: object) -> T:
    """Run a model's validation, and raise any failure without the pydantic error attached."""
    failure: ValidationFailed
    try:
        return validate(tree)
    except ValidationError as error:
        failure = ValidationFailed(file, safe_errors(error))
    raise failure


def validate_config(tree: object, file: str = "config.yaml") -> Config:
    """Validate data as `config.yaml` and register its secrets. Raises `ValidationFailed`."""
    config = _checked(file, Config.model_validate, tree)
    register_model_secrets(config)
    return config


def validate_route(tree: object, file: str) -> Route:
    """Validate data as one route file. Raises `ValidationFailed`.

    Rules that need other files, such as the route name matching the file name, are not checked.
    """
    return _checked(file, parse_route, tree)


def validate_secrets(tree: object, file: str = "secrets.yaml") -> Secrets:
    """Validate data as `secrets.yaml` and register its secrets. Raises `ValidationFailed`."""
    secrets = _checked(file, Secrets.model_validate, tree)
    register_model_secrets(secrets)
    return secrets


class Store:
    """The config, route, secrets and audit files under one data directory."""

    def __init__(self, root: Path) -> None:
        """Use `root` as the data directory. Nothing is created until the first write."""
        self.root = root
        self.config_dir = root / "config"
        self.config_path = self.config_dir / "config.yaml"
        self.routes_dir = self.config_dir / "routes"
        self.secrets_dir = root / "secrets"
        self.secrets_path = self.secrets_dir / "secrets.yaml"
        self.lock_path = root / "store.lock"
        self.migration_backups_dir = root / "migration-backups"
        self.revoked_sessions_path = root / "state" / "revoked-sessions.json"
        self.audit = AuditLog(root / "audit")

    @contextmanager
    def lock(self, timeout: float | None = None) -> Iterator[HeldLock]:
        """Hold the store's lock, waiting for any other holder, or raise `LockTimeout` after
        `timeout` seconds. See `file_lock` for the limits.

        Every store write needs the `HeldLock` this yields. The lock lives outside `config/`, so it
        never appears in a snapshot or in the config hash.
        """
        ensure_dir(self.root)
        with file_lock(self.lock_path, timeout) as held:
            yield held

    def route_path(self, name: str) -> Path:
        """Return the file of a route. A name that is not a valid route name raises an error."""
        failed = False
        try:
            _ROUTE_NAME.validate_python(name)
        except ValidationError:
            failed = True
        if failed:
            raise InvalidRouteName()
        return self.routes_dir / f"{name}.yaml"

    def route_files(self) -> list[Path]:
        """Return the route files sorted by name, skipping hidden files such as editor backups."""
        return sorted(
            path for path in self.routes_dir.glob("*.yaml") if not path.name.startswith(".")
        )

    def configured(self) -> bool:
        """Whether `config.yaml` exists. Before setup it does not, and that is not an error."""
        return self.config_path.exists()

    def load_config(self) -> Config:
        """Load, validate and register `config.yaml`."""
        return validate_config(load_tree(self.config_path), self.config_path.name)

    def load_routes(self) -> list[Route]:
        """Load and validate every route, sorted by file name."""
        return [validate_route(load_tree(path), path.name) for path in self.route_files()]

    def load_secrets(self) -> Secrets:
        """Load, validate and register `secrets.yaml`."""
        return validate_secrets(load_tree(self.secrets_path), self.secrets_path.name)

    def save_config(self, tree: CommentedMap, lock: HeldLock) -> None:
        """Validate a tree and write it as `config.yaml`. Nothing is written on a failure."""
        lock.require(self.lock_path)
        validate_config(tree, self.config_path.name)
        ensure_dir(self.config_dir)
        write_tree(self.config_path, tree)

    def save_secrets(self, tree: CommentedMap, lock: HeldLock) -> None:
        """Validate a tree and write it as `secrets.yaml`, always with mode 0600.

        New values are registered before the write. Nothing is written on a failure.
        """
        lock.require(self.lock_path)
        validate_secrets(tree, self.secrets_path.name)
        ensure_dir(self.secrets_dir)
        write_tree(self.secrets_path, tree, mode=0o600)
