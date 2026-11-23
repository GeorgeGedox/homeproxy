"""The daily refresh of `cloudflare.lst`, the Cloudflare edge ranges HAProxy trusts."""

import http.client
import os
import urllib.error
from collections.abc import Sequence
from datetime import timedelta

from homeproxy.generator.cloudflare_ranges import (
    CACHE_FILE,
    CLOUDFLARE_LIST,
    RangesError,
    parse_ranges,
    ranges_file,
)
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.jobs.runner import JobRunner
from homeproxy.store import Store
from homeproxy.util import http as http_request
from homeproxy.util.atomic import atomic_write, ensure_dir

KIND = "cloudflare.ranges"
INTERVAL = timedelta(days=1)
RANGES_URL_ENV = "HOMEPROXY_CF_RANGES_URL"
"""Where the lists `ips-v4` and `ips-v6` are, in place of Cloudflare's site. Tests set it."""
DEFAULT_RANGES_URL = "https://www.cloudflare.com"
FETCH_TIMEOUT = 30.0
_MAX_LIST_SIZE = 65536


def range_urls() -> tuple[str, ...]:
    """Return the URLs of Cloudflare's IPv4 and IPv6 lists, from `RANGES_URL_ENV` if it is set."""
    base = os.environ.get(RANGES_URL_ENV, DEFAULT_RANGES_URL).rstrip("/")
    return (f"{base}/ips-v4", f"{base}/ips-v6")


def fetch_ranges(urls: Sequence[str]) -> tuple[str, ...]:
    """Return the ranges of every list. Raises `RangesError` if any list is missing or not valid."""
    ranges: list[str] = []
    for url in urls:
        try:
            with http_request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
                data = response.read(_MAX_LIST_SIZE + 1)
            text = data.decode("ascii")
        except urllib.error.HTTPError as error:
            raise RangesError(f"{url} answered HTTP {error.code}") from None
        except OSError:
            raise RangesError(f"{url} could not be read, check the internet connection") from None
        except (ValueError, http.client.HTTPException):
            raise RangesError(f"{url} sent an answer HomeProxy could not read") from None
        if len(data) > _MAX_LIST_SIZE:
            # A list cut short would leave some of Cloudflare's edge untrusted.
            raise RangesError(f"{url} is larger than {_MAX_LIST_SIZE} bytes")
        try:
            ranges += parse_ranges(text)
        except RangesError as error:
            raise RangesError(f"{url}: {error}") from None
    return tuple(ranges)


def refresh(store: Store, client: HAProxyClient, urls: Sequence[str] | None = None) -> None:
    """Fetch Cloudflare's ranges, keep them for the next apply and swap them into HAProxy.

    A failed fetch changes nothing. HAProxy's ACL is replaced at runtime and then written to the
    live release, so a reload keeps it. A live release without `cloudflare.lst` does not use the
    ranges, so it gets them at the next apply. Holds the pipeline's lock, so an apply never renders
    a list that this run is about to change.
    """
    ranges = fetch_ranges(range_urls() if urls is None else urls)
    data = ranges_file(ranges).encode("ascii")
    with store.lock():
        cache = store.root / CACHE_FILE
        ensure_dir(cache.parent)
        atomic_write(cache, data, mode=0o644)
        live = (store.root / "generated").resolve() / CLOUDFLARE_LIST
        if not live.is_file() or live.read_bytes() == data:
            return
        client.replace_acl(CLOUDFLARE_LIST, ranges)
        atomic_write(live, data, mode=0o644)


def schedule_refresh(
    runner: JobRunner, store: Store, client: HAProxyClient, urls: Sequence[str] | None = None
) -> None:
    """Refresh now, then once a day."""
    runner.submit(KIND, lambda: refresh(store, client, urls))
    runner.schedule_every(KIND, lambda: refresh(store, client, urls), interval=INTERVAL)
