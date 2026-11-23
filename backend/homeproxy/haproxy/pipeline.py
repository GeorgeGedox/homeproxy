"""The apply pipeline: the one way a change reaches HAProxy.

`Pipeline.apply` holds the store's lock from start to end and either finishes with HAProxy serving
a release built from `/data/config` plus the pending edits, or leaves the old release running:

1. Validate the overlay with the models and `check_rules`. Stop on a hand-edit conflict.
2. Render it. A release equal to the live one changes nothing in HAProxy, unless nothing is
   pending: then the live release is reloaded and verified, which resyncs HAProxy after a failed
   rollback.
3. Write it to a new `/data/releases/<id>/` and run `haproxy -c` on it.
4. Point `/data/generated.prev` at the live release, then `/data/generated` at the new one. If only
   the maps in `RUNTIME_MAPS` differ, update them through the runtime API.
   Otherwise, or if a map command fails, save the server state and reload through the master CLI.
5. Verify with `show info` and a connection to each HAProxy port. A down upstream is a warning.
6. On failure, point `/data/generated` back at the old release, and reload unless HAProxy refused
   the new one. `/data/config` is unchanged and the edits stay pending.
7. Promote the edits, remove `generated.prev`, take a snapshot (with nothing pending, only when
   `/data/config` differs from the newest one), keep the live and the previous release, signal
   the DNS process to reread `dns-records.json`, write the CrowdSec bouncer's config, and audit.

`generated.prev` is what `init-data` links back at boot, so a container killed before the promote
finishes starts on the last promoted release and not on one that was never confirmed.

The release also holds the DNS table. A change to it alone needs no reload, because HAProxy never
reads it.

Certificates are not part of a release, so an apply never changes one. Hot swaps and the reload
for a new certificate belong to the certificate code.

No code path here stops HAProxy: every failure leaves the last working worker serving.
"""

import logging
import shutil
import socket
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from homeproxy.dns.service import PID_FILE
from homeproxy.dns.signal import reload_dns
from homeproxy.dns.snapshot import FILE_NAME as DNS_FILE
from homeproxy.dns.snapshot import build_snapshot
from homeproxy.generator import (
    BACKEND_PREFIX,
    BasicAuth,
    GeneratorError,
    RenderedRelease,
    ensure_default_certificate,
    render,
    write_release,
)
from homeproxy.generator.cloudflare_ranges import load_ranges
from homeproxy.geoip.data import load_country_map, load_data_info
from homeproxy.haproxy.apply_history import applied_change_summary, change_message, snapshot
from homeproxy.haproxy.apply_rules import (
    check_rules,
    country_warnings,
    fallback_warnings,
    path_only_hosts,
    prometheus_warnings,
    public_range_warnings,
)
from homeproxy.haproxy.bouncer import bouncer_warnings
from homeproxy.haproxy.client import HAProxyClient, HAProxyError
from homeproxy.haproxy.release_links import (
    RELEASES,
    clear_previous,
    link,
    live_release,
    restore_previous,
)
from homeproxy.haproxy.runtime_maps import map_changes, update_maps
from homeproxy.haproxy.stats import ProxyType, StatRow
from homeproxy.models import Config, HttpRoute, Route, route_label
from homeproxy.store import HeldLock, Store, StoreError
from homeproxy.store.pending import Overlay, PendingChanges, PendingConflict
from homeproxy.util.atomic import atomic_write, ensure_dir
from homeproxy.util.run import RunError, run

logger = logging.getLogger(__name__)

Stage = Literal["validate", "check", "apply", "verify", "promote", "done"]
ApplyPath = Literal["none", "map", "reload"]

