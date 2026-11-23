"""The built map and its description in `/data/geoip/`, which each apply renders into a release."""

import logging
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from homeproxy.geoip.sources import Provider
from homeproxy.util.atomic import atomic_write, ensure_dir

logger = logging.getLogger(__name__)

COUNTRY_MAP = "country.map"
"""The release file, and the runtime name of the map that `map_ip(country.map)` loads."""
DATA_DIR = Path("geoip")
"""Relative to the data directory."""
_INFO_FILE = "country.json"

# One search for a bad line. A repeated group over the whole text would need memory per line.
_BAD_LINE = re.compile(r"^(?![0-9a-f.:]+/[0-9]{1,3} [A-Z]{2}$)", re.MULTILINE)


class GeoipData(BaseModel):
    """Where the map in use came from, so the UI can show its age and the right attribution.

    `countries` holds the codes the map can block. A description saved before it existed fails to
    load, which makes the next scheduled check download the data again.
    """

    provider: Provider
    updated_at: datetime
    entries: int
    countries: list[str]


def load_country_map(data_dir: Path) -> str:
    """Return the map the refresh job saved in `data_dir`, or an empty map before the first one.

    A saved map that is not in the builder's format is logged and replaced by the empty map, so an
    apply never fails over a file the admin cannot fix.
    """
    path = data_dir / DATA_DIR / COUNTRY_MAP
    try:
        text = path.read_text(encoding="ascii")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as error:
        logger.warning("cannot read %s, using an empty country map: %s", path, error)
        return ""
    if text and (not text.endswith("\n") or _BAD_LINE.search(text, 0, len(text) - 1)):
        logger.warning("%s is not valid, using an empty country map", path)
        return ""
    return text


def load_data_info(data_dir: Path) -> GeoipData | None:
    """Describe the saved map, or return None when there is none or its description is lost."""
    try:
        return GeoipData.model_validate_json((data_dir / DATA_DIR / _INFO_FILE).read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValidationError) as error:
        logger.warning("cannot read the GeoIP data description: %s", error)
        return None


def save_country_map(data_dir: Path, text: str, info: GeoipData | None) -> None:
    """Save a built map and its description. The caller holds the store's lock.

    With `info` None the description is removed, so the next scheduled check downloads again, and
    the map is kept unless `text` is empty: a rollback to a map whose description failed to load
    must not stop blocking.
    """
    directory = data_dir / DATA_DIR
    if info is None:
        (directory / _INFO_FILE).unlink(missing_ok=True)
        if not text:
            (directory / COUNTRY_MAP).unlink(missing_ok=True)
            return
    ensure_dir(directory)
    atomic_write(directory / COUNTRY_MAP, text.encode("ascii"), mode=0o644)
    if info is not None:
        atomic_write(directory / _INFO_FILE, info.model_dump_json().encode("ascii"), mode=0o644)
