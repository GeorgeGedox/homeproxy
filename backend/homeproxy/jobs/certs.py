"""Certificate jobs: issuance on request, and the daily renewal check."""

from datetime import UTC, datetime, timedelta

from homeproxy.certs.service import CertificateService
from homeproxy.jobs.models import Job
from homeproxy.jobs.runner import JobRunner
from homeproxy.notify import notify_on_failure

ISSUE_KIND = "certificate.issue"
RENEW_KIND = "certificate.renew"

RENEWAL_INTERVAL = timedelta(days=1)
FIRST_RENEWAL_DELAY = timedelta(minutes=5)
"""Each start resets the interval, so a container that restarts daily would never reach a run
one day after start. The first check runs soon after start instead."""
RENEWAL_JITTER = timedelta(hours=1)
"""Spreads the load on Let's Encrypt, in place of lego's own random sleep."""


def submit_issue(
    runner: JobRunner, service: CertificateService, name: str, *, force: bool = False
) -> Job:
    """Queue issuance of one certificate, or a renewal with `force`, and return its job.

    A failure sends a `certificate_failed` notification.
    """

    def issue() -> None:
        title = f"Certificate {name} failed"
        notify_on_failure(
            service.store, "certificate_failed", title, lambda: service.renew(name, force=force)
        )

    # lego runs one at a time, so a queue of issues must not hold every worker.
    return runner.submit(ISSUE_KIND, issue, subject=name, serial=True)


def renew_due(service: CertificateService) -> None:
    """Issue missing certificates and renew those due. A failure sends one `certificate_failed`
    notification that names each certificate that failed."""
    notify_on_failure(
        service.store, "certificate_failed", "Certificate renewal failed", service.renew_all
    )


def schedule_renewal(runner: JobRunner, service: CertificateService) -> None:
    """Check every certificate daily, first a few minutes after start, and renew those due."""
    runner.schedule_every(
        RENEW_KIND,
        lambda: renew_due(service),
        interval=RENEWAL_INTERVAL,
        first_run=datetime.now(UTC) + FIRST_RENEWAL_DELAY,
        jitter=RENEWAL_JITTER,
        serial=True,
    )
