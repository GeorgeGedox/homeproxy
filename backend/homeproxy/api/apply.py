"""`POST /apply`: run the apply pipeline, then bring the certificates in line with the new config.

After a successful apply, a certificate the config now needs and that has no bundle yet, or whose
bundle is not from the CA the config picks, is issued as a background job. A bundle the config no
longer needs is removed and HAProxy reloads, because HAProxy would otherwise keep serving it on an
exact SNI match until it expires. The basic auth users of removed routes are dropped from
`secrets.yaml`. While the Cloudflare integration is on, a reconcile job brings the route records in
line with the applied routes, and a DDNS job updates the DDNS records, so a new domain does not
wait for the 5-minute timer. Their failures show on the jobs and never fail the apply. A failed
apply sends an `apply_failed` notification.
"""

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from homeproxy.certs.service import CertificateService
from homeproxy.generator import DEFAULT_CERTIFICATE_NAME
from homeproxy.haproxy.client import HAProxyError
from homeproxy.haproxy.pipeline import ApplyPath, Pipeline, Stage
from homeproxy.jobs import JobRunner
from homeproxy.jobs.certs import submit_issue
from homeproxy.jobs.ddns import Ddns
from homeproxy.jobs.reconcile import Reconcile
from homeproxy.notify import notify
from homeproxy.store import Store, StoreError, load_tree
from homeproxy.store.pending import PendingChanges

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["apply"])


@dataclass(frozen=True)
class Services:
    """The process's pipeline, certificate service, job runner and Cloudflare jobs, kept on
    `app.state.services`."""

    pipeline: Pipeline
    certificates: CertificateService
    jobs: JobRunner
    reconcile: Reconcile
    ddns: Ddns


def services(request: Request) -> Services:
    """FastAPI dependency: the services the app's lifespan created."""
    found: Services = request.app.state.services
    return found


ServicesDep = Annotated[Services, Depends(services)]


class ApplyResponse(BaseModel):
    """The outcome of an apply. Every text is safe to show.

    `stage` is where a failed apply stopped, or `done`. `path` is how HAProxy got the release:
    `none`, `map` (runtime API) or `reload`. `haproxy_log` is the output of `haproxy -c` or of the
    reload. `certificate_jobs` are the ids of the issuance jobs this apply started.
    `cloudflare_job` is the id of the Cloudflare reconcile job, or null while that integration is
    off.
    """

    success: bool
    stage: Stage
    path: ApplyPath | None
    errors: list[str]
    warnings: list[str]
    snapshot_id: str | None
    haproxy_log: str
    certificate_jobs: list[str]
    cloudflare_job: str | None = None


def _remove_stale_bundles(services: Services, needed: set[str]) -> list[str]:
    """Remove bundles no certificate of the plan uses, and reload. Return warnings."""
    certs_dir = services.certificates.certs_dir
    stale = [
        path
        for path in certs_dir.glob("*.pem")
        if path.stem not in needed
        and path.name != DEFAULT_CERTIFICATE_NAME
        and not path.name.startswith(".")
    ]
    if not stale:
        return []
    # The store lock keeps this reload apart from an apply or a certificate install.
    with services.pipeline.store.lock():
        for path in stale:
            path.unlink(missing_ok=True)
        try:
            result = services.pipeline.client.reload()
        except HAProxyError as error:
            logger.error("reload after removing unused certificates failed: %s", error)
            return ["unused certificates were removed, but HAProxy did not reload"]
    if not result.success:
        return ["unused certificates were removed, but HAProxy refused the reload"]
    logger.info("removed unused certificates: %s", ", ".join(p.stem for p in stale))
    return []


def _drop_unused_basic_auth(store: Store) -> list[str]:
    """Drop the basic auth users of routes that no longer exist. Return warnings.

    Route edits keep users (a rename copies them), so a discard leaves a route that applies. The
    names come from the overlay, not only `/data/config`, so a route staged since this apply
    keeps its users.
    """
    try:
        with store.lock() as held:
            paths = PendingChanges(store).overlay_files(held)
            routes = {Path(path).stem for path in paths if path.startswith("routes/")}
            unused = store.load_secrets().basic_auth.keys() - routes
            if not unused:
                return []
            tree = load_tree(store.secrets_path)
            for name in unused:
                del tree["basic_auth"][name]
            store.save_secrets(tree, held)
    except (OSError, StoreError) as error:
        logger.error("cannot drop the basic auth users of removed routes: %s", error)
        return ["the basic auth users of removed routes could not be dropped; see the log"]
    logger.info("dropped the basic auth users of removed routes: %s", ", ".join(sorted(unused)))
    return []


def _follow_certificates(services: Services) -> tuple[list[str], list[str]]:
    """Issue missing certificates, and all of them after a CA switch, and remove unused ones.
    Return job ids and warnings."""
    certificates = services.certificates
    try:
        plan = certificates.plan()
        missing = [spec.name for spec in plan if certificates.needs_issue(spec)]
    except (OSError, StoreError) as error:
        logger.error("cannot plan certificates after an apply: %s", error)
        return [], ["the certificates could not be checked; see the log"]
    warnings = _remove_stale_bundles(services, {spec.name for spec in plan})
    jobs = [submit_issue(services.jobs, certificates, name).id for name in missing]
    return jobs, warnings


def _notify_failure(store: Store, stage: Stage, errors: tuple[str, ...]) -> None:
    # From `apply` on, the old release went back live.
    title = "Apply rolled back" if stage in ("apply", "verify") else f"Apply failed at {stage}"
    notify(store, "apply_failed", title, "\n".join(errors) or "no error was given")


def _submit_cloudflare(services: Services) -> str | None:
    """Queue the Cloudflare reconcile and DDNS while the integration is on. Return the
    reconcile's job id."""
    try:
        if not services.pipeline.store.load_config().cloudflare.enabled:
            return None
    except (OSError, StoreError) as error:
        logger.error("cannot read the Cloudflare toggle after an apply: %s", error)
        # The jobs read the config again and show why they failed.
    services.ddns.submit(services.jobs)
    return services.reconcile.submit(services.jobs)


@router.post("/apply", response_model=ApplyResponse, responses={401: {"description": "No session"}})
def apply(services: ServicesDep) -> ApplyResponse:
    """Apply the pending changes, or re-apply the saved config when nothing is pending.

    A failed apply answers 200 with `success` false: the old config stays live and the changes
    stay pending. A down upstream is a warning, not a failure.
    """
    result = services.pipeline.apply("admin")
    jobs: list[str] = []
    cloudflare_job = None
    warnings = list(result.warnings)
    if result.success:
        warnings += _drop_unused_basic_auth(services.pipeline.store)
        jobs, more = _follow_certificates(services)
        warnings += more
        cloudflare_job = _submit_cloudflare(services)
        services.reconcile.schedule(services.jobs)
    else:
        _notify_failure(services.pipeline.store, result.stage, result.errors)
    fields = asdict(result) | {"warnings": warnings, "errors": list(result.errors)}
    return ApplyResponse(**fields, certificate_jobs=jobs, cloudflare_job=cloudflare_job)
