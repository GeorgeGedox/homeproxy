"""GeoIP: download DB-IP Lite or MaxMind GeoLite2 data and build `country.map` from it.

`sources` downloads and reads the data, `country_map` builds the map and `data` keeps it in
`/data/geoip/`, from where each apply renders it into the release. The refresh job is in
`homeproxy.jobs.geoip`.
"""
