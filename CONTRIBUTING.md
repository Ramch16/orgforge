# Contributing to Vittics Builder

Thanks for helping. Vittics Builder is young, and real-world reports are the most valuable contribution.

## Report a problem
Open an issue with the bug template. Include the activity log or ticket history around the
problem (remove any secrets) and your settings (`llm.provider`, any CLI engine, `sandbox.mode`).

## Develop
```bash
git clone https://github.com/Ramch16/vittics-builder.git && cd vittics-builder
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q
```
The tests run the whole company offline with scripted mock agents: no API key, no cost.
To try the dashboard the same way:
```bash
mkdir -p /tmp/of && cd /tmp/of && VITTICS_PROVIDER=mock vittics-builder init && VITTICS_PROVIDER=mock vittics-builder serve
```

## Guidelines
- Keep changes focused, and add or update a test for any behaviour you change.
- Match the surrounding code: small modules, plain functions, comments that explain why.
- Safety first: agents must never get the API key, push code, or act outside their workspace.
  Changes near `tools.py`, `engines.py` or the sandbox need a test that shows the boundary holds.
- Update the README and the comments in `default_org.yaml` when settings or behaviour change,
  and add a line to `CHANGELOG.md`.

## Releases
Versions follow `0.MINOR.PATCH` until 1.0. Each release bumps `pyproject.toml` and
`vittics_builder/__init__.py`, adds a `CHANGELOG.md` entry, and is tagged `vX.Y.Z`.
