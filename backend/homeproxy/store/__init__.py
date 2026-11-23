"""The only writer of `/data/config`, `/data/secrets` and `/data/audit`.

`Store` loads and saves the YAML files, `AuditLog` is the audit log and `migrate_files` upgrades old
file versions. To change a file, hold `Store.lock()`, load its tree with `load_tree`, edit the tree
and pass it to a `Store.save_*` method.

Errors from this package never quote a file, so a secret in a broken `secrets.yaml` cannot reach a
log. Logging `str(error)` is safe. Loading `config.yaml` or `secrets.yaml` registers their secrets
for redaction.
"""

from homeproxy.store.audit import AuditEntry, AuditLog
from homeproxy.store.errors import (
    InvalidRouteName,
    LockNotHeld,
    MigrationError,
    ParseError,
    SerializeError,
    StoreError,
    ValidationFailed,
)
from homeproxy.store.files import Store, validate_config, validate_route, validate_secrets
from homeproxy.store.lock import HeldLock, file_lock
from homeproxy.store.migrate import CURRENT_VERSION, MIGRATIONS, migrate_files, migrate_tree
from homeproxy.store.secrets import register_model_secrets
from homeproxy.store.tree import dump_tree, load_tree, parse_tree, write_tree

__all__ = [
    "CURRENT_VERSION",
    "MIGRATIONS",
    "AuditEntry",
    "AuditLog",
    "HeldLock",
    "InvalidRouteName",
    "LockNotHeld",
    "MigrationError",
    "ParseError",
    "SerializeError",
    "Store",
    "StoreError",
    "ValidationFailed",
    "dump_tree",
    "file_lock",
    "load_tree",
    "migrate_files",
    "migrate_tree",
    "parse_tree",
    "register_model_secrets",
    "validate_config",
    "validate_route",
    "validate_secrets",
    "write_tree",
]
