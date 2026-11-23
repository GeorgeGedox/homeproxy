"""JSON schema export for the UI forms.

Run `python -m homeproxy.models.schema` to print the schema to standard output. The schema holds
field names, patterns and defaults, and no values from any file.
"""

import json
import sys
from typing import Any

from pydantic import BaseModel

from homeproxy.models.config import Config
from homeproxy.models.domain import Domain
from homeproxy.models.route import Route
from homeproxy.models.secrets import Secrets


class HomeProxySchemas(BaseModel):
    """Wrapper that puts every file model in one document with one shared `$defs` section."""

    config: Config
    route: Route
    domain: Domain
    secrets: Secrets


def json_schema() -> dict[str, Any]:
    """Return one JSON schema document. Each file model is a property of its root object."""
    return HomeProxySchemas.model_json_schema()


def main() -> None:
    """Print the JSON schema to standard output."""
    json.dump(json_schema(), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
