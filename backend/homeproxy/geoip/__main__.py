"""Build `country.map` in a child process, so the memory of the build goes away when it exits.

`python -m homeproxy.geoip <provider> <directory>`, with the MaxMind license key in `KEY_ENV`
and never in the arguments. The full data also goes to the log collector's lookup file in
`directory`. The map goes to stdout. A `GeoipError` goes to stderr, with exit 1.
"""

import os
import sys
from pathlib import Path
from typing import cast

from homeproxy.geoip.country_map import GeoipError, build_country_map
from homeproxy.geoip.lookup import write_lookup
from homeproxy.geoip.sources import Provider, fetch_ranges

KEY_ENV = "HOMEPROXY_MAXMIND_KEY"


def main() -> int:
    """Write the map of the provider in `sys.argv` to stdout, and return the exit status."""
    provider, directory = sys.argv[1:3]
    key = os.environ.get(KEY_ENV, "")
    try:
        ipv4, ipv6 = fetch_ranges(cast(Provider, provider), key, Path(directory))
        text = build_country_map(ipv4, ipv6)
    except GeoipError as error:
        print(error, file=sys.stderr)
        return 1
    write_lookup(Path(directory), ipv4, ipv6)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
