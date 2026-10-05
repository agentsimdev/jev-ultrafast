# AgentSIM governed runtime boundary

This fork keeps Jev Ultrafast's indexed action loop and adds fail-closed
application controls around it. These controls are a library contract, not
evidence that a production browser worker or network boundary is deployed.

```text
AgentSIM policy and revocation
        |
        v
GovernancePolicy ---- cancellation, deadline, exact hosts, verifier
        |
        v
Jev indexed action loop
        |
        v
caller-owned isolated browser worker ---- required, not implemented here
        |
        v
AgentSIM signed egress gateway --------- required, enforced outside this library
```

## What this fork enforces

- A governed run cannot silently attach to the default shared local Chrome.
  The caller must provide its browser factory.
- Exact allowed hosts are checked before browser creation, before model calls,
  before actions, and after observations.
- Cancellation and deadlines stop before the next model call or mutation.
- `DONE` becomes `done` only after a caller-provided verifier returns `True`.
- Governance receipts omit query strings, fragments, typed values, page text,
  credentials, and model inputs.
- Step and model-call budgets remain bounded and may be tightened per run.

## What the AgentSIM supervisor must enforce

- One isolated Chromium profile and process boundary per run.
- A forced HTTP CONNECT path through the signed AgentSIM gateway, with no
  direct network fallback.
- Short-lived proxy credentials tied to the AgentSIM principal and run.
- Worker termination and profile deletion after completion, cancellation, or
  deadline expiry.
- Independent gateway and worker evidence for allowed, denied, and revoked
  traffic. Passing library tests does not prove these runtime properties.

## Integration shape

```python
from jev_ultrafast import Agent, GovernancePolicy


policy = GovernancePolicy(
    allowed_hosts=frozenset({"app.example.test"}),
    cancel_check=lambda: run_store.is_cancelled(run_id),
    verifier=lambda page, history: verify_expected_result(page, history),
    receipt_sink=lambda receipt: run_store.append_receipt(run_id, receipt),
    deadline_seconds=120,
    max_steps=40,
)

with Agent(
    "https://app.example.test/",
    "Complete the bounded test workflow",
    browser_factory=lambda url: isolated_worker.attach(url, run_id=run_id),
    governance=policy,
) as agent:
    for state in agent.run():
        persist_progress(run_id, state)
```

The factory is the trust boundary. AgentSIM must not mark a run verified merely
because a factory was supplied; deployment evidence must prove isolation and
forced gateway routing independently.
