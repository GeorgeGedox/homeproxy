"""A small client for the Cloudflare DNS records API: find a zone, list, create, update, delete.

Every call is logged with its method, path and status. A rate-limited call (HTTP 429) waits and
tries again. The token is registered for redaction and only ever sent in a header.
"""

import http.client
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from homeproxy.certs.token import BASE_URL_ENV, DEFAULT_BASE_URL
from homeproxy.models import Domain, Secrets
from homeproxy.util import http as http_request
from homeproxy.util.redact import register_secret

log = logging.getLogger(__name__)

TIMEOUT = 15.0
RATE_LIMIT_TRIES = 4
MAX_RETRY_AFTER = 60.0
"""A longer wait fails the call, so a reconcile never blocks an apply follow-up for long."""
PAGE_SIZE = 100


class CloudflareError(Exception):
    """A Cloudflare API call failed. The message never holds the token."""


@dataclass(frozen=True)
class CloudflareRecord:
    """One record as Cloudflare lists it."""

    id: str
    type: str
    name: str
    content: str
    proxied: bool
    comment: str
    ttl: int = 1


@dataclass(frozen=True)
class RecordData:
    """The fields HomeProxy writes for a record. A `ttl` of 1 means automatic."""

    type: str
    name: str
    content: str
    proxied: bool
    comment: str
    ttl: int = 1


class CloudflareClient:
    """Calls the API with one token. The base URL comes from `CLOUDFLARE_BASE_URL`, as for lego."""

    def __init__(self, token: str) -> None:
        """Use `token` for every call. It needs Zone:Zone:Read and Zone:DNS:Edit."""
        register_secret(token)
        self._token = token
        self._base = os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL).rstrip("/")

    @classmethod
    def for_domain(cls, domain: Domain, secrets: Secrets) -> "CloudflareClient":
        """Return a client with the domain's token. Raises `CloudflareError` if it has none."""
        if domain.credentials is None:
            raise CloudflareError(f"{domain.zone} has no DNS provider")
        credential = secrets.dns_credentials.get(domain.credentials)
        if credential is None:
            raise CloudflareError(f"credentials {domain.credentials!r} are not in secrets.yaml")
        if credential.api_token is None:
            raise CloudflareError(f"credentials {domain.credentials!r} hold no Cloudflare token")
        return cls(credential.api_token.get_secret_value())

    def zone_id(self, zone: str) -> str:
        """Return the id of `zone`. Raises `CloudflareError` if the token cannot see it."""
        query = urllib.parse.urlencode({"name": zone})
        body = self._call("GET", f"/zones?{query}")
        try:
            found = [z for z in body["result"] if z["name"] == zone]
        except (KeyError, TypeError) as error:
            log.warning("cloudflare zones: unexpected answer: %r", error)
            raise CloudflareError("unexpected answer listing zones") from None
        if not found:
            raise CloudflareError(f"the API token cannot read the zone {zone}")
        return str(found[0]["id"])

    def list_records(self, zone_id: str) -> list[CloudflareRecord]:
        """Return every DNS record in the zone, across all pages."""
        records: list[CloudflareRecord] = []
        page, pages = 1, 1
        while page <= pages:
            path = f"/zones/{_quote(zone_id)}/dns_records?per_page={PAGE_SIZE}&page={page}"
            body = self._call("GET", path)
            try:
                records += [_record(item) for item in body["result"]]
                pages = int(body.get("result_info", {}).get("total_pages", 1))
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                log.warning("cloudflare records: unexpected answer: %r", error)
                raise CloudflareError("unexpected answer listing records") from None
            page += 1
        return records

    def create_record(self, zone_id: str, data: RecordData) -> None:
        """Create a record."""
        self._call("POST", f"/zones/{_quote(zone_id)}/dns_records", _payload(data))

    def update_record(self, zone_id: str, record_id: str, data: RecordData) -> None:
        """Overwrite a record."""
        path = f"/zones/{_quote(zone_id)}/dns_records/{_quote(record_id)}"
        self._call("PUT", path, _payload(data))

    def delete_record(self, zone_id: str, record_id: str) -> None:
        """Delete a record."""
        self._call("DELETE", f"/zones/{_quote(zone_id)}/dns_records/{_quote(record_id)}")

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        """Return the JSON body of a successful call, waiting out rate limits."""
        for attempt in range(RATE_LIMIT_TRIES):
            status, retry_after, body = self._send(method, path, payload)
            log.info("cloudflare %s %s: HTTP %s", method, path.split("?")[0], status)
            if status != 429 or attempt + 1 == RATE_LIMIT_TRIES:
                break
            wait = 2.0**attempt if retry_after is None else retry_after
            if not 0 <= wait <= MAX_RETRY_AFTER:  # also false for nan
                raise CloudflareError(f"rate limited, retry after {wait:g} s")
            time.sleep(wait)
        if status >= 400 or not isinstance(body, dict) or not body.get("success"):
            raise CloudflareError(f"{method} {path.split('?')[0]} failed: {_reason(status, body)}")
        return body

    def _send(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> tuple[int, float | None, Any]:
        """Return the status, the Retry-After seconds and the JSON body of one request."""
        request = urllib.request.Request(  # noqa: S310 - the base URL is https or a local test mock
            f"{self._base}{path}",
            data=None if payload is None else json.dumps(payload).encode(),
            method=method,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with http_request.urlopen(request, timeout=TIMEOUT) as response:
                return response.status, None, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, _seconds(error.headers.get("Retry-After")), _json(error)
        except OSError as error:
            log.warning("cloudflare %s %s: %s", method, path.split("?")[0], type(error).__name__)
            raise CloudflareError(
                "Cloudflare could not be reached, check the internet connection"
            ) from None
        except (ValueError, http.client.HTTPException) as error:
            log.warning("cloudflare %s %s: %s", method, path.split("?")[0], type(error).__name__)
            raise CloudflareError("Cloudflare sent an answer HomeProxy could not read") from None


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _payload(data: RecordData) -> dict[str, Any]:
    return {
        "type": data.type,
        "name": data.name,
        "content": data.content,
        "proxied": data.proxied,
        "ttl": data.ttl,
        "comment": data.comment,
    }


def _record(item: dict[str, Any]) -> CloudflareRecord:
    return CloudflareRecord(
        id=str(item["id"]),
        type=str(item["type"]),
        name=str(item["name"]).lower(),
        content=str(item.get("content", "")),
        proxied=bool(item.get("proxied", False)),
        comment=str(item.get("comment") or ""),
        ttl=int(item.get("ttl") or 1),
    )


def _seconds(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _json(error: urllib.error.HTTPError) -> Any:
    try:
        return json.load(error)
    except (OSError, ValueError, http.client.HTTPException):
        return {}


def _reason(status: int, body: Any) -> str:
    """Return Cloudflare's own error messages, or the HTTP status."""
    errors = body.get("errors") if isinstance(body, dict) else None
    messages = [str(e.get("message")) for e in errors or [] if isinstance(e, dict)]
    return "; ".join(messages) or f"HTTP {status}"
