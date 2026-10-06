# Phase 1: company brain and execution foundations

OrgForge now extends its existing organization with adaptive project teams,
searchable company knowledge, evidence-based model selection, bounded autonomous
work, MCP integrations, and native browser journeys. The CEO/CTO approval gates,
review/rework policy, acceptance contract, and project budgets remain in force.
These foundations do not claim feature parity with Ruflo.

## Install and try it

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,mcp,browser]'
# Use an installed Chromium, or install Playwright's matching browser:
playwright install chromium
python -m pytest -q

mkdir -p ../mycompany
ORGFORGE_PROVIDER=mock orgforge --home ../mycompany init
ORGFORGE_PROVIDER=mock orgforge --home ../mycompany serve
```

Browser journeys prefer Playwright's bundled Chromium. When it is absent, they
use an installed `chromium` or `chromium-browser`. Set
`tools.browser.executable_path` or `ORGFORGE_BROWSER_EXECUTABLE` to choose another
Chromium executable. An explicit path takes precedence. System Chromium versions
can differ from Playwright's pinned version; the bundled browser is the most
portable choice. Browser installation also needs its host OS libraries.

The dashboard's **Company knowledge** page searches memory, shows project teams
and bottlenecks, and compares model results. Projects with runnable work have a
**Work 5 cycles** action. The CLI provides the same foundations:

```bash
orgforge swarm 1
orgforge memory 'Why PostgreSQL instead of MongoDB?' --project 1 --category architecture
orgforge memory 'authentication failures' --history
orgforge routing
orgforge work 1 --cycles 5 --seconds 300
```

`work` counts one planning stage or one build batch as a cycle. A batch includes
its normal bounded review/rework attempts. The time limit is checked between
cycles, rather than killing a builder or interrupting a Git merge. Individual
model/tool/CLI timeouts remain in force. Calling `work` again resumes the saved
project state. Work stops at approvals, escalation, budget pauses, and release
verification failures; it never approves those decisions itself. `run` retains
its existing run-until-decision behavior.

## Adaptive teams

Teams form from dependency-ready tickets and are persisted per project and task.
Distinct free agents receive each batch, up to `pipeline.max_parallel`; retries
keep the same seat. Existing isolated execution and merging handle parallel
code changes. `delegate` continues to let agents ask other roles for help within
`pipeline.max_delegation_depth`.

Configure additional role capabilities in the company's **local** `org.yaml`:

```yaml
swarms:
  capabilities:
    frontend_engineer: [backend_engineer]
  auto_staff: false
```

Exact-role staff are preferred; configured substitutes can take overflow work.
This is explicit capability matching, not an LLM inventing staff competencies.
Workload bottlenecks still create human hiring requests by default. Setting
`auto_staff: true` preauthorizes workload-based hiring of existing staffed roles,
limited by `staffing.max_per_role` and `staffing.hire_when_waiting`. Hiring persists
in the company; it is not a temporary increase in concurrency or an automatic
firing policy. Per-project team membership describes ticket assignments, while
reporting lines and human authority stay organizational.

## Persistent memory

The nine memory categories are `business`, `product`, `architecture`, `code`,
`customer`, `security`, `agent`, `failure`, and `decision`. Agents can `remember`
facts with a category, project/company scope, and evidence reference. Failed
reviews are saved as failure memories with ticket and attempt references.

`recall_memory` searches those facts and returns their author, category, source,
and timestamp; read-only project chats can use it too. SQLite FTS5 retrieves old
facts without the former 300-record cutoff. Existing databases are migrated and
indexed automatically. Normal recall includes the current project and
company-wide knowledge. `include_history` / `--history` explicitly includes other
projects. Search is lexical, with ranked keyword matches, not embedding/vector
RAG or a knowledge graph. Memory records supplied by agents are claims with
provenance, not independently verified facts.

## Model routing and review evidence

Routing is off by default. Without a rule, the assigned agent model is retained.
Rules may target a run purpose, role, or role kind, in that precedence order:

```yaml
routing:
  enabled: true
  independent_reviews: true
  rules:
    planner: [claude-sonnet-5-5, claude-opus-5-5]
    builder: [claude-sonnet-5-5, 'cli:codex', 'cli:gemini', 'ollama:qwen2.5-coder']
    reviewer: [claude-haiku-4-5, claude-sonnet-5-5]
    qa_engineer: [claude-haiku-4-5, claude-sonnet-5-5]
