# Vittics Builder

*Formerly OrgForge. Existing companies, settings and worker machines carry over; see the CHANGELOG.*

A software company staffed by AI agents and run by two people: a CEO and a CTO.

You write a brief. A product manager turns it into requirements, a UX designer
specifies how it is used, an architect designs the system and splits the work,
specialists build it in a real git repository, and code review, security review
and QA check every task. Before release the product is audited for security,
performance and compliance. The humans approve at the points that matter. Agents are scored on every piece of work.
Those who keep failing are put on probation, replaced, and, if the replacement
turns out worse, brought back.

```
brief ─► requirements + UX design ─► CEO ─► architecture + plan ─► CTO ─► build ─► release check + audits ─► CTO ─► CEO ─► shipped
                                                                         │                    │
                        each task: implement ─► code review ─► security review ─► QA          └ findings become fix tasks,
                        fails: rework (max_rework times) ─► escalate to CTO                     then the audits run again
```

## Adaptive company operations

Vittics Builder now supports dynamic teams, persistent company memory, reviewed model/strategy
routing, native MCP/browser tools, scheduled employees, benchmark competitions and
production incident triage. Customer personas plan journeys that Chromium verifies;
an independent board reports eight product dimensions for CEO review. Pinned role/skill
packages and signed peer proposals connect the company to reviewed external work.

Setup and scope: [Phase 1](docs/PHASE1.md), [Phase 2](docs/PHASE2.md),
[Phase 3](docs/PHASE3.md), and [Docker/HTTPS hosting](docs/DEPLOYMENT.md).
The Operations dashboard shows workers, incidents, learned approaches and assessment evidence.

## Verified product delivery (0.3)

Vittics Builder now requires executable acceptance evidence before release. It remains
an AI-assisted development system: it cannot guarantee every idea is feasible,
that generated tests are sufficient, or that a product is production ready.

The architect specifies a `product.json` contract and plans implementation,
end-to-end tests, installation instructions and operational documentation.
After the build, integration QA must submit an explicit approval and report;
every staffed auditor must also complete an approval and report. The pipeline
then executes the contract's checks itself and saves their actual output in
`docs/VERIFICATION.json`. Failures create repair tasks. After `max_rework`
repair rounds, the release is blocked and needs CTO guidance; approval cannot
bypass this gate. Send it back with guidance to start another repair cycle.

A minimal contract looks like:

```json
{
  "name": "Example product",
  "setup": "Install Python 3.11+ and the dependencies in requirements.txt",
  "run": "python app.py",
  "checks": [
    {
      "id": "main-user-journey",
      "requirement": "A user can create, save, and reopen an item",
      "command": "python -m unittest tests.test_user_journey"
    }
  ]
}
```

The CTO approves `product.json` together with the design, and Vittics Builder pins that
exact file. Agents cannot quietly weaken it later: if `product.json` differs from
the approved version when the checks are about to run, the release stops and the
CTO sees a diff. Approving accepts the new checks; sending back restores the
approved file and passes the feedback to the team. Projects designed before
0.3.1 have no pinned contract, so the CTO reviews their `product.json` once.

Commands must terminate, fail with a nonzero exit status on errors, and exercise
real behavior. For a web product, include browser and API checks; for a library,
include representative usage checks. Server checks must start their own test
instance and clean up afterward. The command timeout and sandbox policy apply.
`README.md` and `docs/OPERATIONS.md` must be present and nonempty. Agents choose
the appropriate language and framework; this contract is runtime independent.
Humans must review that its checks cover the brief and are not trivial success
commands. Passing selected checks is evidence, not proof of universal correctness.

The verified Git revision is pinned to release approvals. Editing the product
after verification invalidates approval; send it back to build and verify again.
A completed product is labeled **Ready for deployment**, not as already deployed.
Export its signed-off source, documentation and evidence with:

```bash
vittics-builder export 1 --output ../product-release.zip
```

Export refuses unfinished or modified releases, existing output files, and
output paths within the product workspace. Cloud deployment still requires a
chosen hosting target, credentials, and product-specific configuration.

Other reliability changes: missing review verdicts and exhausted agent runs
fail closed; invalid or cyclic plans are rejected before task creation;
interrupted in-progress tasks can resume within a single running company process;
Git errors now stop work instead of silently succeeding. Do not run multiple
Vittics Builder processes against the same company workspace concurrently.

Existing companies gain the new gates when they next run a build. Older pending
release approvals lack verification metadata: reject them with instructions to
add `product.json` and delivery documentation, then rebuild. Previously signed-off
products are not retroactively certified by this upgrade. Mock mode exercises
orchestration using a scripted library; it does not validate AI coding quality.

## Install

Python 3.11+ and git.

```bash
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=sk-ant-...
```

## Adaptive company foundations

Project teams now adapt to ready work, with configurable role capabilities and
optional capped automatic staffing. Persistent memory has nine categories,
evidence references, and indexed retrieval across past projects. Optional model
routing uses reviewed success, quality, latency, and cost; bounded autonomous
work preserves human approvals. Role-scoped MCP tools and native browser journeys
let API agents interact with external tools and validate real applications.

