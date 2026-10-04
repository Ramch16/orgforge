# Changelog

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
