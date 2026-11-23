"""Reading Cloudflare range lists, as fetched and as the refresh job saved them."""

from pathlib import Path

import pytest

from homeproxy.generator.cloudflare_ranges import (
    CACHE_FILE,
    DEFAULT_RANGES,
    RangesError,
    load_ranges,
    parse_ranges,
)


def test_list_with_both_families_and_blank_lines_parses() -> None:
    ranges = parse_ranges("173.245.48.0/20\n\n2400:cb00::/32")
    assert ranges == ("173.245.48.0/20", "2400:cb00::/32")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "0.0.0.0/0\n",
        "0.0.0.0/1\n128.0.0.0/1\n",
        "2000::/3\n",
        "192.168.0.0/16\n",
        "173.245.48.1/20\n",
        "173.245.48.0/20\n    http-request allow\n",
        "<html>captive portal</html>\n",
    ],
    ids=[
        "empty",
        "catch-all",
        "two-halves-of-ipv4",
        "short-ipv6-prefix",
        "private",
        "host-bits",
        "directive",
        "html",
    ],
)
def test_list_that_is_not_only_ranges_is_refused(text: str) -> None:
    with pytest.raises(RangesError):
        parse_ranges(text)


def test_built_in_ranges_are_valid() -> None:
    assert parse_ranges("\n".join(DEFAULT_RANGES)) == DEFAULT_RANGES


def test_saved_ranges_are_loaded(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / CACHE_FILE).write_text("1.1.1.0/24\n")
    assert load_ranges(tmp_path) == ("1.1.1.0/24",)


@pytest.mark.parametrize(
    "saved", [None, b"not a range\n", b"\xff\n"], ids=["missing", "not-a-range", "binary"]
)
def test_missing_or_broken_saved_ranges_fall_back_to_the_built_in_ones(
    tmp_path: Path, saved: bytes | None
) -> None:
    if saved is not None:
        (tmp_path / "state").mkdir()
        (tmp_path / CACHE_FILE).write_bytes(saved)
    assert load_ranges(tmp_path) == DEFAULT_RANGES
