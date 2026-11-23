"""The `/data/generated` and `/data/generated.prev` links that point at releases.

`generated` is the live release that HAProxy and the DNS process read. `generated.prev` exists only
during an apply and points at the last promoted release, which `init-data` and `restore_previous`
link back after a crash.
"""

import os
from pathlib import Path

from homeproxy.util.atomic import fsync_dir

RELEASES = "releases"
GENERATED = "generated"
PREVIOUS = "generated.prev"


def live_release(root: Path) -> Path:
    """Return the release directory `generated` points at."""
    return root / os.readlink(root / GENERATED)


def link(root: Path, release: Path, name: str = GENERATED) -> None:
    """Point a link in `root` at a release with one rename, so a reader never sees a gap."""
    temp = root / f".{name}.tmp"
    temp.unlink(missing_ok=True)
    temp.symlink_to(Path(RELEASES) / release.name)
    os.replace(temp, root / name)
    fsync_dir(root)


def restore_previous(root: Path, *, keep_current: bool) -> bool:
    """Switch `generated` back to `generated.prev` if the old release is still there, and return
    whether it switched."""
    previous = root / PREVIOUS
    if keep_current or not previous.is_symlink() or not previous.resolve().is_dir():
        clear_previous(root)
        return False
    os.replace(previous, root / GENERATED)
    fsync_dir(root)
    return True


def clear_previous(root: Path) -> None:
    """Remove `generated.prev`."""
    (root / PREVIOUS).unlink(missing_ok=True)
    fsync_dir(root)
