"""Certificates: lego with DNS-01, PEM bundles in `/data/certs/`, and their delivery to HAProxy.

`plan` decides which certificates the config needs, `lego` obtains and renews them, `bundle`
turns lego's files into HAProxy bundles and reads their expiry, `deploy` installs a bundle with a
hot swap or a reload, and `service` ties these together for the jobs in `homeproxy.jobs.certs`.
"""
