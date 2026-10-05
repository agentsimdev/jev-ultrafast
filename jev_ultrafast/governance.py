"""Fail-closed controls around the browser loop.

Network isolation remains the browser supervisor's responsibility. These
controls prevent a governed run from silently falling back to the shared local
Chrome path and stop model calls or browser actions when the application has
cancelled, expired, or routed the run outside its declared host set.
"""

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


class GovernanceError(RuntimeError):
    """A governed run stopped before another model call or browser action."""

    code = "governance_error"


class RunCancelled(GovernanceError):
    code = "cancelled"


class RunDeadlineExceeded(GovernanceError):
    code = "deadline_exceeded"


class RouteDenied(GovernanceError):
    code = "route_denied"


class OutcomeUnverified(GovernanceError):
    code = "outcome_unverified"


def safe_url(url):
    """Keep origin evidence without paths, query strings, or user info."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, "/", "", ""))


@dataclass(frozen=True)
class GovernancePolicy:
    allowed_hosts: frozenset[str]
    verifier: object
    cancel_check: object = field(default=lambda: False, repr=False)
    receipt_sink: object = field(default=lambda _receipt: None, repr=False)
    deadline_seconds: float = 120
    max_steps: int = 60
    clock: object = field(default=time.monotonic, repr=False)

    def __post_init__(self):
        hosts = frozenset(host.strip().lower().rstrip(".") for host in self.allowed_hosts if host.strip())
        if not hosts:
            raise ValueError("Governed runs need at least one allowed host")
        if any("/" in host or ":" in host for host in hosts):
            raise ValueError("Allowed hosts must be hostnames without schemes, ports, or paths")
        if not callable(self.verifier):
            raise ValueError("Governed runs need an independent outcome verifier")
        if not callable(self.cancel_check) or not callable(self.receipt_sink) or not callable(self.clock):
            raise ValueError("Governance callbacks must be callable")
        if not 0 < self.deadline_seconds <= 3600:
            raise ValueError("deadline_seconds must be between 0 and 3600")
        if not 0 < self.max_steps <= 120:
            raise ValueError("max_steps must be between 1 and 120")
        object.__setattr__(self, "allowed_hosts", hosts)

    def start(self):
        return RunGuard(self, self.clock())


class RunGuard:
    def __init__(self, policy, started_at):
        self.policy = policy
        self.started_at = started_at

    def checkpoint(self, stage, page=None):
        if self.policy.cancel_check():
            raise RunCancelled("Run cancelled; no further model call or browser action executed")
        if self.policy.clock() - self.started_at >= self.policy.deadline_seconds:
            raise RunDeadlineExceeded("Run deadline exceeded; no further model call or browser action executed")
        if page is not None:
            self.check_route(page["url"])
        self.receipt("checkpoint", stage=stage, page=page)

    def check_route(self, url):
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or host not in self.policy.allowed_hosts:
            raise RouteDenied("Observed route is outside the governed host set")

    def verify(self, page, history):
        self.checkpoint("before_outcome_verification", page)
        try:
            verified = self.policy.verifier(page, tuple(history)) is True
        except Exception as error:
            raise OutcomeUnverified("Independent outcome verification failed") from error
        if not verified:
            raise OutcomeUnverified("Independent outcome verification did not confirm completion")
        self.receipt("outcome_verified", page=page, actions=len(history))

    def receipt(self, event, *, page=None, **fields):
        receipt = {"event": event, **fields}
        if page is not None:
            receipt["url"] = safe_url(page["url"])
        self.policy.receipt_sink(receipt)
