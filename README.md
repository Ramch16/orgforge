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
one commit per completed task and a `release` tag at sign-off.

To try everything without an API key, set `ORGFORGE_PROVIDER=mock`. Scripted
agents run the full flow offline. `ORGFORGE_MOCK_BAD_AGENTS=Ife,Kenji` makes
those agents fail reviews so you can watch escalation and replacement.

## Two people, one dashboard

```bash
export ORGFORGE_CEO_TOKEN=... ORGFORGE_CTO_TOKEN=...
orgforge serve --host 0.0.0.0 --port 4700
```

Each of you signs in with your own token. The dashboard shows the decisions
waiting for you, every project and task, the org chart with scores, and the
activity log. Put it behind HTTPS (a reverse proxy or a tunnel) before exposing
it beyond your machine.

| Decision                              | Who                               |
| ------------------------------------- | --------------------------------- |
| Requirements, final sign-off          | CEO                               |
| Design and plan, escalations, release | CTO                               |
| Replace or reinstate an agent         | Whoever the department reports to |
| Hire, fire, rate                      | CEO anywhere; CTO in CTO departments |

## Performance, firing and rehiring

Every evaluation is a score from 0 to 100: peer review and QA on each task
attempt, your approval (92) or rejection (35) of requirements and designs, and
manual ratings (`orgforge rate Kenji 40 --note "Ignored the design"`). An
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
orgforge org fire Kenji --reason "Keeps skipping tests" --as cto   # replaced by a successor
orgforge org fire Kenji --no-replace                               # seat left empty
orgforge org rehire Kenji --as cto                                 # current seat holder steps down
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
- Tasks run one at a time. Parallel work is not implemented.
- Long tasks are bounded by `llm.max_turns` and the model's context window.
- Only the Anthropic provider and the offline mock are included. Another vendor
  is one class with a `complete()` method in `orgforge/llm.py`.
- API usage costs money, and the full company spends more of it: every task
  attempt is checked by three agents and every release by three auditors. To
  run leaner, let go of roles you do not need (`orgforge org fire Leila
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
| `orgforge/performance.py` | Scores and HR policy                               |
| `orgforge/pipeline.py`    | Stages, task loop, human decisions                 |
| `orgforge/agent.py`       | One agent run: prompt and tool loop                |
| `orgforge/tools.py`       | Workspace file and command tools                   |
| `orgforge/llm.py`         | Anthropic provider and offline mock                |
| `orgforge/server.py`, `static/index.html` | Dashboard API and page             |
| `orgforge/cli.py`         | Command line                                       |
| `tests/`                  | `pytest` runs the whole company offline            |

## Licence

Proprietary, all rights reserved. See `LICENSE` and `NOTICE.md`.
