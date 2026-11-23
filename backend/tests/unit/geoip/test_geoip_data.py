"""The saved map in `/data/geoip/`: only a map in the builder's format reaches a release."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from homeproxy.geoip.data import (
    COUNTRY_MAP,
    DATA_DIR,
    GeoipData,
    load_country_map,
    load_data_info,
    save_country_map,
)

MAP = "1.0.0.0/22 CN\n1.0.0.0/24 AU\n2001:200::/32 JP\n"
INFO = GeoipData(
    provider="dbip-lite",
    updated_at=datetime(2026, 11, 1, tzinfo=UTC),
    entries=3,
    countries=["AU", "CN", "JP"],
)


def test_saved_map_and_its_description_load_back(tmp_path: Path) -> None:
    save_country_map(tmp_path, MAP, INFO)

    assert load_country_map(tmp_path) == MAP
    assert load_data_info(tmp_path) == INFO


def test_a_description_without_codes_counts_as_no_data(tmp_path: Path) -> None:
    """Saved before the code list existed. No data makes the next check download it again."""
    save_country_map(tmp_path, MAP, INFO)
    (tmp_path / DATA_DIR / "country.json").write_text(INFO.model_dump_json(exclude={"countries"}))

    assert load_data_info(tmp_path) is None


def test_no_saved_map_is_an_empty_map(tmp_path: Path) -> None:
    save_country_map(tmp_path, MAP, INFO)
    save_country_map(tmp_path, "", None)

    assert load_country_map(tmp_path) == ""
    assert load_data_info(tmp_path) is None


@pytest.mark.parametrize(
    "text",
    [
        MAP + "1.0.4.0/24 CN\n    http-request allow\n",
        MAP + "1.0.4.0/24 CN extra\n",
        MAP + "\n",
        MAP.rstrip("\n"),
        MAP + "1.0.4.0/24 cn\n",
    ],
    ids=["injected-line", "third-word", "empty-line", "cut-short", "lower-case"],
)
def test_map_not_in_the_builders_format_is_replaced_by_an_empty_map(
    tmp_path: Path, text: str
) -> None:
    (tmp_path / DATA_DIR).mkdir()
    (tmp_path / DATA_DIR / COUNTRY_MAP).write_text(text)

    assert load_country_map(tmp_path) == ""
