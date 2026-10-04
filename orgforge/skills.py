"""Skills: guidelines the company gives its agents, on every engine.

A skill is a Markdown file with a short header:

    ---
    name: karpathy-guidelines
    title: Karpathy coding guidelines
    kinds: [builder, reviewer]        # which kinds of role get it (leave out for everyone)
    departments: [engineering]        # and/or which departments (leave out for all)
    ---
    The guideline text...

Built-in skills ship with OrgForge and are switched on in org.yaml (`skills:`). Files in the
company's `skills/` folder are always used, unless their header says `enabled: false`.
"""
from __future__ import annotations

import re
import urllib.request
from pathlib import Path

import yaml

BUILTIN = Path(__file__).with_name("skills")
HEADER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


class SkillError(ValueError):
    pass


def parse(text: str, fallback_name: str) -> dict:
    m = HEADER.match(text)
    meta, body = (yaml.safe_load(m.group(1)) or {}, m.group(2)) if m else ({}, text)
    if not isinstance(meta, dict):
        meta = {}
    name = str(meta.get("name") or fallback_name)
    return {"name": name, "title": str(meta.get("title") or name.replace("-", " ").capitalize()),
            "kinds": [str(k) for k in (meta.get("kinds") or [])], "source": str(meta.get("source") or ""),
            "departments": [str(d) for d in (meta.get("departments") or [])],
            "enabled": meta.get("enabled", True) is not False, "body": body.strip()}


class Skills:
    def __init__(self, settings) -> None:
        self.s = settings
        self.folder = settings.root / "skills"

    def all(self) -> list[dict]:
        out = []
        for f in sorted(BUILTIN.glob("*.md")):
            skill = parse(f.read_text(), f.stem)
            out.append({**skill, "builtin": True, "enabled": skill["name"] in self.s.skills})
        if self.folder.is_dir():
            for f in sorted(self.folder.glob("*.md")):
                out.append({**parse(f.read_text(), f.stem), "builtin": False, "file": str(f)})
        return out

    def for_kind(self, kind: str, department: str | None = None) -> str:
        """The skills an agent of this kind of role (in this department) works by, as prompt text."""
        chosen = [s for s in self.all() if s["enabled"] and s["body"] and (not s["kinds"] or kind in s["kinds"])
                  and (not s["departments"] or department is None or department in s["departments"])]
        if not chosen:
            return ""
        return "Company skills (follow these; the CEO's and CTO's instructions come first):\n\n" + "\n\n".join(
            f"## {s['title']}\n{s['body']}" for s in chosen)

    def add(self, ref: str) -> dict:
        """Install a skill from a Markdown file or URL into the company's skills folder."""
        if re.match(r"^https?://", ref):
            if "github.com" in ref and "/blob/" in ref:
                ref = ref.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
            with urllib.request.urlopen(ref, timeout=30) as r:
                text = r.read(200_000).decode("utf-8", errors="replace")
            stem = Path(ref.split("?")[0]).stem
        else:
            path = Path(ref).expanduser()
            if not path.is_file():
                raise SkillError(f"No file at {path}.")
            text, stem = path.read_text(), path.stem
        if stem.upper() == "CLAUDE":
            stem = "imported-guidelines"
        skill = parse(text, re.sub(r"[^a-z0-9-]+", "-", stem.lower()).strip("-") or "skill")
        if len(skill["body"]) < 20:
            raise SkillError("That file has no guideline text.")
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / f"{skill['name']}.md"
        target.write_text(text if HEADER.match(text) else f"---\nname: {skill['name']}\nsource: {ref}\n---\n{text}")
        return {**skill, "file": str(target)}