The dashboard's **Company knowledge** page provides memory search, project teams,
and model results. Start with `vittics-builder work 1 --cycles 5 --seconds 300`,
`vittics-builder swarm 1`, `vittics-builder memory 'architecture decisions' --history`, and
`vittics-builder routing`. Routing, automatic staffing, MCP, and browser access are opt-in.
See [Phase 1 setup, configuration, and limitations](docs/PHASE1.md).

## The company

Ten departments, 21 roles, 22 agents out of the box.

| Department | Reports to | Roles |
| --- | --- | --- |
| Product | CEO | Product manager, technical writer |
| Design | CEO | UX designer |
| Marketing | CEO | Product marketer (launch copy) |
| Customer Support | CEO | Support specialist (FAQ, troubleshooting) |
| Legal and Compliance | CEO | Compliance officer (licence and privacy audit) |
| Engineering | CTO | Architect, backend (2), frontend, mobile |
| Data and AI | CTO | Database engineer, data engineer, ML engineer |
| Quality and Testing | CTO | Code reviewer, QA engineer, test automation engineer, performance engineer |
| Security | CTO | Security engineer (reviews every task), security auditor (audits the release) |
| Platform and Reliability | CTO | DevOps engineer, site reliability engineer |

The architect only gives tasks to the roles a product needs, so a command-line
tool will not involve the mobile engineer. Sales, finance and HR are not
agents: HR is the built-in performance system below, and the other two do not
produce software.

If you created a company with an earlier version, add the new departments with
`vittics-builder org sync`. It only adds what is missing.

## Start a company

```bash
mkdir mycompany && cd mycompany
vittics-builder init                      # writes org.yaml and the database
# edit org.yaml: company name, your two names, models, HR policy
vittics-builder org show
vittics-builder new "Invoice API" --brief "REST API to create, list and pay invoices. Python, SQLite, tests."
```

Work runs until a human decision is needed, then stops and tells you who must decide.

```bash
vittics-builder inbox --as ceo
vittics-builder approve 1 --as ceo
vittics-builder reject 2 --as cto --note "Use Postgres, and split the auth task in two."
vittics-builder status
vittics-builder log
```

The product is built in `workspaces/<id>-<name>/`, a normal git repository with
one commit per completed task and a `release` tag at sign-off. This marks a local verified release, not a deployment.

To try everything without an API key, set `VITTICS_PROVIDER=mock`. Scripted
agents run the full flow offline. `VITTICS_MOCK_BAD_AGENTS=Hari,Sandy` makes
those agents fail reviews so you can watch escalation and replacement.

## Two people, one dashboard

```bash
export VITTICS_CEO_TOKEN=... VITTICS_CTO_TOKEN=...
vittics-builder serve --host 0.0.0.0 --port 4700
```

Each of you signs in with your own token. The dashboard shows the decisions
waiting for you, every project, the ticket board, the org chart with scores, and the
activity log. Put it behind HTTPS (a reverse proxy or a tunnel) before exposing
it beyond your machine.

| Decision                              | Who                               |
| ------------------------------------- | --------------------------------- |
| Requirements, final sign-off          | CEO                               |
| Design and plan, escalations, release | CTO                               |
| Replace or reinstate an agent         | Whoever the department reports to |
| Workload hiring requests              | Whoever the department reports to |
| Hire, fire, rate                      | CEO anywhere; CTO in CTO departments |

## From idea to plan of action

Not every idea should be built. Submit it as an idea (the default on the
dashboard, or `vittics-builder idea new`) and the company decides first:

1. **Assessment.** The product manager (Ram) leads it. He asks Engineering
   (Sony) whether it is achievable and how big it is, Marketing (Anshu) who
   would use or pay for it and whether to keep it internal or sell it, and
   Legal (Srujana) about legal and data risks. Each answer is a ticket. Ram
   then writes `docs/ASSESSMENT.md`: problem, users, internal use or selling,
   feasibility, effort and cost, revenue options, risks and his recommendation.
   You can chat with any of them while it happens.
2. **Technical sign-off.** The CTO (Lucky) confirms the technical assessment,
   or sends it back with concerns for Ram to reassess.
3. **Decision.** The CEO (Niki) chooses: build it for internal use, build it to
   sell, park it, or drop it. A parked idea can be revisited later.
4. **Plan of action.** Ram writes `docs/PLAN.md`, one plan across departments,
   and assigns a ticket to each department that has work beyond the standard
   steps: for example help docs for Support, terms and privacy for Legal, and
   pricing and a launch plan for Marketing (only when selling). Work that needs
   the finished product waits for the build. The CEO approves the business side
   and the CTO the technical side; either can send it back.
5. **Build.** Requirements, design, build, checks and release then run as
   usual, with the department tickets alongside.

```bash
vittics-builder idea new "Leave Tracker" --brief "Staff request leave and managers approve it" --as ceo
vittics-builder approve 7 --as cto                      # technical sign-off
vittics-builder idea decide 8 commercial --note "Worth selling to small firms"
vittics-builder approve 9 --as ceo && vittics-builder approve 10 --as cto   # the plan, both sides
vittics-builder idea revisit 3                          # bring a parked idea back
```

