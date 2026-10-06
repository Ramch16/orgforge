import json

import pytest

from vittics_builder.cli import main
from vittics_builder.costs import price
from vittics_builder.skills import SkillError


def systems_seen(co):
    seen = []
    original = co.runtime.provider.complete
    def spy(**kw):
        seen.append(kw["system"])
        return original(**kw)
    co.runtime.provider.complete = spy
    return seen


def test_karpathy_guidelines_reach_coding_roles_only(co):
    [k] = [s for s in co.skills.all() if s["name"] == "karpathy-guidelines"]
    assert k["enabled"] and k["builtin"] and "forrestchang/andrej-karpathy-skills" in k["source"]
    assert "Think Before Coding" in k["body"] and "Surgical Changes" in k["body"]
    seen = systems_seen(co)
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.runtime.run(co.org.agent("Hari"), "Build it", co.pipeline.workspace(p), project_id=p["id"])
    co.runtime.run(co.org.agent("Anshu"), "Plan the launch", co.pipeline.workspace(p), project_id=p["id"])
    assert "Karpathy coding guidelines" in seen[0]            # backend engineer: builder
    assert "Karpathy coding guidelines" not in seen[-1]       # the marketer is a 'builder', but not in a tech department
    co.chat.send("Hari", "cto", "hi", wait=True)
    assert "Karpathy coding guidelines" not in seen[-1]       # not in chats


def test_company_skills_and_switching_off(co, tmp_path):
    co.s.skills = []
    src = tmp_path / "house-style.md"
    src.write_text("---\nname: house-style\ntitle: House style\nkinds: [product]\n---\nWrite in British English. Keep it short.")
    added = co.skills.add(str(src))
    assert added["name"] == "house-style" and (co.s.root / "skills" / "house-style.md").exists()
    assert co.skills.for_kind("product").startswith("Company skills") and "British English" in co.skills.for_kind("product")
    assert co.skills.for_kind("builder") == ""                 # karpathy off, house style is for product only
    (co.s.root / "skills" / "off.md").write_text("---\nenabled: false\n---\nNever applies to anyone at all.")
    assert "Never applies" not in co.skills.for_kind("product")
    plain = tmp_path / "CLAUDE.md"
    plain.write_text("Always run the tests before you say you are done.")
    assert co.skills.add(str(plain))["name"] == "imported-guidelines"
    with pytest.raises(SkillError):
        co.skills.add(str(tmp_path / "missing.md"))


def test_skills_cli(co, capsys):
    assert main(["--home", str(co.s.root), "skills"]) == 0
    out = capsys.readouterr().out
    assert "on   karpathy-guidelines" in out and "built in" in out


def test_router_endpoints_are_available(co):
    e = co.s.endpoints
    assert e["omniroute"]["base_url"] == "http://localhost:20128/v1" and e["freellmapi"]["key_env"] == "FREELLMAPI_KEY"
    assert co.org.set_model("Pavan", "omniroute:auto")["model"] == "omniroute:auto"
    assert price(co.s, "freellmapi:auto") == (0.0, 0.0)
    co.s.prices = {"omniroute:openai/gpt-5.4": {"input": 1.25, "output": 10}}
    assert price(co.s, "omniroute:openai/gpt-5.4") == (1.25, 10.0)      # paid routes can be priced
