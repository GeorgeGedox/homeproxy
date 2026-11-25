"""What a staged backup holds, for the check before a restore. It never holds a secret value."""

import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from homeproxy.certs.bundle import list_certificates
from homeproxy.store.files import Store
from homeproxy.store.snapshots import Snapshots


@dataclass(frozen=True)
class BackupSummary:
    """The facts about a backup that the admin checks before a restore.

    `made_at` is the newest member time in the archive. `config_version` is the file version
    before migration, and `migrated` says that a restore updates the files from an older one.
    """

    made_at: datetime | None
    config_version: int
    migrated: bool
    routes: int
    domains: list[str]
    certificates: int
    snapshots: int
    password_set: bool
    backup_token_set: bool


def newest_member_time(upload: Path) -> datetime | None:
    """Return the newest member time of a valid archive, or None when no member has one."""
    with tarfile.open(upload, "r:gz") as archive:
        newest = max((member.mtime for member in archive), default=0)
    return datetime.fromtimestamp(newest, UTC) if newest > 0 else None


def summarize(
    staged: Store, made_at: datetime | None, config_version: int, *, migrated: bool
) -> BackupSummary:
    """Describe the validated files in `staged`."""
    config = staged.load_config()
    secrets = staged.load_secrets()
    return BackupSummary(
        made_at=made_at,
        config_version=config_version,
        migrated=migrated,
        routes=len(staged.load_routes()),
        domains=[domain.zone for domain in config.domains],
        certificates=len(list_certificates(staged.root / "certs")),
        snapshots=Snapshots(staged).count(),
        password_set=bool(config.dashboard.password_hash.get_secret_value()),
        backup_token_set=secrets.backup_token_hash is not None,
    )
