"""What a backup holds follows the layout of `/data`. A new top-level entry must be placed."""

import re
from pathlib import Path

import homeproxy
from homeproxy.backup.layout import EXCLUDED, INCLUDED

REPOSITORY = Path(__file__).resolve().parents[4]
PLACED = {*INCLUDED, *EXCLUDED}


def init_data_entries() -> set[str]:
    """The top-level directories `image/scripts/init-data` makes in `/data`."""
    script = (REPOSITORY / "image" / "scripts" / "init-data").read_text()
    arrays = re.findall(r"^(?:public|private)_dirs=\(([^)]*)\)", script, re.MULTILINE)
    return {name for array in arrays for name in array.split()}


def source_entries() -> set[str]:
    """The top-level entries that backend code names by a `/data/<name>` path."""
    sources = Path(homeproxy.__file__).parent.rglob("*.py")
    found = (re.findall(r'"/data/([\w.-]+)', path.read_text()) for path in sources)
    return {name for names in found for name in names}


def test_every_top_level_entry_of_data_is_in_the_backup_or_left_out_on_purpose() -> None:
    entries = init_data_entries() | source_entries()
    assert {"config", "secrets", "state"} <= entries, "the scan found nothing"
    assert sorted(entries - PLACED) == []


def test_an_entry_is_not_both_included_and_excluded() -> None:
    assert set(INCLUDED) & set(EXCLUDED) == set()