Choose "Start building" (or `vittics-builder new`) to skip the assessment when the
decision is already made.

## What powers the agents: API or your subscription

Each agent works through one of two engines, set by its model:

- **The Anthropic API** (`claude-haiku-4-5`, `claude-sonnet-5-5`, `claude-opus-5-5`): pay per token,
  needs `ANTHROPIC_API_KEY`, and runs agents' commands in Vittics Builder's Docker sandbox.
- **A coding CLI on its own login** (`cli:claude-code`, or `cli:claude-code/haiku` to pick its model):
  runs on your Claude subscription instead of the API. Vittics Builder removes `ANTHROPIC_API_KEY` from
  the CLI's environment so it bills your subscription, and records usage at $0. Claude Code does
  its own reading, editing and commands in the project workspace; read-only roles get read-only
  tools, and reviewers may run tests but not edit. It runs on your machine under Claude Code's
  permission rules, not in the Docker sandbox, and your plan's usage limits apply.

```bash
claude                                           # once: sign in to Claude Code, then type /login
vittics-builder engines --test claude-code              # check it works
vittics-builder org set-model --all cli:claude-code     # move the whole team (or name one agent)
```

Seven CLI engines are built in. Tested: `claude-code` and `codex` (Codex CLI 0.160, on a ChatGPT
plan: a full task built, reviewed and checked through it). Set up from each CLI's documentation but
not yet run against Vittics Builder: `gemini`, `copilot`, `cursor`, `opencode` and `qwen`. Codex runs
read-only roles in its `read-only` sandbox and builders in `workspace-write`; the others' permissions
are coarser than Claude Code's: read-only roles get the CLI's read-only or ask-first mode, builders
its edit mode. `vittics-builder engines` shows which are installed. Add or override any engine under
`engines:` in `org.yaml`.

The ChatGPT desktop app includes the Codex CLI. To put it on your PATH:
`ln -s /Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex ~/.local/bin/codex`
(or `npm i -g @openai/codex`), then `codex login` if you are not signed in.

**Local models and other keys.** Any OpenAI-compatible endpoint works, per agent:
`ollama:<model>` and `lmstudio:<model>` (local, $0), `openai:<model>`, `openrouter:<model>` and
`groq:<model>` (with `OPENAI_API_KEY`, `OPENROUTER_API_KEY` or `GROQ_API_KEY`).
Routers you run on your machine work too: **OmniRoute** (`omniroute:auto`, port 20128; many free
providers, no keys needed) and **FreeLLMAPI** (`freellmapi:auto`, port 3001, your own free-tier
keys, `FREELLMAPI_KEY`). Both are MIT-licensed and untested here; they send your prompts and code
to whichever provider they pick, under that provider's terms, and free models are weaker, so they
suit reviews and reports better than building. Add others under
`endpoints:` in `org.yaml` (`base_url`, `key_env`, `free`). Local models must support tool calling.

```bash
vittics-builder engines --test ollama:qwen2.5-coder
vittics-builder org set-model Hari ollama:qwen2.5-coder
```

**When an engine hits its usage limit.** By default the project pauses until you resume it. Turn
on **failover** and the work moves to the next ready model in your list instead:

```yaml
failover:
  enabled: true
  models: ['cli:codex', 'claude-sonnet-5-5', 'ollama:qwen2.5-coder']   # tried in this order
  cooldown_minutes: 60
```

The limited engine rests until the reset time it reports ("resets 10:10pm"), or for
`cooldown_minutes`, and is used again after that. A CLI's limit covers its whole login, so
`cli:claude-code/haiku` rests too. API rate limits and a local model server that is not running
count as limits as well. The backup is told an earlier attempt stopped part-way, so it checks the
workspace first. Every switch is logged and shown under **Company knowledge**, and the project
only pauses when every option is resting. A backup on the API bills per token, within the
project's budget. Independent reviews never fall back to the builder's own model.

```bash
vittics-builder engines                                  # failover order and resting engines
vittics-builder engines --wake cli:claude-code           # try it again now (no value: wake all)
```

**Review depth** also sets cost: `pipeline.review_mode` is `standard` by default (a code reviewer
and QA check each ticket), `thorough` (every reviewer role, including security, per ticket) or
`light` (one reviewer). Release QA and the audits always run.

## Quick tasks and existing code

Not everything is a new product. A **task** is one change, straight to build: an engineer does
it, it is reviewed (by `review_mode`), your checks run, and the CTO reviews the changes. It can
work on an **existing repository** (a folder, a GitHub `owner/name`, or any git URL) or start
from a **GitHub issue** (needs the `gh` CLI). Vittics Builder clones the repository, works on an
`vittics/task-N` branch and leaves your files and history alone; agents never push. Take the
result as a patch (`Download patch`, then `git am`) or pull the branch from the task's workspace.

```bash
vittics-builder task "Fix the crash on empty names" --repo ~/code/app --check "pytest -q" --as cto
vittics-builder task --issue Ramch16/vittics-builder#12 --check "python -m pytest -q"
vittics-builder diff P3                                  # everything task 3 changed
```

## Watching and steering agents

