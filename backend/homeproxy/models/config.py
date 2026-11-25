"""Models for `config.yaml`.

Rules that need more than one file live in the apply pipeline, not here. One example is that each
domain's `credentials` must exist in `secrets.yaml`.
"""

from typing import Annotated, Literal, get_args

from pydantic import Field, ValidationInfo, field_validator

from homeproxy.models.base import StrictModel
from homeproxy.models.domain import Domain
from homeproxy.models.fields import (
    BaseUrl,
    ConfigVersion,
    CountryCode,
    Duration,
    Hostname,
    IpOrCidr,
    IPv4,
    ListenAddress,
    Name,
    PasswordHash,
    Port,
    PublicIp,
    duration_ms,
    plain_pattern,
)


class Dashboard(StrictModel):
    """The LAN-only admin dashboard. It always uses HTTPS."""

    listen: ListenAddress
    password_hash: PasswordHash
    session_ttl: Duration = "12h"


class Network(StrictModel):
    """LAN ranges and the addresses and ports HAProxy uses."""

    lan_cidrs: list[IpOrCidr] = Field(min_length=1)
    haproxy_lan_ip: IPv4
    http_port: Port = 80
    https_port: Port = Field(default=443, validate_default=True)
    public_ip: PublicIp = "auto"
    ipv6: bool = False

    @field_validator("https_port")
    @classmethod
    def _ports_differ(cls, value: int, info: ValidationInfo) -> int:
        if value == info.data.get("http_port"):
            raise ValueError("https_port must differ from http_port")
        return value


MIN_HEARTBEAT_SECONDS = 30
# A reconcile makes about two Cloudflare calls per zone, far under the 1200 per 5 minutes the API
# allows. Records change only at an apply, which reconciles at once, so more often buys nothing.
MIN_RECONCILE_SECONDS = 300

Interval = Annotated[
    str,
    plain_pattern(
        r"^[1-9][0-9]{0,5}[smhd]$", "must be a number and a unit (s, m, h or d), such as 1m"
    ),
]


def _seconds(interval: str) -> int:
    return duration_ms(interval) // 1000


class CloudflareSettings(StrictModel):
    """The global toggle for Cloudflare DNS records and DDNS, and how often reconcile runs."""

    enabled: bool = False
    reconcile_interval: Interval = "15m"

    @property
    def reconcile_seconds(self) -> int:
        """The reconcile interval in seconds."""
        return _seconds(self.reconcile_interval)

    @field_validator("reconcile_interval")
    @classmethod
    def _long_enough(cls, interval: str) -> str:
        if _seconds(interval) < MIN_RECONCILE_SECONDS:
            raise ValueError("must be at least 5m")
        return interval


class DnsServer(StrictModel):
    """The internal DNS server."""

    listen: ListenAddress
    forward_unknown_to: list[IPv4] = Field(default_factory=list)


class DnsRecord(StrictModel):
    """An extra LAN name that is not a route."""

    name: Hostname
    target: IPv4


class Geoip(StrictModel):
    """The GeoIP data source and the global deny-list."""

    provider: Literal["dbip-lite", "maxmind"] = "dbip-lite"
    block_countries: list[CountryCode] = Field(default_factory=list)


