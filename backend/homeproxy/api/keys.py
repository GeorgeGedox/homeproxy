"""`GET /settings/keys` and `PUT /settings/keys`: the write-only CrowdSec and MaxMind keys.

A read says only whether each key is set. A write sets, replaces or clears keys in `secrets.yaml`
at once, because secrets have no pending copy. The values never reach an answer, a log line or the
audit log, which records only the key names. The bouncer gets new CrowdSec keys at once, as an
apply would give them, and the MaxMind key is used by the next GeoIP refresh.
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel, SecretStr
from ruamel.yaml.comments import CommentedMap

from homeproxy.api.changes import edit_lock
from homeproxy.auth.routes import Runtime
from homeproxy.haproxy.apply_rules import crowdsec_key_errors
from homeproxy.haproxy.bouncer import bouncer_warnings
from homeproxy.models import StrictModel
from homeproxy.models.secrets import Secrets, SecretsCrowdsec, SecretsGeoip
from homeproxy.store import Store, StoreError, load_tree

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["settings"])


class KeysInput(StrictModel):
    """Keys to set, replace or clear, by their section in `secrets.yaml`. Leave a key out to keep
    it, or send `""` to clear it."""

    crowdsec: SecretsCrowdsec = SecretsCrowdsec()
    geoip: SecretsGeoip = SecretsGeoip()


class CrowdsecKeysSet(BaseModel):
    """Whether each CrowdSec key has a value."""

    bouncer_key: bool
    machine_id: bool
    machine_password: bool
    captcha_site_key: bool
    captcha_secret_key: bool
    captcha_signing_key: bool


class GeoipKeysSet(BaseModel):
    """Whether the MaxMind license key has a value."""

    maxmind_license_key: bool


class KeysSet(BaseModel):
    """Whether each write-only key has a value. Values are never returned."""

    crowdsec: CrowdsecKeysSet
    geoip: GeoipKeysSet


def _is_set(section: BaseModel) -> dict[str, bool]:
    return {
        name: bool(value.get_secret_value())
        for name, value in section
        if isinstance(value, SecretStr)
    }


def keys_set(secrets: Secrets) -> KeysSet:
    """Return which keys of `secrets` have a value."""
    return KeysSet(
        crowdsec=CrowdsecKeysSet.model_validate(_is_set(secrets.crowdsec)),
        geoip=GeoipKeysSet.model_validate(_is_set(secrets.geoip)),
    )


def _write(tree: CommentedMap, name: str, section: BaseModel) -> list[str]:
    """Write the keys the request sent in `section` into `tree[name]` and return their names."""
    sent = {key: getattr(section, key) for key in sorted(section.model_fields_set)}
    if not sent:
        return []
    target = tree.get(name)
    if not isinstance(target, CommentedMap):
        target = tree[name] = CommentedMap()
    for key, value in sent.items():
        if value.get_secret_value():
            target[key] = value.get_secret_value()
        else:
            target.pop(key, None)
    return [f"{name}.{key}" for key in sent]


def _give_bouncer(store: Store, secrets: Secrets) -> None:
    """Give the bouncer the new keys with the live config, as the last apply gave it.

    Keys the apply would refuse, such as a cleared bouncer key, are not given: the bouncer keeps
    its config, so CrowdSec keeps working. A failure is logged; the next apply tries again.
    """
    try:
        config = store.load_config()
    except StoreError as error:
        logger.error("cannot give the CrowdSec bouncer the new keys: %s", error)
        return
    missing = crowdsec_key_errors(config.crowdsec, secrets.crowdsec)
    if config.crowdsec.enabled and missing:
        logger.warning("the CrowdSec bouncer keeps its keys until an apply: %s", "; ".join(missing))
        return
    bouncer_warnings(store, config)


def _save(store: Store, body: KeysInput) -> KeysSet:
    with edit_lock(store) as held:
        tree = load_tree(store.secrets_path)
        changed = _write(tree, "crowdsec", body.crowdsec) + _write(tree, "geoip", body.geoip)
        if changed:
            store.save_secrets(tree, held)
        secrets = store.load_secrets()
        if body.crowdsec.model_fields_set:
            _give_bouncer(store, secrets)
    if changed:
        logger.info("keys changed: %s", ", ".join(changed))
        store.audit.record("admin", "keys", {"changed": changed})
    return keys_set(secrets)


@router.get(
    "/settings/keys", response_model=KeysSet, responses={401: {"description": "No session"}}
)
def get_keys(runtime: Runtime) -> KeysSet:
    """Return whether each CrowdSec and MaxMind key is set, never a value."""
    return keys_set(runtime.store.load_secrets())


@router.put(
    "/settings/keys",
    response_model=KeysSet,
    responses={
        401: {"description": "No valid session"},
        422: {"description": "A key is not valid; `field` names it. No value is echoed"},
    },
)
def put_keys(body: KeysInput, runtime: Runtime) -> KeysSet:
    """Set, replace or clear keys and return which are set. The CrowdSec bouncer is restarted
    with new keys at once."""
    return _save(runtime.store, body)