Every agent run is recorded as it happens: what the agent was told, what it says, every tool call
and its result, and how it ended. Click a desk in the Office (or **Watch** on the Team page or a
ticket) to follow a run live, and type a message to **steer** it: the agent reads it at its next
step (agents on a CLI engine, which runs in one go, read it at the start of their next run).
**Code changes** on a ticket, the history entries on the review page, and **Changes** on a task
open colour-coded diffs.

```bash
vittics-builder watch Hari                               # Hari's latest run, step by step
vittics-builder steer Hari "Use python3, not python, on this Mac" --as cto
vittics-builder diff T-12
```

## Skills

Skills are guidelines every agent of the listed kinds (and departments) works by, on every
engine: the API, Claude Code and other CLIs, and local models. Built in and on by default:
**Karpathy coding guidelines** (from
[andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills), MIT): think
before coding, simplicity first, surgical changes, goal-driven execution, for engineering, data,
quality, security and platform roles. Switch built-in skills on or off under `skills:` in
`org.yaml`; add your own as Markdown files in the company's `skills/` folder.

```bash
vittics-builder skills                                   # what's on, for whom
vittics-builder skills add ~/notes/house-style.md         # or a URL, e.g. a CLAUDE.md on GitHub
```

## The office, memory and the app

- **Office** shows the company as a floor of departments and desks: who is working (and on
  which ticket), active in the last few minutes, or idle, with live telemetry: agents working
  now, model calls and tokens in the last hour, cost today and a 12-hour activity chart.
- **Shared memory.** Agents `remember` lasting facts and decisions ("the API uses JWT in
  cookies", "Niki wants British spelling"). The most relevant ones, from the project and
  company-wide, are given to every later agent. Each project page lists them.
- **Install it as an app.** In Chrome or Edge, use the install icon in the address bar (Safari:
  File → Add to Dock). Or run `vittics-builder app` to start the dashboard in its own window.

## Running the company day to day

**Costs and budgets.** Every model call is recorded with its estimated cost
(Anthropic list prices: Haiku 4.5 $1/$5, Sonnet 5.5 $2/$10, Opus 5.5 $4/$20 per
million input/output tokens; override with `llm.prices`). The dashboard's Costs
section shows the total, each project against its budget, each department and
the top agents. Each project gets `budgets.default_project_usd` (25 by default,
0 for no limit), or a budget you set when you start it. At 80% it is noted in
the activity log. When the budget is used up, work pauses before the next step
and the CEO is asked: approve to raise it (to the suggested amount, or a number
you write in the note), or send back to stop. A stopped project resumes when the
CEO sets a new budget. A step already running finishes, so spend can go slightly
over. Your Anthropic bill is the source of truth.

**Status reports.** Click **Status report** on a project, or run `vittics-builder report`,
and the product manager writes a short report: done, in progress, blocked or
needs you, cost, next steps. Reports are also written automatically when the
build starts, when a release is ready for review or blocked, at sign-off, and
when work pauses for budget (`reports.automatic`). The facts come from the
tickets, decisions, costs and activity log; if no product manager is staffed, the
plain facts are the report.

**Product review.** **Review product** (also on release and sign-off decisions)
shows everything needed to approve without a terminal: how to run it, every
acceptance check with its real output, the QA and audit verdicts, the documents
(assessment, plan, requirements, design, architecture, README, operations, audit
reports), every file, the history and the cost. Once signed off, **Download
release** gives you the verified source as a zip.

**After release.** Paste customer feedback under **Customer feedback** (or
`vittics-builder feedback`). The support specialist (Venky) triages it against the
product and its tickets: bugs become To do tickets and reopen the product for its
next version; feature requests go to the backlog for you to prioritise;
questions get a suggested reply. Each sign-off is a numbered version, tagged
`v1`, `v2` and so on in the product's git history.

```bash
vittics-builder costs
vittics-builder budget 1 60                             # CEO: raise project 1's budget to $60
vittics-builder report 1 --as cto
vittics-builder feedback 1 "Crashes when the name is empty" --source "support email" --as ceo
```

## How your agents think

Agents need an AI to work. Choose one for the whole team on **Machine → How your agents think**
(a banner says so until you do), or in a terminal:

```bash
vittics-builder ai                                # what is in use, and whether it is ready
vittics-builder ai use claude-code                # your Claude subscription (run `claude`, type /login)
vittics-builder ai use codex                      # your ChatGPT plan (`codex login`)
vittics-builder ai key && vittics-builder ai use api   # an Anthropic API key, asked for hidden
vittics-builder ai test                           # one tiny prompt, to prove it answers
```

Choosing moves every agent to it, and new hires use it too. An API key is stored privately in the
company folder and used only by this server, never by agents' commands or the product's checks.

## Idea to production

Describe what you want in a sentence, on Home (**What do you want to build?**) or in a terminal, and
choose how hands-on to be. The team assesses the idea, plans it, writes requirements, designs, builds
and reviews it, runs the release checks, and after your sign-off runs it on this computer.

```bash
vittics-builder build "A customer support app with login, a database and Stripe payments" --autopilot key
```

| Autopilot | You decide | Approved automatically |
|---|---|---|
| `off`, step by step | every stage | nothing |
| `key`, key decisions (default) | the idea, the plan, the final sign-off | technical sign-off, requirements, design, the release review |
| `final`, final sign-off only | the final sign-off | also the idea (when the assessment recommends building) and the plan |

- **Some things always wait for you:** the final sign-off, any failure (blocked release,
  escalated ticket, failed deployment), changed acceptance checks, budgets, hiring and installs. A
  release review is only created once audits, acceptance checks and executable verification pass.
  The sign-off lists everything autopilot decided, and why. Change the level on the project page or
  with `vittics-builder autopilot <project> <level>`.
- **This computer is checked before building.** If it lacks what the project needs (Node.js,
  PostgreSQL, Docker...), building waits at *Preparing this computer* with the install commands.
- **Keys for the product.** API keys and database URLs go in the project's keys (project page, or
  `vittics-builder keys <project> set STRIPE_SECRET_KEY`, which asks for the value hidden). They are
  stored privately on this computer, given to the product's checks, preview and deploy commands, and
  replaced with `[NAME]` in any output. Agents see the names, never the values.
