# Changelog

## 0.19.0 — 2026-10-06
- **OrgForge is now Vittics Builder.** The command is `vittics-builder`, the Python package
  `vittics_builder`, settings are `VITTICS_*`, a company's data folder is `.vittics/`, the desktop app
  is *Vittics Builder* (`~/VitticsBuilder`), and the repository is Ramch16/vittics-builder.
- Carried over for one release: `orgforge` still runs (with a note); `ORGFORGE_*` variables still
  work; a company's `.orgforge/` folder is renamed to `.vittics/` when first opened; a paired worker's
  `~/.orgforge/worker.json` moves to `~/.vittics/`; the desktop app keeps an existing `~/OrgForge`;
  CLI agents may still answer with an `orgforge` block. Hosted servers now keep data in
  `/var/lib/vittics-builder` (same `company` volume).
- A company that was moved (or whose hosted data path changed) finds its projects' workspaces in its
  own `workspaces/` folder and updates the saved paths.

## 0.18.0 — 2026-10-06
- **Windows.** The tests now run on Windows (and macOS) in CI, and pass. Commands (product checks,
  deploy and health commands, agents' commands) run in Git Bash from Git for Windows, never WSL's
  bash. CLIs installed by npm as `.cmd` wrappers are started through their Node script so cmd.exe
  never re-reads agents' prompts; other batch wrappers are refused when an argument could inject
  commands. The doctor recognises the Microsoft Store's python shortcut; workspaces enable long
  paths; Windows commands are added to the denied list.
- Fixed on Windows: files and command output used the legacy encoding (the dashboard did not load);
  arena benchmark fixtures were not pinned (paths used backslashes); agents' files had their line
  endings changed; listed paths used backslashes.
- **Desktop installers in CI.** A workflow builds the Windows installer (NSIS `.exe`) and the macOS
  `.dmg`, after smoke-testing the bundled backend on each; both unsigned.

## 0.17.0 — 2026-10-06 (released with 0.18.0)
- **Desktop app (macOS).** A Tauri app (`desktop/`) that starts a bundled OrgForge backend
  (PyInstaller) and shows the dashboard: company in `~/OrgForge` created on first launch, automatic
  sign-in, a CEO/CTO switch, and a first-run welcome (check this computer, then submit an idea).
  The backend listens on 127.0.0.1 only, loads the login shell's PATH so Homebrew tools and AI CLIs
  are found when opened from Finder, and exits when the app quits, even if the app is killed. The
  dashboard gets no Tauri APIs. Builds `OrgForge.app` and a `.dmg`; not signed yet.
  New: `orgforge desktop`, the `desktop` extra.

## 0.16.0 — 2026-10-06 (released with 0.18.0)
- **Worker machines.** Other computers can run agents' coding CLIs for the company: pair with a
  one-time code (`orgforge machines pair`, `orgforge worker join`), assign CLI agents to them, and
  their CLI work runs there on that machine's logins while reviews and checks stay here. Workers
  connect out and need no open port; pairing proves the code both ways without sending it; every
  request and reply is signed with a timestamp and single-use nonce; jobs name an engine, never a
  command; workspaces travel as archives without dependency folders and changes come back as
  patches. An offline worker hands the turn back to this computer when the engine is installed
  here. Machine in the dashboard pairs, assigns and revokes.

## 0.15.0 — 2026-10-06 (released with 0.18.0)
- **Machine readiness.** `orgforge doctor` and a Machine page show what the company and each
  project need (from agents' engines, the sandbox, project files, `product.json` and production
  commands), what is installed and at which version, whether Docker is running, and whether
  Claude Code, Codex and the GitHub CLI are signed in. Missing tools install with consent, one at a
  time, from a fixed catalog via Homebrew, winget or npm; admin-only installs (Docker Desktop,
  Homebrew, Apple's command line tools, apt) are shown as commands to run. Never on a hosted
  server. Failed checks that hit "command not found" now name the missing tool, and approving a
  design warns about tools the project needs. macOS command line tool stubs are detected without
  triggering their install dialog.

## 0.14.0 — 2026-10-06
- **The learning loop runs itself.** Learning and routing are on by default (no change without
  evidence or rules). Every review verdict is settled by what happened next (the CTO's call on an
  escalation, failing release checks, an accepted release) and scores reviewers and QA: per ticket
  for escalations, once per reviewer for release-level results so one failed release cannot get a
  reviewer fired. After repeated failed reviews of the same kind of work across tickets, an
  architect proposes an approach (`submit_strategy`); once the CTO approves it, it competes with
  the current one on reviewed results. New: `orgforge learning`, Reviewer accuracy and learned
  approaches under Company knowledge, `learning.propose_after`, `learning.score_reviewers`.
- **Production stage.** A project with a `production:` entry in `org.yaml` is deployed after the
  CEO signs off: environment by environment, each with a deploy command, a health check with
  retries, an optional CEO/CTO go-ahead and automatic rollback to the last healthy version. Failures
  file an urgent incident and go to the CTO (retry, or send back with guidance). When every
  environment is healthy the project is *Live in production*, optionally monitored by a scheduled
  health check whose outages become incident tickets. Deploy commands are set by people only, get
  only the secrets listed under `env:`, and never run on a workspace that changed after sign-off.
  New: `orgforge deployments`, a Production card on each project, `deployments` table.
- **Engine failover.** With `failover.enabled`, an engine that hits its usage limit rests until
  its stated reset time (or `cooldown_minutes`) and the work continues on the next ready model in
  `failover.models`; the project pauses only when every option is resting. Switches are logged,
  recorded in routing decisions, and shown under Company knowledge and in `orgforge engines`;
  `orgforge engines --wake` tries a resting engine again. Off by default.
- Anthropic API rate limits and overloads (after the client's retries), HTTP 429/503 from
  OpenAI-compatible endpoints, and an unreachable local model server now count as usage limits:
  the project pauses (or fails over) instead of logging an error.
- **Codex tested.** Verified against Codex CLI 0.160.1 on a ChatGPT plan with a full task (build,
  code review, QA, checks). Fixed: builders used `--full-auto`, which Codex removed (now
  `--sandbox workspace-write`); prompts go on stdin instead of the command line; usage limits are
  found after Codex's echoed prompt, with their "try again in/at" time; token totals are recorded.

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