class Crowdsec(StrictModel):
    """The CrowdSec integration. Its addresses are required only while it is enabled."""

    enabled: bool = False
    lapi_url: BaseUrl | None = Field(default=None, validate_default=True)
    # An IPv4 address, never a name: HAProxy resolves a log target once, when it reads its config,
    # and a name that does not resolve then stops HAProxy from starting.
    syslog: ListenAddress | None = Field(default=None, validate_default=True)
    captcha_provider: Literal["none", "turnstile", "hcaptcha", "recaptcha"] = "none"
    allowlist: list[IpOrCidr] = Field(default_factory=list)
    appsec: bool = False
    # None means port 7422, the AppSec component's default, on the LAPI's host.
    appsec_url: BaseUrl | None = None

    @field_validator("lapi_url", "syslog")
    @classmethod
    def _required_when_enabled(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None and info.data.get("enabled") is True:
            raise ValueError("required when crowdsec is enabled")
        return value


class CertificateSettings(StrictModel):
    """Which Let's Encrypt CA issues the certificates. Staging is for tests: browsers do not
    trust its certificates."""

    staging: bool = False


class HaproxySettings(StrictModel):
    """Which HAProxy release branch the version check follows."""

    track_branch: Literal["lts"] = "lts"


class Prometheus(StrictModel):
    """HAProxy's built-in Prometheus exporter, served at `/metrics` to LAN clients only."""

    enabled: bool = False
    listen: ListenAddress | None = Field(default=None, validate_default=True)

    @field_validator("listen")
    @classmethod
    def _required_when_enabled(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None and info.data.get("enabled") is True:
            raise ValueError("required when prometheus is enabled")
        return value


NotificationEvent = Literal[
    "certificate_failed",
    "certificate_expiring",
    "apply_failed",
    "route_down",
    "bouncer_down",
    "appsec_down",
    "ddns_failed",
    "geoip_failed",
    "new_version",
]


class NotificationTarget(StrictModel):
    """A place to send messages. Its URL is a secret: `secrets.yaml` `notifications.<name>.url`."""

    name: Name
    type: Literal["discord", "webhook"]
    events: list[NotificationEvent] = Field(
        default_factory=lambda: list(get_args(NotificationEvent))
    )


class Heartbeat(StrictModel):
    """A dead-man switch: GET the ping URL every `interval` while HAProxy answers on its socket.

    The URL is a secret: `secrets.yaml` `notifications.heartbeat_url`.
    """

    enabled: bool = False
    interval: Interval = "1m"

    @property
    def seconds(self) -> int:
        """The interval in seconds."""
        return _seconds(self.interval)

    @field_validator("interval")
    @classmethod
    def _long_enough(cls, interval: str) -> str:
        if _seconds(interval) < MIN_HEARTBEAT_SECONDS:
            raise ValueError("must be at least 30s")
        return interval


class Notifications(StrictModel):
    """Where failures and changes of state are sent. Each target gets the events it lists."""

    targets: list[NotificationTarget] = Field(default_factory=list)
    heartbeat: Heartbeat = Field(default_factory=Heartbeat)

    @field_validator("targets")
    @classmethod
    def _unique_names(cls, targets: list[NotificationTarget]) -> list[NotificationTarget]:
        names = [target.name for target in targets]
        if len(set(names)) != len(names):
            raise ValueError("each target name may appear only once")
        return targets


class Config(StrictModel):
    """The whole of `config.yaml`."""

    version: ConfigVersion
    dashboard: Dashboard
    network: Network
    cloudflare: CloudflareSettings = Field(default_factory=CloudflareSettings)
    domains: list[Domain] = Field(default_factory=list)
    dns_server: DnsServer
    dns_records: list[DnsRecord] = Field(default_factory=list)
    geoip: Geoip = Field(default_factory=Geoip)
    crowdsec: Crowdsec = Field(default_factory=Crowdsec)
    certificates: CertificateSettings = Field(default_factory=CertificateSettings)
    haproxy: HaproxySettings = Field(default_factory=HaproxySettings)
    prometheus: Prometheus = Field(default_factory=Prometheus)
    notifications: Notifications = Field(default_factory=Notifications)

    @field_validator("domains")
    @classmethod
    def _unique_zones(cls, domains: list[Domain]) -> list[Domain]:
        zones = [domain.zone for domain in domains]
        if len(set(zones)) != len(zones):
            raise ValueError("each zone may appear only once")
        return domains

    @field_validator("dns_records")
    @classmethod
    def _unique_record_names(cls, records: list[DnsRecord]) -> list[DnsRecord]:
        names = [record.name for record in records]
        if len(set(names)) != len(names):
            raise ValueError("each record name may appear only once")
        return records