```

The selector combines prior reviewed success rate, review quality, run latency,
and estimated cost. New models receive a neutral prior; configuration order
breaks ties. Only explicitly configured candidates are selected. Local readiness
checks exclude absent CLI executables and missing API credential bindings; they
do not prove a login, endpoint reachability, or remote model availability. Remote
provider errors still fail through the existing runtime. Usage limits pause the
project unless `failover` is enabled (0.14, see the README): then the assignment is
replayed on the next configured model, visibly logged, with a note that the
workspace may hold a partial attempt. Without failover, nothing is replayed.

Independent reviews exclude the builder's selected model identifier. If no other
available candidate remains, review fails closed. This enforces a different model
identifier, not necessarily a different vendor or training lineage.

The selected model, selection reason, run status, duration, usage, and estimated
cost are stored. Builder success/quality are populated only after review; simply
finishing a run leaves that evidence unevaluated. The cost ledger records the
selected model and run ID, so parallel runs are attributed separately. Unknown
prices are marked unpriced, and cost per success is unavailable when costs are
unknown or no evaluated run succeeded. The first version learns at role-kind
level; prompt/strategy optimization and task-similarity learning are future work.

Providers reuse the existing Anthropic, OpenAI-compatible endpoint, and CLI
adapters. Gemini API models use `gemini:<model>` and `GEMINI_API_KEY`; Gemini CLI
uses `cli:gemini` with its own login. Configure credentials securely, and use
`llm.prices` for models without built-in prices. Offline mock tests do not certify
live provider access or coding quality.

## MCP and browser tools

Integrations are disabled by default. Enable them per role in local `org.yaml`:

```yaml
tools:
  mcp:
    repository:
      command: /absolute/path/to/mcp-server
      args: []
      roles: [backend_engineer]
      tools: [read_issue, read_file]
      env_names: [GITHUB_TOKEN]
  browser:
    enabled: true
    roles: [qa_engineer, test_automation_engineer]
    timeout_seconds: 30
    # Omit allowed_origins to allow only literal loopback hosts, on any port.
    allowed_origins: ['http://127.0.0.1:8080']
```

MCP uses the official SDK's initialized stdio transport, advertised JSON schemas,
and standard tool result/error handling. Agents see only configured server names;
`mcp_list_tools` discovers only allowlisted tools. `mcp_call` checks both server
role access and tool allowlists. Every operation opens a fresh server session and
closes it afterward. Stateful MCP services must persist their own state. HTTP MCP
transport, resource subscriptions, and OAuth flows are not implemented here.

MCP commands are trusted company configuration, not agent-supplied commands.
Servers run on the host in the project workspace. Credential-like environment
variables are removed unless named explicitly in `env_names`; do not put values
in YAML. Those servers and the browser are separate host integrations and are
not contained in the command tool's Docker sandbox. Configure only servers and
application origins you intend the selected roles to access. The underlying
platform network and credential policy still applies.

A browser journey retains cookies/page state across steps in one fresh context:

```json
{
  "url": "http://127.0.0.1:8080",
  "steps": [
    {"action": "fill", "selector": "#username", "value": "demo-user"},
    {"action": "click", "selector": "button[type=submit]"},
    {"action": "assert_text", "selector": "body", "value": "Welcome"},
    {"action": "screenshot", "path": ".orgforge/browser/onboarding.png"}
  ]
}
```

Journeys support navigation, clicks, fills, text assertions, and PNG screenshots,
with up to 30 steps and a time budget. HTTP(S) requests and redirect destinations
must match the origin policy, service workers are blocked, and WebSockets are
disabled. Screenshots cannot leave the workspace; default browser artifacts are
ignored by Git. Returns include visible text and artifact paths. Assertions or
blocked requests fail the tool, so an open page alone does not constitute
acceptance evidence. Add representative browser checks to the approved product
contract before relying on them as release gates.

Native MCP/browser tools run in the API agent tool loop. Coding CLI engines still
use their own tool systems; this change does not turn their one-shot JSON result
protocol into an interactive MCP/browser bridge. Native desktop control, visual
reasoning over screenshots remain future extensions. Scheduled workers, competition,
observations, customers, board reviews, packages, federation and hosting are now
documented in [Phase 2](PHASE2.md) and [Phase 3](PHASE3.md).

## API and validation

All new endpoints use the existing CEO/CTO authentication:

- `GET /api/projects/{id}/swarm`
- `GET /api/memories?query=...&project_id=...&category=...&include_history=true`
- `GET /api/routing`
- `POST /api/projects/{id}/work?cycles=5&seconds=300`

The test suite covers legacy SQLite migration, cross-project retrieval, old
memory retrieval, team formation/capability matching, staffing caps, bounded
resume/approval boundaries, model selection/review attribution, authenticated
APIs, tool permissions, real stdio MCP requests, and native Chromium journeys.
Optional MCP/browser integration tests explicitly skip when their dependencies
or browser executable are absent. Install the extras to run those checks.
