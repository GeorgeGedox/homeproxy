"""The files of every earlier release still migrate and validate with the current code.

Each directory in `tests/fixtures/file-versions/` holds the valid files of one release, frozen. A
release that changes a format without a version bump and a migration step fails here. To add a
release, copy its `examples/data` into a new directory of that name.
"""

import shutil
from pathlib import Path

import pytest

from homeproxy.store import Store, migrate_files

FROZEN = Path(__file__).parents[2] / "fixtures" / "file-versions"


@pytest.mark.parametrize("release", sorted(FROZEN.iterdir()), ids=lambda path: path.name)
def test_frozen_files_of_a_release_migrate_and_validate(release: Path, tmp_path: Path) -> None:
    shutil.copytree(release, tmp_path / "data")
    store = Store(tmp_path / "data")
    with store.lock() as held:
        migrate_files(store, held)
    store.load_config()
    store.load_secrets()
    assert {route.type for route in store.load_routes()} == {"http", "tcp", "passthrough"}
