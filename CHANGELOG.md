# Changelog

## 0.13.0 — 2026-10-06
- Reviewed strategy trajectories, task-specific routing evidence and cost per successful run.
- Persistent scheduled employees with leases, heartbeats, retries and dashboard lifecycle controls.
- Immutable benchmark competitions with separate candidate artifacts and no automatic promotion.
- Scoped production observations, deduplicated incident tickets, local credential scans, optional OSV and executable red-team checks.
- Model-planned customer personas verified in Chromium; independent eight-dimension board reviews with CEO decisions and stale-revision checks.
- SHA256-pinned local agent/skill catalog and signed, scoped, replay-safe federation proposals/results.
- Nonroot Docker hosting, persistent company volume, health checks, SQLite backups and Caddy HTTPS deployment files.
- Adaptive project teams with persisted assignments, configured capability matching,
  bottleneck visibility, and opt-in staffing bounded by existing role caps.
- Nine-category persistent memory with evidence references, SQLite FTS5 retrieval,
  migration of old memories, cross-project search, and failed-review lessons.
- Opt-in model selection from reviewed success/quality, latency and estimated costs;
  selected-model usage attribution and optional independent review models.
- Bounded `work` cycles that resume project state while preserving approval gates.
- Optional role-scoped stdio MCP tools and native Chromium journeys with origin
  restrictions, executable assertions and workspace screenshots.
- Company knowledge dashboard for memory, teams and model performance, with
  authenticated APIs and setup documentation in `docs/PHASE1.md`.


## 0.12.0
- **Skills.** Guidelines agents work by, by kind of role and department, on every engine. Built
  in: the Karpathy coding guidelines (andrej-karpathy-skills, MIT), on for technical roles. Add
  your own in the company's `skills/` folder or with `orgforge skills add <file-or-url>`.
- **Router endpoints.** `omniroute:auto` (OmniRoute) and `freellmapi:auto` (FreeLLMAPI), local
  OpenAI-compatible routers to many free and paid providers.

## 0.11.0
- **Quick tasks** (`orgforge task`, New task): one change straight to build, optionally on an
  existing repository or a GitHub issue, on an `orgforge/task-N` branch, with your checks and a
  CTO review of the changes; delivered as a patch or branch. Agents never push.
- **Watch and steer agents.** Every run is logged live; the agent console streams it and the CEO
  or CTO can message an agent mid-task. Diffs per ticket, commit and task.
- **More engines.** Codex, Gemini CLI, GitHub Copilot CLI, Cursor, OpenCode and Qwen Code presets
  (from their docs, marked untested). Local models through Ollama and LM Studio, and OpenAI,
  OpenRouter and Groq keys, through any OpenAI-compatible endpoint, per agent.
- **MIT licence.**
- Fix: product repositories ignore caches and build output; on most machines committed
  `__pycache__` files made parallel merges conflict.

## 0.10.0
- **CLI engines.** Agents can work through coding CLIs on their own logins instead of the
  pay-per-token API. Claude Code is built in (uses your Claude subscription; OrgForge removes
  `ANTHROPIC_API_KEY` so the CLI bills its own login). Add Codex, Gemini or any CLI under
  `engines:` in `org.yaml`. Read-only roles get read-only tools; reviewers may run tests but not
  edit. `orgforge engines --test` checks an engine; `orgforge org set-model --all cli:claude-code`
  moves the team.
- **Cheaper reviews.** `pipeline.review_mode`: `standard` (default; code review and QA per
  ticket), `thorough` (every reviewer role) or `light` (one reviewer). Release QA and audits
  always run.
- **Shared memory.** Agents `remember` lasting facts and decisions; the most relevant ones are
  given to later agents on the project and company-wide.
- **Office.** A live floor of departments and desks showing who is working, active or idle and
  on what, with live telemetry (calls, tokens, cost today, a 12-hour activity chart).
- **Installable app.** The dashboard installs as an app (manifest, icons, service worker);
  `orgforge app` starts it in its own window.
- **Project health.** Continuous integration on GitHub, issue and pull request templates,
  `CONTRIBUTING.md`, `SECURITY.md` and this changelog.

## 0.9.0
- Redesigned dashboard: sidebar navigation, Home overview, a page per project, project cards,
  team cards, collapsible decisions, toasts, links that survive refresh, light and dark, phones.

## 0.8.x
- Status questions in chat are answered from the company's records; ticket owners reply when the
  CEO or CTO files or comments; question tickets are answered and closed, never built.
- Costs and budgets, status reports, product review page with release download, customer
  feedback triage and numbered versions (`v1`, `v2`, ...).

## 0.7.0
- Ideas are assessed by product (with engineering, marketing and legal input), signed off by the
  CTO, decided by the CEO, and planned across departments before they are built.

## 0.6.0
- The CEO and CTO can chat with any agent; requests become tickets.

## 0.5.0
- Parallel tickets in git worktrees; workload-based hiring requests; teammates name new hires.

## 0.4.0
- Jira-style ticket tracker used by every agent and department, with handoffs and transfers.

## 0.3.x
- Executable acceptance checks before release; pinned CTO-approved `product.json`.

## 0.2.0
- Full company: design, security, data, reliability, compliance, marketing, support; audits.

## 0.1.0
- AI-staffed software company with a human CEO and CTO.
