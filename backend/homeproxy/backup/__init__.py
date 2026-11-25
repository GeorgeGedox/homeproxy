"""Full backup of `/data`: the archive, the backup token and the restore on a fresh install.

`layout` says which top-level entries of `/data` are in an archive. `archive` builds one. `token`
makes and checks the backup token. `extract` reads an uploaded archive, which is hostile input, into
a staging directory, and `restore` validates the staged files and moves them into `/data`.
"""

from homeproxy.backup.archive import archive_name, build_archive
from homeproxy.backup.errors import RestoreRefused
from homeproxy.backup.restore import UPLOAD_NAME, restore_backup, staging_dir

__all__ = [
    "UPLOAD_NAME",
    "RestoreRefused",
    "archive_name",
    "build_archive",
    "restore_backup",
    "staging_dir",
]
