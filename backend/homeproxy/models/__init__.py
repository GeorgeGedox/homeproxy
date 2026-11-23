"""Pydantic models for `config.yaml`, `routes/*.yaml` and `secrets.yaml`.

These models are the authority on the file formats. They validate plain data, and loading YAML is
the job of `homeproxy.store`. Use `safe_errors` wherever a validation error can reach a log or a
response.
"""

from homeproxy.models.base import StrictModel
from homeproxy.models.change import Action, ChangeSummary
from homeproxy.models.config import Config
from homeproxy.models.domain import Domain
from homeproxy.models.errors import SafeError, safe_errors
from homeproxy.models.route import (
    HttpRoute,
    PassthroughRoute,
    RedirectRoute,
    Route,
    TcpRoute,
    parse_route,
    route_label,
)
from homeproxy.models.secrets import Secrets

__all__ = [
    "Action",
    "ChangeSummary",
    "Config",
    "Domain",
    "HttpRoute",
    "PassthroughRoute",
    "RedirectRoute",
    "Route",
    "SafeError",
    "Secrets",
    "StrictModel",
    "TcpRoute",
    "parse_route",
    "route_label",
    "safe_errors",
]
