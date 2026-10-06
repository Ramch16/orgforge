# Learning and company operations

Phase 2 adds reviewed strategy learning, scheduled employees, benchmark competition,
cost/quality routing, incident ingestion and executable security checks. Existing
companies receive additive SQLite tables on startup. Existing release approvals,
budgets and bounded work remain in force.

## Reviewed learning and model costs

Add approved approaches to the company's `org.yaml`:

```yaml
learning:
  enabled: true
  min_trials: 2
  strategies:
    test_first:
      kinds: [builder]
      prompt: Write a regression test, implement the smallest fix, then run the tests.
routing:
  enabled: true
  rules:
    builder: [claude-haiku-4-5, claude-sonnet-5-5]
```

Strategies receive credit from task reviews or immutable arena benchmarks, not
from an agent declaring itself done. Each routed attempt has a strategy, task
kind, coarse task signature, model, elapsed time, token ledger and estimated
cost. Exploration is opt-in and bounded by `min_trials` (1–20). Once strategies
have evidence, smoothed success and quality determine the preferred approach.
Model routing prefers signature-specific evidence when available, falling back
to role-kind history; it considers success, quality, latency and cost.

`vittics-builder --home /path/to/company learn` shows reviewed approaches. The Company
knowledge dashboard shows model evidence, including estimated cost per success.
Configure `llm.prices` for other models: unknown prices stay unpriced rather than
being treated as free. CLI subscription costs and local model estimates are not
provider billing reconciliation. Learning selects configured strategies; it does
not rewrite prompts or install tools on its own.

## Scheduled employees

```sh
vittics-builder --home /path/to/company workers add --name nightly-qa --project 1 \
  --kind check --interval 3600 --config qa-worker.json
vittics-builder --home /path/to/company workers list
vittics-builder --home /path/to/company workers tick
```

`qa-worker.json`:

```json
{"command": "python -m pytest -q"}
```

The Operations dashboard schedules jobs and starts/stops the scheduler. Its
saved enabled state survives a restart; the company web service owns the running
scheduler. Jobs support `check`, `security`, `performance`, `documentation`,
`report`, `work` and `customers`. Command jobs use the project's configured
workspace/sandbox. A security job without a command runs the embedded credential
scan. Customer jobs use a `journeys` object as described in Phase 3. Work jobs
accept `cycles`, capped at 100 and 300 seconds; human approvals remain stops.

A persisted lease and heartbeat prevent two schedulers claiming the same due
job. Interrupted runs become abandoned when the lease expires; failed jobs back
 off up to eight times their interval. Treat command jobs as at-least-once:
choose repeatable checks or idempotent operations. Stopping the scheduler stops
new jobs; a command already executing can finish. Run one company service,
particularly when scheduled work can write product files.

Performance and documentation checks execute your commands. They do not infer
thresholds, alter documentation or inspect production infrastructure without
configured checks. Status-report jobs use the existing report workflow.

## Agent arena

Create `candidates.json` using builder agent IDs from `vittics-builder org`:

```json
[
  {"name":"baseline","agent":5,"strategy":"baseline"},
  {"name":"test-first","agent":6,"strategy":"test_first"}
]
```

```sh
vittics-builder --home /path/to/company arena 1 --goal 'Improve the parser' \
  --candidates candidates.json --check 'python -m pytest tests/test_parser.py -q'
```

Two to eight candidates start from identical copied snapshots. Existing `tests/`,
`benchmarks/` and `product.json` are hash-pinned and checked before and after
execution. A winner must complete and pass every executable check; successful
candidates are compared by known estimated cost, then elapsed time. A budget
interruption produces no winner. Evidence, separate candidate workspaces and the
winning approach are retained. Explicit `model` values override automatic model
selection for that candidate.

Live competitions require Docker command isolation and API models; host CLI
engines are rejected. Mock competitions exercise orchestration locally. Candidates
cannot delegate or use remote integrations. No candidate is automatically merged
or deployed. Benchmark quality depends on the checks you approve; passing them
is evidence about those checks, not a guarantee of overall quality.

## Production observations and security

```sh
vittics-builder --home /path/to/company observe 1 --source production --title 'API error' \
  --body 'Request handler raised an exception' --severity error
vittics-builder --home /path/to/company assess security 1
vittics-builder --home /path/to/company assess red-team 1 \
  --check 'python -m pytest tests/test_auth_denials.py -q'
```

Observations deduplicate by project/source/event key, count repeats and create
backlog bug tickets. Warning/error/critical events need triage; informational
observations do not create tickets. Email addresses and credential assignments
are redacted. Do not submit unnecessary private production data: this is not a
comprehensive PII classifier.

For a separate telemetry producer, configure:

```yaml
observability:
  key_env: VITTICS_OBSERVABILITY_TOKEN
  projects: [1]
```

Bind a token of at least 32 characters and POST to `/api/telemetry/events` with
`X-Observation-Token`. JSON fields: `project_id`, `source`, `title`, `body`,
`severity`, optional `event_key`. This credential cannot access the dashboard.
Admin APIs use existing `X-Token` authentication. Integrations must forward their
logs/metrics/traces explicitly; Vittics Builder does not provision monitoring exporters
or automatically deploy incident fixes.

The bounded local security scan detects embedded Anthropic/OpenAI credential
patterns without storing credential values. Dependency checking optionally uses
OSV for exact-pinned PyPI `requirements.txt`: enable `security.osv_enabled`, allow
HTTPS to `api.osv.dev`, and run `assess dependencies 1`. Remote OSV access requires
your environment's network configuration. Red-team checks run 1–30 explicit
negative-test commands, record actual exit codes and create a critical incident
on failure. Existing security agents still review normal development tickets.

## API

Authenticated endpoints include `/api/operations`, worker creation/tick/service,
`/api/observations`, project `arena`, `red-team` and `security-scan`, plus persisted
arena/assessment evidence. CEO-only package and board endpoints are described in
[Phase 3](PHASE3.md). The browser dashboard exposes scheduler controls, incidents,
strategy evidence, competition/review details and package checksums.
