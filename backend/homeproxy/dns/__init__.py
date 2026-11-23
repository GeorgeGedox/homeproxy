"""The internal DNS server: answers route names in the configured zones with LAN addresses.

It runs as its own supervised process (`python -m homeproxy.dns`) and reads only the record
snapshot that the apply pipeline writes, so it keeps answering when the dashboard stops.
"""
