# OrgForge

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

## Verified product delivery (0.3)

OrgForge now requires executable acceptance evidence before release. It remains
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

The CTO approves `product.json` together with the design, and OrgForge pins that
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
orgforge export 1 --output ../product-release.zip
```

Export refuses unfinished or modified releases, existing output files, and
output paths within the product workspace. Cloud deployment still requires a
chosen hosting target, credentials, and product-specific configuration.

Other reliability changes: missing review verdicts and exhausted agent runs
fail closed; invalid or cyclic plans are rejected before task creation;
interrupted in-progress tasks can resume within a single running company process;
Git errors now stop work instead of silently succeeding. Do not run multiple
OrgForge processes against the same company workspace concurrently.

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
`orgforge org sync`. It only adds what is missing.

## Start a company

```bash
mkdir mycompany && cd mycompany
orgforge init                      # writes org.yaml and the database
# edit org.yaml: company name, your two names, models, HR policy
orgforge org show
orgforge new "Invoice API" --brief "REST API to create, list and pay invoices. Python, SQLite, tests."
```

Work runs until a human decision is needed, then stops and tells you who must decide.

```bash
orgforge inbox --as ceo
orgforge approve 1 --as ceo
orgforge reject 2 --as cto --note "Use Postgres, and split the auth task in two."
orgforge status
orgforge log
```

The product is built in `workspaces/<id>-<name>/`, a normal git repository with
one commit per completed task and a `release` tag at sign-off. This marks a local verified release, not a deployment.

To try everything without an API key, set `ORGFORGE_PROVIDER=mock`. Scripted
agents run the full flow offline. `ORGFORGE_MOCK_BAD_AGENTS=Hari,Sandy` makes
those agents fail reviews so you can watch escalation and replacement.

## Two people, one dashboard

```bash
export ORGFORGE_CEO_TOKEN=... ORGFORGE_CTO_TOKEN=...
orgforge serve --host 0.0.0.0 --port 4700
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
dashboard, or `orgforge idea new`) and the company decides first:

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
orgforge idea new "Leave Tracker" --brief "Staff request leave and managers approve it" --as ceo
orgforge approve 7 --as cto                      # technical sign-off
orgforge idea decide 8 commercial --note "Worth selling to small firms"
orgforge approve 9 --as ceo && orgforge approve 10 --as cto   # the plan, both sides
orgforge idea revisit 3                          # bring a parked idea back
```

Choose "Start building" (or `orgforge new`) to skip the assessment when the
decision is already made.

## Talking to the team

The CEO and CTO can talk to any agent directly. On the dashboard, click
**Message** next to anyone in People. In the terminal, use `orgforge chat`.
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
orgforge chat Sony "Why did you split the API into two services?" --project 1 --as cto
orgforge chat Hari "Please add input validation to the signup form" --project 1 --as cto
orgforge chat Sony --as cto                      # read the conversation
```

## Parallel work and hiring for workload

The team works up to `pipeline.max_parallel` tickets at the same time per
project (default 3). Each agent works one ticket at a time, in its own git
worktree on a `ticket/t-<id>` branch. When a ticket passes review it is merged
into `main`, and the project history shows a merge per ticket. If it conflicts
with work merged meanwhile, it goes back to its agent to redo on the latest
code, on its own so it cannot conflict again. A conflict does not count against
the agent. Set `max_parallel: 1` to work one ticket at a time.

No agent can hire. OrgForge watches the queue instead: when a role has
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
(a workload hire, a replacement, or `orgforge org hire` with no `--name`), the
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
orgforge ticket new 1 "Greeting crashes on empty name" --type bug --priority urgent --todo
orgforge ticket list --project 1
orgforge ticket comment T-5 "Add a regression test" --as cto
orgforge ticket update T-5 --role ux_designer        # transfer to Design
orgforge ticket show T-5                             # full history and handoffs
```

## Performance, firing and rehiring

Every evaluation is a score from 0 to 100: peer review and QA on each task
attempt, your approval (92) or rejection (35) of requirements and designs, and
manual ratings (`orgforge rate Sandy 40 --note "Ignored the design"`). An
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
orgforge org fire Sandy --reason "Keeps skipping tests" --as cto   # replaced by a successor
orgforge org fire Sandy --no-replace                               # seat left empty
orgforge org rehire Sandy --as cto                                 # current seat holder steps down
orgforge org show --all                                            # include former agents
```

## Changing the structure

Nothing about the org is fixed. Add or close departments, define roles, hire as many agents as you want.

```bash
orgforge org add-dept data --name "Data" --reports-to cto
orgforge org add-role data_engineer --dept data --kind builder \
  --tools read_file,write_file,replace_in_file,list_files,run_command \
  --prompt "You build data pipelines and their tests."
orgforge org hire --role data_engineer --as cto
orgforge org hire --role backend_engineer --model claude-opus-5-5
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
- Only the Anthropic provider and the offline mock are included. Another vendor
  is one class with a `complete()` method in `orgforge/llm.py`.
- API usage costs money, and the full company spends more of it: every task
  attempt is checked by three agents and every release by three auditors. To
  run leaner, let go of roles you do not need (`orgforge org fire Mani
  --no-replace`). Token counts are tracked per agent in the database.
- Reviewers, QA and auditors are not scored automatically (nobody reviews the
  reviewers). Rate them yourself with `orgforge rate` when their checks miss
  things or block good work.
- Audits are done by a language model reading and running the code. They catch
  common problems but are not a substitute for a professional penetration test
  or legal review before a commercial launch.

## Layout

| Path                      | What                                               |
| ------------------------- | -------------------------------------------------- |
| `orgforge/default_org.yaml` | Default departments, roles, seats and policies   |
| `orgforge/org.py`         | Departments, roles, hire, fire, replace, rehire    |
| `orgforge/naming.py`      | Teammates name new hires                           |
| `orgforge/chat.py`        | CEO and CTO chat with agents                       |
| `orgforge/performance.py` | Scores and HR policy                               |
| `orgforge/pipeline.py`    | Stages, task loop, human decisions                 |
| `orgforge/tickets.py`     | Ticket tracker and the agents' ticket tools        |
| `orgforge/agent.py`       | One agent run: prompt and tool loop                |
| `orgforge/tools.py`       | Workspace file and command tools                   |
| `orgforge/llm.py`         | Anthropic provider and offline mock                |
| `orgforge/server.py`, `static/index.html` | Dashboard API and page             |
| `orgforge/cli.py`         | Command line                                       |
| `tests/`                  | `pytest` runs the whole company offline            |

## Licence

Proprietary, all rights reserved. See `LICENSE` and `NOTICE.md`.