- **The preview.** A web app or API says how to start in `product.json`
  (`"serve": {"command": "npm start", "health": "/healthz"}`, listening on `$PORT`). Without a
  `production:` entry, sign-off runs it here, from a clean copy of the release, and the project page
  links to it. A new version replaces the old one only once it answers; if it fails, the old one keeps
  running and the CTO decides. `vittics-builder preview <project> start|stop`.

## Machine readiness

`vittics-builder doctor` (and **Machine** in the dashboard) shows whether this computer is ready: what the
company and each project need, what is installed, which version, and whether each AI CLI and the
GitHub CLI are signed in.

- **Needs come from what is actually used:** Git and Python for Vittics Builder itself, the CLIs your
  agents work through (and failover backups), Docker when the sandbox uses it, and for each project
  its files (`package.json` means Node.js, `go.mod` Go, `Cargo.toml` Rust, ...) and the commands in
  its `product.json` and production config. Pick a project to see exactly why.
- **Nothing is installed without asking.** Only tools Vittics Builder knows can be installed: with
  Homebrew on macOS, winget on Windows, and npm for the AI CLIs, one at a time after you confirm.
  Anything needing admin rights or a licence (Docker Desktop, Homebrew itself, Apple's command line
  tools, apt on Linux) is shown as the command for you to run. Signing in is always yours.
  A hosted Vittics Builder never installs; `machine.installs: false` switches it off anywhere.
- **Failures point at the machine.** When release checks fail with "command not found", the CTO is
  told which tool is missing (including the `python` vs `python3` trap), and approving a design
  warns if the project needs something this computer lacks.

```bash
vittics-builder doctor                                   # company-wide
vittics-builder doctor --project 2                       # one project, with reasons
vittics-builder doctor --install node gemini             # asks before each one (--yes to skip asking)
vittics-builder doctor --missing                         # everything missing that can be installed
```

## The desktop app

Vittics Builder also comes as a desktop app (Tauri). It starts its own bundled backend, keeps the company
in `~/VitticsBuilder` (an existing `~/OrgForge` is kept), signs you in, and lets you switch between CEO and CTO, since on your own computer
you are both. The first screen walks you through checking the machine and submitting a first idea.
The backend listens only on 127.0.0.1 and stops when the app quits.

