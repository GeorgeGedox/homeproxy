"""Registers the secrets in loaded models for redaction."""

from urllib.parse import urlsplit, urlunsplit

from homeproxy.models import Config, Secrets
from homeproxy.util.redact import register_secret


def register_model_secrets(model: Config | Secrets) -> None:
    """Register every secret in a loaded model with `register_secret`.

    `provider`, `machine_id` and `captcha_site_key` are left out on purpose. They are not secret,
    and replacing every occurrence of a value such as `cloudflare` would garble logs. Empty values
    are ignored by `register_secret`. Registration is per process.
    """
    if isinstance(model, Config):
        register_secret(model.dashboard.password_hash.get_secret_value())
        return
    tokens = [
        s for credential in model.dns_credentials.values() for s in credential.secret_values()
    ]
    hashes = [value for users in model.basic_auth.values() for value in users.values()]
    # A receiver that is a route on this proxy gets only the path and query in the access log.
    # shortcut: a target under 16 characters, such as "/api", is skipped, or it would garble every
    # log line; redact such short targets in access lines only if a receiver ever needs it.
    urls = (urlsplit(url.get_secret_value()) for url in model.notifications.urls())
    targets = (urlunsplit(("", "", url.path, url.query, "")) for url in urls)
    request_targets = [target for target in targets if len(target) >= 16]
    register_secret(
        *(
            secret.get_secret_value()
            for secret in (
                model.session_key,
                *tokens,
                *hashes,
                model.crowdsec.bouncer_key,
                model.crowdsec.machine_password,
                model.crowdsec.captcha_secret_key,
                model.crowdsec.captcha_signing_key,
                model.geoip.maxmind_license_key,
                *([] if model.backup_token_hash is None else [model.backup_token_hash]),
                *model.notifications.urls(),
            )
        ),
        *request_targets,
    )
