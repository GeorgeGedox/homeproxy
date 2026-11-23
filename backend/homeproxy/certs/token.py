"""Check that a Cloudflare token can do what DNS-01 needs, and say what is missing if not.

lego reports a token without Zone:Read as a zone it cannot find, which does not tell the user
what to fix. This check names the missing permission instead.
"""

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from homeproxy.certs.errors import TokenError
from homeproxy.util import http as http_request
from homeproxy.util.redact import register_secret

BASE_URL_ENV = "CLOUDFLARE_BASE_URL"
"""lego reads the same variable, so a test or a mock points both at the same API."""
DEFAULT_BASE_URL = "https://api.cloudflare.com/client/v4"
TIMEOUT = 15.0

_NEEDS = "It needs Zone:Zone:Read and Zone:DNS:Edit on this zone."


def _get(path: str, token: str) -> tuple[int, dict[str, Any]]:
    """Return the HTTP status and JSON body of a GET.

    Raises `TokenError` if there is no answer, or it cannot be read.
    """
    base = os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL).rstrip("/")
    request = urllib.request.Request(  # noqa: S310 - the base URL is https or a local test mock
        f"{base}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with http_request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            try:
                return error.code, json.load(error)
            except ValueError:
                return error.code, {}
    except OSError:
        raise TokenError(
            "Cloudflare could not be reached. Check the internet connection."
        ) from None
    except (ValueError, http.client.HTTPException):
        raise TokenError("Cloudflare sent an answer HomeProxy could not read.") from None


def check_token(zone: str, token: str) -> None:
    """Raise `TokenError` unless the token can read `zone` and its DNS records.

    DNS:Edit cannot be checked without writing a record, so only reading is checked. lego's own
    error shows a missing DNS:Edit when it tries to write the challenge record.
    """
    register_secret(token)
    status, body = _get(f"/zones?{urllib.parse.urlencode({'name': zone})}", token)
    if status == 401:
        raise TokenError("The Cloudflare API token is invalid or expired.")
    zones = body.get("result") if status == 200 else None
    found = [z for z in zones or [] if isinstance(z, dict) and z.get("name") == zone]
    if not found:
        raise TokenError(
            f"The Cloudflare API token cannot read the zone {zone}, or the zone is not in this "
            f"account. {_NEEDS}"
        )
    zone_id = urllib.parse.quote(str(found[0].get("id", "")), safe="")
    status, _ = _get(f"/zones/{zone_id}/dns_records?per_page=1", token)
    if status != 200:
        raise TokenError(f"The Cloudflare API token cannot read DNS records in {zone}. {_NEEDS}")
