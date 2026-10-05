"""Offline checks for the AgentSIM governance boundary."""

import time
from unittest.mock import Mock

import pytest

from jev_ultrafast import (
    Agent,
    GovernancePolicy,
    OutcomeUnverified,
    RouteDenied,
    RunCancelled,
    RunDeadlineExceeded,
)
from jev_ultrafast.browser import fingerprint


def page(url="https://allowed.test/start"):
    state = {
        "url": url,
        "title": "Allowed",
        "text": "Ready",
        "scroll": {"y": 0},
        "actions": [{"id": "e1", "kind": "click", "label": "Continue", "role": "button", "node": 1}],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def decision(choice="e1"):
    return {
        "choice": choice,
        "operation": "CLICK" if choice == "e1" else choice,
        "target": "1" if choice == "e1" else None,
        "confidence": 1.0,
        "probabilities": {choice: 1.0},
        "latency_ms": 1,
        "usage": {},
    }


def policy(**overrides):
    values = {
        "allowed_hosts": frozenset({"allowed.test"}),
        "verifier": lambda _page, _history: True,
    }
    values.update(overrides)
    return GovernancePolicy(**values)


def browser_factory(state=None):
    state = state or page()
    browser = Mock()
    browser.observe.return_value = state
    browser.fresh.return_value = True
    return Mock(return_value=browser), browser


def agent_with(governance, state=None):
    factory, browser = browser_factory(state)
    agent = Agent("https://allowed.test/start", "Continue", browser_factory=factory, governance=governance)
    return agent, browser


def test_governed_run_cannot_fall_back_to_shared_local_chrome():
    with pytest.raises(ValueError, match="isolated browser factory"):
        Agent("https://allowed.test/start", "Continue", governance=policy())


def test_denied_start_route_stops_before_browser_creation():
    factory = Mock()
    with pytest.raises(RouteDenied):
        Agent("https://denied.test/", "Continue", browser_factory=factory, governance=policy())
    factory.assert_not_called()


def test_denied_initial_observation_closes_caller_browser():
    factory, browser = browser_factory(page("https://denied.test/landing"))
    with pytest.raises(RouteDenied):
        Agent("https://allowed.test/start", "Continue", browser_factory=factory, governance=policy())
    browser.close.assert_called_once()


def test_redirect_to_denied_route_blocks_before_another_action():
    allowed = page()
    denied = page("https://denied.test/landing")
    factory, browser = browser_factory(allowed)
    browser.observe.side_effect = [allowed, denied]
    agent = Agent("https://allowed.test/start", "Continue", browser_factory=factory, governance=policy())
    agent.state["decision"] = decision()
    agent.state["status"] = "predicted"
    agent.state["started_at"] = time.perf_counter()
    with pytest.raises(RouteDenied):
        agent.command("act", {"fingerprint": allowed["fingerprint"]})
    assert agent.state["status"] == "blocked"
    assert agent.state["blocked_reason"] == "route_denied"
    browser.act.assert_called_once()


def test_cancellation_stops_before_browser_mutation():
    cancelled = False
    agent, browser = agent_with(policy(cancel_check=lambda: cancelled))
    agent.state["decision"] = decision()
    agent.state["status"] = "predicted"
    agent.state["started_at"] = time.perf_counter()
    cancelled = True
    with pytest.raises(RunCancelled):
        agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    browser.act.assert_not_called()
    assert agent.state["blocked_reason"] == "cancelled"


def test_deadline_stops_before_model_call(monkeypatch):
    ticks = iter([0, 0, 0, 2, 2])
    agent, _browser = agent_with(policy(deadline_seconds=1, clock=lambda: next(ticks)))
    choose = Mock()
    monkeypatch.setattr("jev_ultrafast.agent.choose", choose)
    agent.state["started_at"] = time.perf_counter()
    with pytest.raises(RunDeadlineExceeded):
        agent.command("predict")
    choose.assert_not_called()


def test_cancellation_during_model_call_blocks_before_decision_is_usable(monkeypatch):
    control = {"cancelled": False}
    agent, browser = agent_with(policy(cancel_check=lambda: control["cancelled"]))

    def choose_and_cancel(*_args):
        control["cancelled"] = True
        return decision()

    monkeypatch.setattr("jev_ultrafast.agent.choose", choose_and_cancel)
    with pytest.raises(RunCancelled):
        agent.command("predict")
    assert agent.state["decision"] is None
    assert agent.state["blocked_reason"] == "cancelled"
    browser.act.assert_not_called()


def test_done_requires_independent_verification():
    agent, _browser = agent_with(policy(verifier=lambda _page, _history: False))
    agent.state["decision"] = decision("DONE")
    agent.state["status"] = "predicted"
    agent.state["started_at"] = time.perf_counter()
    with pytest.raises(OutcomeUnverified):
        agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    assert agent.state["status"] == "blocked"
    assert agent.state["blocked_reason"] == "outcome_unverified"


def test_verified_done_emits_query_free_receipt():
    receipts = []
    state = page("https://allowed.test/result?token=secret#fragment")
    agent, _browser = agent_with(policy(receipt_sink=receipts.append), state)
    agent.state["decision"] = decision("DONE")
    agent.state["status"] = "predicted"
    agent.state["started_at"] = time.perf_counter()
    result = agent.command("act", {"fingerprint": state["fingerprint"]})
    assert result["status"] == "done"
    receipt = next(item for item in receipts if item["event"] == "outcome_verified")
    assert receipt["url"] == "https://allowed.test/"
    assert "secret" not in str(receipts)
