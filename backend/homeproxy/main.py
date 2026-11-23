"""FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from homeproxy.api import (
    apply,
    audit,
    auth,
    basic_auth,
    certificates,
    changes,
    cloudflare,
    credentials,
    dns,
    domains,
    geoip,
    history,
    jobs,
    keys,
    logs,
    metrics,
    notifications,
    routes,
    security,
    settings,
    setup,
    status,
)
from homeproxy.api.apply import Services
from homeproxy.api.errors import install_error_handlers
from homeproxy.api.metrics import collect_metrics
from homeproxy.auth import install_auth
from homeproxy.auth.runtime import data_root
from homeproxy.certs.service import CertificateService
from homeproxy.geoip.lookup import CountryLookup
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.haproxy.pipeline import Pipeline
from homeproxy.haproxy.version_check import releases_url
from homeproxy.jobs import JobRunner
from homeproxy.jobs import geoip as geoip_jobs
from homeproxy.jobs.certs import schedule_renewal
from homeproxy.jobs.cloudflare_ranges import schedule_refresh
from homeproxy.jobs.ddns import Ddns
from homeproxy.jobs.heartbeat import Heartbeat
from homeproxy.jobs.reconcile import Reconcile
from homeproxy.jobs.state_alerts import StateAlerts
from homeproxy.jobs.update_check import UpdateCheck, update_url
from homeproxy.jobs.version_check import VersionCheck
from homeproxy.logs import LogCollector
from homeproxy.logs.files import LogFiles
from homeproxy.store import Store, StoreError
from homeproxy.ui import mount_ui

logger = logging.getLogger(__name__)


class Health(BaseModel):
    """Liveness response."""

    status: str


router = APIRouter(prefix="/api/v1")


@router.get("/healthz", response_model=Health)
def healthz() -> Health:
    """Report that the API process is alive. Needs no authentication."""
    return Health(status="ok")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the services, finish an interrupted promote, and start the jobs and the log collector.

    A part that fails to start is logged, and the API still starts. HAProxy never depends on it.
    """
    root = data_root()
    client = HAProxyClient()
    store = Store(root)
    pipeline = Pipeline(store, client)
    runner = JobRunner()
    certificates = CertificateService.from_environ(root, client)
    services = Services(pipeline, certificates, runner, Reconcile(store), Ddns(store))
    app.state.services = services
    app.state.version_check = VersionCheck(
        lambda: client.show_info().version, releases_url(), store=store
    )
    app.state.update_check = UpdateCheck(update_url(), store=store)
    try:
        await run_in_threadpool(pipeline.recover)
    except (OSError, StoreError) as error:
        # The API must still start, so the admin can see and discard the pending changes.
        logger.error("cannot finish an interrupted apply: %s", error)
    runner.start()
    schedule_renewal(runner, certificates)
    app.state.version_check.schedule(runner)
    app.state.update_check.schedule(runner)
    schedule_refresh(runner, store, client)
    geoip_jobs.schedule_refresh(runner, pipeline)
    services.reconcile.schedule(runner)
    services.ddns.schedule(runner)
    Heartbeat(store, client.show_info).schedule(runner)
    StateAlerts(store, client.show_stat, certificates.certificates).schedule(runner)
    files = LogFiles(root)
    collector = LogCollector(countries=CountryLookup(root), other=files.haproxy)
    collector.subscribe(files.access)
    app.state.collector = collector
    async with collect_metrics(collector, root / "state" / "metrics.db") as metrics:
        app.state.metrics = metrics
        try:
            await collector.start()
        except OSError as error:
            # HAProxy drops its access lines until a collector binds the socket, and keeps serving.
            logger.error("cannot start the log collector: %s", error)
        try:
            yield
        finally:
            collector.close()
            files.close()
            runner.shutdown()


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    app = FastAPI(
        title="HomeProxy", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.include_router(router)
    for module in (
        setup,
        auth,
        settings,
        keys,
        basic_auth,
        routes,
        domains,
        credentials,
        changes,
        apply,
        history,
        status,
        certificates,
        dns,
        cloudflare,
        security,
        geoip,
        jobs,
        audit,
        metrics,
        logs,
        notifications,
    ):
        app.include_router(module.router)
    install_error_handlers(app)
    # Every /api/ path except healthz, setup and login needs a session from here on.
    install_auth(app)
    mount_ui(app)
    return app


app = create_app()