Build it (macOS today; needs Rust, Node.js and Vittics Builder's `.venv` with the `desktop` extra):

```bash
pip install -e '.[desktop]'                       # PyInstaller, in Vittics Builder's .venv
cd desktop && npm install
npm run build                                     # bundles the backend, then the app
# -> desktop/src-tauri/target/release/bundle/macos/Vittics Builder.app and bundle/dmg/Vittics Builder_<version>.dmg
```

The app is not signed yet: macOS asks for confirmation on first open (Control-click the app, then
Open). Signing and notarising need an Apple Developer ID; a Windows build needs a Windows machine.
`vittics-builder desktop` runs the same backend from a terminal.

The `desktop` workflow on GitHub builds both installers (Windows `.exe`, macOS `.dmg`) on pushes to
version branches, or on demand from the Actions tab; download them from the run's artifacts.

## Worker machines

Lend another computer's coding CLIs to the company: a spare laptop, a desktop with more memory, a
Windows machine. Agents assigned to it do their CLI work there, on that computer's own logins
(its Codex or Claude subscription); reviews, checks and decisions stay here.

```bash
# here (the company): make the dashboard reachable on your network, then pair
vittics-builder serve --host 0.0.0.0
vittics-builder machines pair windows-laptop             # prints a one-time code (10 minutes)

# on the other computer (same Vittics Builder version, its CLIs installed and signed in)
vittics-builder worker join http://my-mac.local:4700 ABCD-EFGH-JKMN-PQRS
vittics-builder worker run

# here again
vittics-builder machines assign Hari windows-laptop      # unassign Hari: back to this computer
vittics-builder machines                                 # online, engines, agents, jobs done
```

- **The worker connects; nothing connects to it.** It needs no open port.
- **Pairing never sends the code or the key in the clear.** The worker proves it knows the code,
  the company proves it does too, and the worker's key arrives encrypted under the code. Codes are
  16 characters, work once and expire after 10 minutes. Revoke a machine any time.
- **Every message is signed both ways,** with a timestamp and a single-use nonce. The worker only
  acts on jobs its own company signed; the company only accepts results from paired, unrevoked
  workers.
- **A job names an engine, never a command.** The worker runs its own definition of that engine,
  in a fresh folder that is deleted afterwards, and returns the changes as a patch applied here.
  Dependency folders (`node_modules`, `.venv`, ...) are not sent.
- **Use HTTPS, a private network or Tailscale.** Workers refuse other addresses unless told
  `--insecure`. Run the worker under a dedicated user account: agents run commands there.
- **Offline is not a failure.** If the worker is offline, the turn runs on this computer when the
  engine is installed here; otherwise it counts as unavailable, so failover or a pause takes over.

Only agents on a coding CLI (`cli:...`) can move; API agents need no machine. Machine in the
dashboard shows the same, and adds pairing, assignment and revoking.

## Production: deploy, health checks and rollback

Give a project a `production:` entry in `org.yaml` and the CEO's sign-off deploys it, instead of
stopping at "Ready for deployment". Projects without one work as before.

```yaml
production:
  projects:
    Greeter:
      env: [FLY_API_TOKEN]
      environments:
        - name: staging
          deploy: ./scripts/deploy.sh staging {version}
          health: curl -fsS https://staging.example.com/healthz
          rollback: ./scripts/deploy.sh staging {previous}
        - name: production
          approval: ceo
          deploy: ./scripts/deploy.sh production {version}
          health: curl -fsS https://example.com/healthz
          rollback: ./scripts/deploy.sh production {previous}
          monitor_minutes: 5
```

- **In order, environment by environment.** Each runs its deploy command in the product workspace
  at the signed-off release, then its health check (5 tries, 10 seconds apart, by default). An
  environment with `approval:` waits for that person's go-ahead; holding it back needs no reason.
- **Unhealthy means roll back.** The rollback command runs with the last healthy version as
  `{previous}` and is health-checked too. An urgent incident ticket is filed, and the CTO either
  retries the deployment or sends it back to the team with guidance. A first deployment has
  nothing to roll back to, so it just stops.
- **Live.** When every environment is healthy the project is *Live in production*. With
  `monitor_minutes` (and the workers service on), its health check keeps running, and an outage
  becomes an urgent incident ticket (one per outage, not one per check). New tickets, customer
  feedback and incidents reopen it for the next version, which deploys the same way.
- **You own these commands, not the agents.** They run with production credentials, so they live
  in `org.yaml`, outside every workspace. Only the variables listed under `env:` are passed through;
  other keys, tokens and passwords in your environment are withheld. Output is redacted before it
  is stored. Nothing deploys if the workspace changed after sign-off.

```bash
vittics-builder deployments 1                            # what is where, and its status
```

## Talking to the team

The CEO and CTO can talk to any agent directly. On the dashboard, click
**Message** next to anyone in People. In the terminal, use `vittics-builder chat`.
Each of you has a private conversation with each agent, and the agent
remembers it.

Agents answer in their own role. Pick the project the conversation is about
and they can read its files and tickets, so you can ask Ram about the
requirements, Sony about the design, or Badri what QA found. They cannot change
code from a chat. When you ask for a change, they file a ticket for the right
department, on your behalf, and tell you its id. The ticket goes through review
and the release checks like any other, and starts the team if the project is
building. Ticket ids in replies link to the ticket.

```bash
vittics-builder chat Sony "Why did you split the API into two services?" --project 1 --as cto
vittics-builder chat Hari "Please add input validation to the signup form" --project 1 --as cto
vittics-builder chat Sony --as cto                      # read the conversation
```

## Parallel work and hiring for workload

The team works up to `pipeline.max_parallel` tickets at the same time per
project (default 3). Each agent works one ticket at a time, in its own git
worktree on a `ticket/t-<id>` branch. When a ticket passes review it is merged
into `main`, and the project history shows a merge per ticket. If it conflicts
with work merged meanwhile, it goes back to its agent to redo on the latest
code, on its own so it cannot conflict again. A conflict does not count against
the agent. Set `max_parallel: 1` to work one ticket at a time.

No agent can hire. Vittics Builder watches the queue instead: when a role has
`staffing.hire_when_waiting` waiting tickets per agent (default 3), it asks the
CEO or CTO, whoever the department reports to, to hire one more, explaining
the queue and the extra model cost. Approve and an agent is hired with the next
free seat. Decline and it does not ask again for that role until the
queue grows. It never asks past `staffing.max_per_role` agents in a role
(default 4).

```yaml
pipeline:
  max_parallel: 3
staffing:
  hire_when_waiting: 3
  max_per_role: 4
```

New hires are named by their teammates. When an agent joins without a name
(a workload hire, a replacement, or `vittics-builder org hire` with no `--name`), the
longest-serving agent in that department picks one, in the spirit of the names
already there. It must be a single first name nobody at the company uses. If
the agent cannot find one, or the model is unavailable, a built-in name is used.
Names you give and the founding team in `org.yaml` are never changed.

## Tickets

The whole company works through an internal, Jira-style ticket tracker. Every
piece of work is a ticket (`T-12`) on the dashboard board: Backlog, To do,
In progress, In review, Needs CTO and Done, filterable by project and
department.

- **Each department's stage work is a ticket.** Requirements (Product), UX
  design (Design), architecture and plan (Engineering), release QA (Quality)
  and every audit (Security, Compliance, Platform). Each ticket records who
  handed it over to whom, when it went to the CEO or CTO, and their decision.
- **Build work is tickets.** That includes plan tasks, audit fixes and fixes
  after work is sent back. Each records the agent's progress, every handoff to
  a reviewer in another department, each reviewer's verdict, send-backs and
  escalations.
- **Agents log as they work.** Every agent on a project has ticket tools. They
  comment progress, decisions and blockers, file tickets for bugs or follow-up
  work they find, and transfer a ticket to the role or department that should
  own it. Agent-filed tickets are picked up by the team. Past 10 open, or 25 in
  total per project, they wait in the backlog for the CEO or CTO to triage.
  Agents can transfer a ticket at most 4 times, and their tickets never reopen
  a product that is releasing.
- **The CEO and CTO triage.** You can file tickets (task, bug or story; urgent
  to low), comment, change priority, transfer a ticket to another department,
  and move it between backlog, to do and cancelled. The pipeline owns the
  working statuses. The agent working a ticket reads its comments, and CEO or
  CTO instructions take precedence.

The team works to-do tickets highest priority first. Backlog tickets do not
block a release. Tickets filed during the release checks are built before the
release goes to the CTO. A to-do ticket you file on a product that is
releasing, or already ready for deployment, sends it back to build, withdraws
pending release approvals, and runs the release checks again.

```bash
vittics-builder ticket new 1 "Greeting crashes on empty name" --type bug --priority urgent --todo
vittics-builder ticket list --project 1
vittics-builder ticket comment T-5 "Add a regression test" --as cto
vittics-builder ticket update T-5 --role ux_designer        # transfer to Design
vittics-builder ticket show T-5                             # full history and handoffs
```

## Performance, firing and rehiring

Every evaluation is a score from 0 to 100: peer review and QA on each task
attempt, your approval (92) or rejection (35) of requirements and designs, and
manual ratings (`vittics-builder rate Sandy 40 --note "Ignored the design"`). An
agent's standing is a rolling average weighted towards recent work.

| Situation (after `min_tasks` evaluations)                          | Result                         |
| ------------------------------------------------------------------ | ------------------------------ |
| Score below `probation_below`                                      | Probation, with the reviewers' findings added to the agent's instructions |
| Recovers to `probation_below + recovery_margin`                    | Back in good standing          |
| Still low after `probation_tasks` more evaluations                 | Replacement proposed           |
| Score below `fire_below`                                           | Replacement proposed at once   |
| Replacement scores `rehire_margin` below the agent it replaced     | Reinstating the predecessor proposed |

A replacement takes the same seat, inherits the lessons from the predecessor's
worst reviews, and moves one step up `model_ladder` if `escalate_model` is on.
With `hr.auto_fire: false` (default) proposals go to the CEO or CTO; with
`true` they happen automatically. You can always act yourself:

```bash
vittics-builder org fire Sandy --reason "Keeps skipping tests" --as cto   # replaced by a successor
vittics-builder org fire Sandy --no-replace                               # seat left empty
vittics-builder org rehire Sandy --as cto                                 # current seat holder steps down
vittics-builder org show --all                                            # include former agents
```

## The learning loop

On by default (`learning:` in `org.yaml`); `learning.enabled: false` turns it off.

- **Reviewers are held to what happened next.** Every review verdict is settled later by the CTO's
  call on an escalated ticket (only each reviewer's latest round counts), by release checks failing
  after tickets were approved, or by the release being accepted. A ruling on a ticket counts like a
  human approval or rejection (92 or 35). A release-level result is shared, so it counts once per
  reviewer: 92 when accepted, 60 when checks fail. Accuracy feeds the reviewers' performance (so
  probation applies to them too) and the routing evidence for their models.
  `score_reviewers: false` keeps measuring without scoring.
- **Failures become proposals.** After `propose_after` (3) failed reviews of the same kind of work,
  across at least two tickets, an architect reads the findings and proposes an approach, such as
  "write the failing test first". The CTO approves or rejects it; nothing is used before that.
- **Approved approaches compete.** Each is tried `min_trials` (2) times next to the current approach
  for that kind of work, then the one with better reviewed results is kept.
- **Routing is on too,** but picks among models only where you list candidates under
  `routing.rules`; without a rule every agent keeps its own model.

```bash
vittics-builder learning                                 # reviewer accuracy and learned approaches
```

Company knowledge in the dashboard shows the same, next to model results.

## Changing the structure

Nothing about the org is fixed. Add or close departments, define roles, hire as many agents as you want.

```bash
vittics-builder org add-dept data --name "Data" --reports-to cto
vittics-builder org add-role data_engineer --dept data --kind builder \
  --tools read_file,write_file,replace_in_file,list_files,run_command \
  --prompt "You build data pipelines and their tests."
vittics-builder org hire --role data_engineer --as cto
vittics-builder org hire --role backend_engineer --model claude-opus-5-5
```

A role's `kind` tells the pipeline what it is for:

| Kind | When it works |
| --- | --- |
| `product` | Writes the requirements from the brief |
| `designer` | Writes `docs/DESIGN.md`; approved by the CEO together with the requirements |
| `planner` | Writes the architecture and the task plan |
| `builder` | Gets tasks from the plan |
| `reviewer` | Checks every task. Each staffed reviewer role checks each task |
| `qa` | Tests every task and runs the release check |
| `auditor` | Audits the finished product; blocking findings become fix tasks |

New roles are used from the next project on. An accessibility reviewer, for
example, is one `add-role --kind reviewer` and one `hire` away.

## What agents can do

| Tool              | Does                                                        |
| ----------------- | ----------------------------------------------------------- |
| `read_file`, `write_file`, `replace_in_file`, `list_files` | Work on files inside the project workspace |
| `run_command`     | Run shell commands in the workspace: install, build, test    |
| `delegate`        | Hand a sub-task to a colleague in another role               |
| `submit_plan`     | Architect: submit the task breakdown                         |
| `submit_review`   | Reviewer and QA: score and verdict                           |

Each role gets only the tools listed for it.

## Safety

- File tools cannot leave the project workspace.
- Commands run without your API keys or tokens in their environment, with a
  timeout, and a deny list blocks `sudo`, deleting outside the workspace,
  piping downloads into a shell, credential paths and `git push`.
- That deny list is a seatbelt, not a sandbox. For real work set
  `sandbox.mode: docker` in `org.yaml` so commands run in a container with no
  network by default.
- Publishing is yours: agents never push. Review the workspace, then push it yourself.

## Limits to know about

- It aims at any software product, but results depend on the model, the brief
  and your reviews. Expect to send work back. Small, clearly scoped first
  releases work best.
- Parallel tickets share one machine. With `sandbox.mode: local` they run commands side by side, so tests
  that bind fixed ports or write outside the workspace can clash; use `max_parallel: 1` or Docker for those.
  Reviewers, QA and auditors may check several tickets at once.
- Long tasks are bounded by `llm.max_turns` and the model's context window.
- Anthropic, the offline mock, OpenAI-compatible endpoints, and coding CLI engines
  are supported. Another provider implements `complete()` in `vittics_builder/llm.py`.
- API usage costs money, and the full company spends more of it: every task
  attempt is checked by three agents and every release by three auditors. To
  run leaner, let go of roles you do not need (`vittics-builder org fire Mani
  --no-replace`). Token counts are tracked per agent in the database.
- Reviewers and QA are scored only from what can be checked: the CTO's call on
  escalated tickets, release checks and accepted releases (see *The learning
  loop*). Auditors are not scored automatically; rate them yourself with
  `vittics-builder rate` when their checks miss things or block good work.
- Audits are done by a language model reading and running the code. They catch
  common problems but are not a substitute for a professional penetration test
  or legal review before a commercial launch.

## Layout

| Path                      | What                                               |
| ------------------------- | -------------------------------------------------- |
| `vittics_builder/default_org.yaml` | Default departments, roles, seats and policies   |
| `vittics_builder/org.py`         | Departments, roles, hire, fire, replace, rehire    |
| `vittics_builder/naming.py`      | Teammates name new hires                           |
| `vittics_builder/chat.py`        | CEO and CTO chat with agents                       |
| `vittics_builder/engines.py`     | Coding CLI engines (Claude Code and others)        |
| `vittics_builder/runs.py`        | Live run log and steering                          |
| `vittics_builder/skills.py`      | Skills (built in: `skills/karpathy-guidelines.md`) |
| `vittics_builder/sources.py`     | Existing repositories and GitHub issues            |
| `vittics_builder/memory.py`      | Shared long-term memory                            |
| `vittics_builder/telemetry.py`   | Office presence and live telemetry                 |
| `vittics_builder/costs.py`       | Cost ledger, prices and budgets                    |
| `vittics_builder/reports.py`     | Status reports                                     |
| `vittics_builder/review.py`      | Product review page                                |
| `vittics_builder/feedback.py`    | Customer feedback triage                           |
| `vittics_builder/performance.py` | Scores and HR policy                               |
| `vittics_builder/pipeline.py`    | Stages, task loop, human decisions                 |
| `vittics_builder/tickets.py`     | Ticket tracker and the agents' ticket tools        |
| `vittics_builder/agent.py`       | One agent run: prompt and tool loop                |
| `vittics_builder/tools.py`       | Workspace file and command tools                   |
| `vittics_builder/llm.py`         | Anthropic provider and offline mock                |
| `vittics_builder/server.py`, `static/index.html` | Dashboard API and page             |
| `vittics_builder/cli.py`         | Command line                                       |
| `tests/`                  | `pytest` runs the whole company offline            |

## Licence

MIT. Use it, change it and build on it, commercially too; keep the copyright notice. See `LICENSE`,
and `NOTICE.md` for third-party packages. Contributions are welcome: see `CONTRIBUTING.md`.