_CHECK_TIMEOUT = 120.0
_PROBE_TIMEOUT = 2.0
_POLL_INTERVAL = 0.2
_SETTLE_SECONDS = 7.0
"""How long to wait for the first health check of new servers before reporting health: the 1 s
`max-spread-checks`, then the 5 s connect timeout that an absent host runs into."""
_RELOAD_DEADLINE = 30.0
"""How long to wait for a new worker after a reload whose result never arrived."""


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of one apply. Every text in it is safe to show to the admin.

    `stage` is where a failed apply stopped, or `done`. `path` is how HAProxy got the release:
    `none` when the pending edits change nothing in it, `map` through the runtime API, `reload`
    through the master CLI. `haproxy_log` is the output of `haproxy -c` or the reload's startup log.
    """

    success: bool
    stage: Stage
    path: ApplyPath | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    snapshot_id: str | None = None
    haproxy_log: str = ""


class _Failure(Exception):
    """Stops an apply. `refused` means HAProxy answered `Success=0` and kept the old worker."""

    def __init__(self, stage: Stage, *errors: str, log: str = "", refused: bool = False) -> None:
        super().__init__("; ".join(errors))
        self.stage: Stage = stage
        self.errors = list(errors)
        self.log = log
        self.refused = refused


@dataclass(frozen=True)
class Pipeline:
    """Applies the pending edits of one store to one HAProxy. Call it from a worker thread.

    `haproxy_command` runs the HAProxy binary for `haproxy -c`, `probe_host` is where HAProxy's
    ports answer, and `dns_pid_file` names the DNS process to signal. All default to the image,
    where the API, HAProxy and DNS share one host.
    """

    store: Store
    client: HAProxyClient = field(default_factory=HAProxyClient)
    haproxy_command: tuple[str, ...] = ("haproxy",)
    probe_host: str = "127.0.0.1"
    dns_pid_file: Path = PID_FILE

    @property
    def _releases(self) -> Path:
        return self.store.root / RELEASES

    def recover(self) -> None:
        """Finish what a crash cut short. Call it once at startup, before any read.

        An apply that died before its promote leaves `generated.prev`. Switch back to it, as
        `init-data` does at boot, because the API can restart while the container keeps running,
        and reload, because HAProxy may run the new release. A promote that already began is
        finished and `generated.prev` is dropped, because `/data/config` holds the edits of the
        new release.
        """
        with self.store.lock() as held:
            pending = PendingChanges(self.store)
            committed = pending.promote_started(held)
            if restore_previous(self.store.root, keep_current=committed):
                try:
                    self._reload()
                except _Failure as failure:
                    logger.error("reloading the restored release failed: %s", failure)
            pending.status(held)

    def apply(self, user: str) -> ApplyResult:
        """Apply the pending edits, or re-apply `/data/config` when nothing is pending."""
        with self.store.lock() as held:
            return self._audit(user, *self._result(held))

    def apply_unless_pending(self, user: str) -> ApplyResult | None:
        """Re-apply `/data/config`, as after new GeoIP data. While edits are pending, return None
        and change nothing, because only the admin's own apply may make them live."""
        with self.store.lock() as held:
            if PendingChanges(self.store).status(held).changes:
                return None
            return self._audit(user, *self._result(held))

    def _result(self, held: HeldLock) -> tuple[ApplyResult, list[str]]:
        """Return the outcome and its upstream health warnings."""
        try:
            return self._apply(held)
        except _Failure as failure:
            result = ApplyResult(
                success=False,
                stage=failure.stage,
                errors=tuple(failure.errors),
                haproxy_log=failure.log,
            )
            return result, []

    def _apply(self, held: HeldLock) -> tuple[ApplyResult, list[str]]:
        pending = PendingChanges(self.store)
        overlay = self._validated_overlay(pending, held)
        release = self._render(overlay)
        warnings = public_range_warnings(overlay.config)
        warnings += prometheus_warnings(overlay.config)
        warnings += fallback_warnings(overlay.config, overlay.routes)
        warnings += path_only_hosts(overlay.routes).values()
        warnings += country_warnings(
            overlay.config, overlay.routes, load_data_info(self.store.root)
        )
        live = live_release(self.store.root)
        live_files = read_release(live)
        new_dir = live
        if live_files != dict(release.files):
            new_dir = self._write_and_check(release)
            path: ApplyPath = self._switch(live, new_dir, release, overlay.config)
        elif pending.status(held).changes:
            path = "none"
        else:
            # A rollback that failed can leave HAProxy running a release that is no longer live.
            self._reload()
            self._verify(overlay.config)
            path = "reload"
        health = self._health_warnings(overlay.routes)
        warnings += health
        message = change_message(self.store, pending, held)
        changes = applied_change_summary(self.store, pending, held, live_files, release)
        try:
            promoted = pending.promote(held)
        except PendingConflict as conflict:
            # HAProxy must not keep running edits that may now be discarded.
            undo = self._roll_back(live, new_dir, reload=True) if new_dir != live else []
            raise _Failure("promote", str(conflict), *undo) from None
        except OSError as exc:
            # HAProxy already runs this release. The next operation under the lock finishes it.
            logger.error("promoting the pending edits failed: %s", exc)
            warnings.append("the edits are live but could not all be saved; they will be retried")
            promoted = ()
        clear_previous(self.store.root)
        if new_dir != live:
            self._prune(keep={live.name, new_dir.name})
        snapshot_id = snapshot(self.store, message, changes, bool(promoted), held, warnings)
        reload_dns(self.dns_pid_file)
        warnings += bouncer_warnings(self.store, overlay.config)
        result = ApplyResult(
            success=True, stage="done", path=path, warnings=tuple(warnings), snapshot_id=snapshot_id
        )
        return result, health

    # Validate and render

    def _validated_overlay(self, pending: PendingChanges, held: HeldLock) -> Overlay:
        try:
            status = pending.status(held)
            if status.conflict:
                raise PendingConflict(status.conflicting_paths)
            overlay = pending.load_overlay(held)
            errors = check_rules(overlay, self.store.load_secrets)
        except StoreError as exc:
            raise _Failure("validate", str(exc)) from None
        except FileNotFoundError as exc:
            missing = Path(str(exc.filename or exc)).name
            raise _Failure("validate", f"missing file: {missing}") from None
        if errors:
            raise _Failure("validate", *errors)
        return overlay

    def render_overlay(self, overlay: Overlay) -> RenderedRelease:
        """Render an overlay as an apply would, writing nothing. `secrets.yaml` is read only when
        a route has basic auth, like `check_rules` does. Raises `GeneratorError`, `StoreError`, or
        `FileNotFoundError` for a missing `secrets.yaml`."""
        includes = {}
        for path, data in overlay.includes.items():
            try:
                includes[Path(path).stem] = data.decode("utf-8")
            except UnicodeDecodeError:
                raise GeneratorError(f"{path} is not UTF-8 text") from None
        basic = any(isinstance(r, HttpRoute) and r.auth.type == "basic" for r in overlay.routes)
        basic_auth: BasicAuth = self.store.load_secrets().basic_auth if basic else {}
        ranges, countries = load_ranges(self.store.root), load_country_map(self.store.root)
        release = render(overlay.config, overlay.routes, includes, basic_auth, ranges, countries)
        dns_table = build_snapshot(overlay.config, overlay.routes).model_dump_json(indent=2)
        return replace(release, files={**release.files, DNS_FILE: dns_table})

    def _render(self, overlay: Overlay) -> RenderedRelease:
        try:
            return self.render_overlay(overlay)
        except (GeneratorError, StoreError) as exc:
            raise _Failure("validate", str(exc)) from None
        except FileNotFoundError:
            raise _Failure("validate", "missing file: secrets.yaml") from None

    def _write_and_check(self, release: RenderedRelease) -> Path:
        new_dir = self._releases / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        try:
            ensure_default_certificate(self.store.root / "certs")
            ensure_dir(self._releases)
            write_release(release, new_dir)
            result = run(
                [*self.haproxy_command, "-c", "-f", f"{new_dir}/conf.d/"],
                timeout=_CHECK_TIMEOUT,
                check=False,
            )
        except (OSError, RunError) as exc:
            shutil.rmtree(new_dir, ignore_errors=True)
            raise _Failure("check", f"cannot check the new release: {exc}") from None
        if result.returncode != 0:
            shutil.rmtree(new_dir, ignore_errors=True)
            log = (result.stdout + result.stderr).strip()
            raise _Failure("check", "haproxy -c refused the new release", log=log)
        return new_dir

    # Switch, verify and roll back

    def _switch(
        self, live: Path, new_dir: Path, release: RenderedRelease, config: Config
    ) -> ApplyPath:
        changes = map_changes(read_release(live), release.files)
        path: ApplyPath = "reload"
        try:
            link(self.store.root, live, "generated.prev")
            link(self.store.root, new_dir)
        except OSError as exc:
            # HAProxy has not seen the new release, so it needs no reload.
            errors = self._roll_back(live, new_dir, reload=False)
            raise _Failure("apply", f"cannot switch to the new release: {exc}", *errors) from None
        try:
            if changes is not None and update_maps(self.client, changes):
                path = "map"
            else:
                self._reload()
            self._verify(config)
        except _Failure as failure:
            # Runtime map changes reach the old worker too, so only a refused reload with no map
            # change leaves it exactly as it was.
            needs_reload = changes is not None or not failure.refused
            failure.errors += self._roll_back(live, new_dir, reload=needs_reload)
            raise
        return path

    def _reload(self) -> None:
        self._save_server_state()
        before = self._worker_pid()
        try:
            result = self.client.reload()
        except HAProxyError as exc:
            # The reload may still finish. Swapping the release back now could load either one.
            logger.warning("reload result unknown, waiting for a new worker: %s", exc)
            if not self._wait_for_new_worker(before):
                raise _Failure("apply", f"HAProxy did not confirm the reload: {exc}") from None
            return
        if not result.success:
            raise _Failure(
                "apply", "HAProxy refused the new release", log=result.startup_log, refused=True
            )

    def _save_server_state(self) -> None:
        """Save server health for the next worker. Without it, health checks only start over."""
        try:
            state = self.client.show_servers_state()
            target = self.store.root / "state" / "haproxy-server-state"
            ensure_dir(target.parent)
            atomic_write(target, state.encode("utf-8"))
        except (HAProxyError, OSError) as exc:
            logger.warning("cannot save the server state: %s", exc)

    def _worker_pid(self) -> int | None:
        try:
            return self.client.show_info().pid
        except HAProxyError:
            return None

    def _wait_for_new_worker(self, before: int | None) -> bool:
        deadline = time.monotonic() + _RELOAD_DEADLINE
        while time.monotonic() < deadline:
            pid = self._worker_pid()
            if pid is not None and pid != before:
                return True
            time.sleep(_POLL_INTERVAL)
        return False

    def _verify(self, config: Config) -> None:
        try:
            self.client.show_info()
        except HAProxyError as exc:
            raise _Failure("verify", f"HAProxy does not answer show info: {exc}") from None
        for port in (config.network.http_port, config.network.https_port):
            try:
                socket.create_connection((self.probe_host, port), timeout=_PROBE_TIMEOUT).close()
            except OSError as exc:
                raise _Failure("verify", f"HAProxy does not answer on port {port}: {exc}") from None

    def _roll_back(self, live: Path, new_dir: Path, *, reload: bool) -> list[str]:
        """Bring the old release back. Return what went wrong, if anything."""
        try:
            link(self.store.root, live)
        except OSError as exc:
            # `generated.prev` stays, so the next start switches back to the old release.
            logger.error("rollback could not link the old release: %s", exc)
            return [f"rolling back also failed, the next start restores the old release: {exc}"]
        clear_previous(self.store.root)
        shutil.rmtree(new_dir, ignore_errors=True)
        if not reload:
            return []
        try:
            self._reload()
        except _Failure as failure:
            logger.error("rollback reload failed: %s", failure)
            return [f"rolling back also failed, HAProxy keeps its current worker: {failure}"]
        return []

    def _health_warnings(self, routes: tuple[Route, ...]) -> list[str]:
        """Warn for each upstream that is down. Health is never a reason to roll back."""
        deadline = time.monotonic() + _SETTLE_SECONDS
        while True:
            try:
                servers = [r for r in self.client.show_stat() if r.type is ProxyType.SERVER]
            except HAProxyError as exc:
                return [f"cannot read upstream health: {exc}"]
            if time.monotonic() >= deadline or not any(map(_first_check_pending, servers)):
                break
            time.sleep(_POLL_INTERVAL)
        labels = {BACKEND_PREFIX + route.name: route_label(route) for route in routes}
        warnings = []
        for server in servers:
            if not server.proxy.startswith(BACKEND_PREFIX):
                continue
            label = labels.get(server.proxy, server.proxy.removeprefix(BACKEND_PREFIX))
            if server.status.startswith("DOWN"):
                warnings.append(f"{label}: upstream {server.service} is down")
            elif server.status.startswith("MAINT"):
                warnings.append(f"{label}: upstream {server.service} is in maintenance")
        return warnings

    # Record

    def _prune(self, keep: set[str]) -> None:
        """Keep the live and the previous release. Other entries, including crash leftovers, go."""
        for entry in self._releases.iterdir():
            if entry.name not in keep:
                shutil.rmtree(entry, ignore_errors=True)

    def _audit(self, user: str, result: ApplyResult, health: list[str]) -> ApplyResult:
        """Record the apply. Its health warnings are left out: `/status` reports health live."""
        details = {
            "success": result.success,
            "stage": result.stage,
            "path": result.path,
            "snapshot": result.snapshot_id,
            "errors": list(result.errors),
            "warnings": [w for w in result.warnings if w not in health],
        }
        try:
            self.store.audit.append(user, "apply", details)
        except OSError as exc:
            logger.error("cannot write the audit log: %s", exc)
            warning = "the audit log could not be written"
            return replace(result, warnings=(*result.warnings, warning))
        return result


def read_release(release: Path) -> dict[str, str]:
    """Return the files of a release by relative path. A missing release has no files."""
    if not release.is_dir():
        return {}
    return {
        # Not `read_text`, which would turn the CRLF of an include into LF.
        str(path.relative_to(release)): path.read_bytes().decode("utf-8", "replace")
        for path in release.rglob("*")
        if path.is_file()
    }


def _first_check_pending(server: StatRow) -> bool:
    return server.check_status.lstrip("* ") == "INI"
