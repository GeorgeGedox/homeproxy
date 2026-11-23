"""Outbound HTTP requests, all named HomeProxy.

Some sites answer Python's default user agent with 403: Cloudflare's IP lists and db-ip.com did
in November 2026, which silently broke the range refresh and the GeoIP download. The CrowdSec LAPI
needs a `name/version` agent: it refuses a bare name as "incorrect Username or Password".
"""

import importlib.metadata
import urllib.request
from http.client import HTTPResponse

USER_AGENT = f"HomeProxy/{importlib.metadata.version('homeproxy')}"


def urlopen(request: str | urllib.request.Request, timeout: float) -> HTTPResponse:
    """Open `request` like `urllib.request.urlopen`, sending HomeProxy's user agent. A redirect
    never carries the `Authorization` header."""
    if isinstance(request, str):
        request = urllib.request.Request(request)  # noqa: S310 - callers pass https or test URLs
    request.add_header("User-Agent", USER_AGENT)
    token = request.get_header("Authorization")
    if token is not None:
        # urllib copies headers to a redirect's target, even on another host; this one stays.
        request.remove_header("Authorization")
        request.add_unredirected_header("Authorization", token)
    response: HTTPResponse = urllib.request.urlopen(request, timeout=timeout)  # noqa: S310, TID251
    return response
