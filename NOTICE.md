# Provenance

Vittics Builder was written from scratch in Python. It contains no source code,
prompts, assets, names or configuration from any other agent-office or
multi-agent project, and shares no file structure with one. The general idea
of AI agents organised into company roles is not owned by anyone; this
implementation is original.

## Third-party dependencies

Installed from PyPI at install time, not bundled. All are permissively
licensed and allow commercial use. Check each licence before you ship.

| Package   | Licence      |
| --------- | ------------ |
| PyYAML    | MIT          |
| anthropic | MIT          |
| FastAPI   | MIT          |
| Uvicorn   | BSD-3-Clause |
| mcp (optional) | MIT |
| playwright (optional) | Apache-2.0 |

Use of the Claude API is governed by Anthropic's commercial terms for your
API account. Products your agents build may pull in their own dependencies;
review those licences per project.

## Bundled content

`vittics_builder/skills/karpathy-guidelines.md` is the guidelines file from
[andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills), MIT-licensed
according to its README, included unchanged apart from a header naming its source.
