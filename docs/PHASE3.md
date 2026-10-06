# Product validation and connected companies

Phase 3 adds customer personas, an independent board, pinned agent packages,
signed peer exchanges and a single-company hosting distribution.

## AI customers

Enable the native browser in the company's `org.yaml` and install the browser
extra (the Docker image already includes it):

```yaml
tools:
  browser:
    enabled: true
    allowed_origins: ["http://127.0.0.1:8000"]
    timeout_seconds: 30
```

Start the product separately. Provide `customers.json`:

```json
{
  "beginner": {
    "url": "http://127.0.0.1:8000",
    "goal": "Complete onboarding and confirm the dashboard shows my account."
  },
  "developer": {
    "url": "http://127.0.0.1:8000",
    "steps": [
      {"action":"click","selector":"#help"},
      {"action":"assert_text","selector":"main","value":"API documentation"}
    ]
  }
}
```

```sh
orgforge --home /path/to/company assess customers 1 --journeys customers.json
```

Supported personas: `enterprise_admin`, `developer`, `beginner`,
`security_officer`, `end_user`. A goal commissions a persona agent in the AI
Customers department to read product source/documentation and propose a bounded
journey. A separate native Chromium executor performs it and records actual
assertions, page text and optional screenshots. Explicit steps provide repeatable
regression checks without a planning model. Every journey requires a text
assertion. Failures become backlog product tickets; planning failures are recorded
as failed assessments. The mock provider uses a scripted onboarding journey for
testing; real persona planning requires your configured model.

This is browser validation with model-planned steps, not general desktop control
or an adaptive visual agent. The application must be reachable from the company
process (inside its container when hosted). Authentication may be represented by
fill/click steps; use disposable test accounts and avoid private credentials in
persistent scenario files. Browser origin restrictions apply to every request.

## Board of Directors

```sh
orgforge --home /path/to/company assess board 1
orgforge --home /path/to/company inbox --as ceo
```

Three separate advisor seats (CTO, security, customer) evaluate functionality, UX,
security, cost, architecture, scalability, customer value and maintainability.
They have read-only tools, structured score/findings submission and project
budget enforcement. Agents who authored project work are excluded. Board seats
are created on first use and stay outside ordinary pipeline reviewer selection.
Scores are model judgments, distinct from executable browser/benchmark evidence.

Commit product changes before requesting review. The board pins the Git revision;
dirty files or changed commits invalidate its pending approval. The CEO reviews
evidence and approves or rejects through the existing inbox. Rejection creates a
backlog feedback ticket. Board acceptance does not authorize deployment or bypass
release checks. Independence refers to seats and task authorship; configure
separate models under routing rules for `board_cto`, `board_security` and
`board_customer` if you also want provider/model diversity.

## Agent package marketplace

The first marketplace is a reviewed local catalog of immutable, versioned role
and Markdown skill bundles. Example `package.json`:

```json
{
  "manifest": {"name":"company/test-guidance","version":"1.0.0"},
  "assets": {"test-guidance.md":"# Regression tests\nExercise behavior and failures; keep tests independent."}
}
```

```sh
orgforge --home /path/to/company marketplace register --bundle package.json
orgforge --home /path/to/company marketplace install \
  --name company/test-guidance@1.0.0 --sha256 REVIEWED_SHA256
```

Review the returned checksum and complete bundle before installation. Bundles can
include `roles` with `id`, existing `department`, supported `kind`, `tools`,
`prompt`, optional `title`. Installation cannot overwrite existing roles or
skills, run executable hooks, or hire agents automatically. Invalid bundles roll
back their new files and role changes. Remote registration and installation are
CEO-only. There is no public marketplace server, payments, executable plugin
registry or package-signing authority in this implementation.

## Federation

Each company explicitly configures peer identity, secret environment binding,
project IDs, accepted message kinds and permitted proposal roles:

```yaml
federation:
  enabled: true
  identity: company-a
  peers:
    company-b:
      url: https://company-b.example.com
      key_env: ORGFORGE_PEER_B_KEY
      projects: [1]
      capabilities: [observation, task_proposal, task_result]
      roles: [backend_engineer]
```

Bind a distinct shared signing key of at least 32 characters for each peer pair;
the receiving company trusts the sender with the same key. Sender and recipient
identities must agree. Payload project IDs refer to the receiving company's IDs.
Keys stay out of configuration and messages. Peers may accept different scopes in
each direction.

```sh
orgforge --home /path/to/company federation send --peer company-b \
  --kind task_proposal --payload proposal.json
```

A proposal payload contains `project_id`, `title`, `body`, `role`. Observations
contain `project_id`, `title`, `body`, optional `severity` and `event_key`. Task
results contain `project_id`, `ticket_id`, `body`.

Messages use canonical JSON, HMAC-SHA256, recipient checks, five-minute expiry,
a 64KB limit and persisted idempotent receipts. Email/credential markers are
redacted before signing and after verification; accepted messages are audited.
Transport requires verified HTTPS; loopback HTTP is allowed for local tests and
redirects are rejected. `/api/federation/inbox` authenticates the peer signature
rather than CEO/CTO tokens. `sign` and `receive` CLI actions support reviewed
file-based transfer.

Proposals enter backlog. Results append evidence for human/local agent review;
they cannot mark work complete or ship it. This connects companies through scoped
work requests and results. Distributed execution provisioning, automatic mesh
scheduling, PKI identity, comprehensive PII filtering and remote sandbox control
are future extensions.

## Cloud hosting

Use the [Docker and Caddy deployment guide](DEPLOYMENT.md). It preserves company
memory, workers, packages and project workspaces in a persistent volume. Hosting
is single-company and single-instance; public SaaS tenancy and multi-node SQLite
replication are outside this distribution.
