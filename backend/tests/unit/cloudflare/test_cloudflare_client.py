"""The API client against the fake Cloudflare: rate limits, logging and errors."""

import logging

import pytest

from homeproxy.cloudflare import CloudflareClient, CloudflareError
from tests.unit.cloudflare.conftest import TOKENS, FakeCloudflare


def test_rate_limited_call_waits_and_retries_and_every_call_is_logged(
    cloudflare: FakeCloudflare, caplog: pytest.LogCaptureFixture
) -> None:
    cloudflare.rate_limited = 2
    caplog.set_level(logging.INFO, "homeproxy.cloudflare")

    zone_id = CloudflareClient(TOKENS["example.com"]).zone_id("example.com")

    assert zone_id == "id-example.com"
    assert caplog.messages == ["cloudflare GET /zones: HTTP 429"] * 2 + [
        "cloudflare GET /zones: HTTP 200"
    ]


def test_api_error_names_cloudflare_message_without_the_token(cloudflare: FakeCloudflare) -> None:
    token = TOKENS["example.net"]  # the other zone's token

    with pytest.raises(CloudflareError, match="Authentication error") as failure:
        CloudflareClient(token).list_records("id-example.com")

    assert token not in str(failure.value)
