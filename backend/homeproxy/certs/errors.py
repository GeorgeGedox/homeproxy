"""Certificate errors. Their text is shown to the user as a job error, so it must be safe to show.

Only secrets registered with `homeproxy.util.redact` are removed from it. Every token reaches
lego through `run(secret_env=...)` or the token check, which registers it first.
"""


class CertificateError(Exception):
    """A certificate could not be issued, renewed or installed."""


class TokenError(CertificateError):
    """The DNS provider token cannot do what certificates need. The text says what is missing."""
