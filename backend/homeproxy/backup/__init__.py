"""Full backup of `/data`: the archive, the backup token, the check and the restore.

`layout` says which top-level entries of `/data` are in an archive. `archive` builds one. `token`
makes and checks the backup token. `extract` reads an uploaded archive, which is hostile input, into
a staging directory, `restore` validates the staged files and, on a fresh or a set-up install,
moves them into `/data`, and `summary` describes them for the check before a restore.
"""

from homeproxy.backup.archive import archive_name, build_archive
from homeproxy.backup.errors import RestoreRefused
from homeproxy.backup.restore import UPLOAD_NAME, check_backup, restore_backup, staging_dir
from homeproxy.backup.summary import BackupSummary

__all__ = [
    "UPLOAD_NAME",
    "BackupSummary",
    "RestoreRefused",
    "archive_name",
    "build_archive",
    "check_backup",
    "restore_backup",
    "staging_dir",
]
