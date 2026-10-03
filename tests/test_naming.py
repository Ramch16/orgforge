from orgforge.company import Company
from orgforge.llm import LLMResponse, MockProvider
from orgforge.org import NAME_POOL


class Replies(MockProvider):
    """Answers naming requests with fixed replies, in order, and records what was asked."""

    def __init__(self, *replies, fail=False):
        super().__init__()
        self.replies, self.fail, self.asked = list(replies), fail, []

    def complete(self, **kwargs):
        if (kwargs.get("meta") or {}).get("purpose") != "name_hire":
            return super().complete(**kwargs)
        self.asked.append(kwargs)
        if self.fail:
            raise RuntimeError("model unavailable")
        text = self.replies.pop(0) if self.replies else ""
        return LLMResponse(content=[{"type": "text", "text": text}], text=text)


def naming_events(co):
    return [e["message"] for e in co.db.all("SELECT message FROM events WHERE message LIKE '%named the new%'")]


def test_teammate_in_the_department_names_a_new_hire(co):
    hired = co.org.hire("backend_engineer", by=co.s.cto_name)
    assert hired["name"] == "Kiran"
    [event] = naming_events(co)
    assert event == "Sony (Engineering) named the new Backend Engineer Kiran."   # longest-serving in Engineering


def test_ceo_department_hire_is_named_by_that_department(co):
    hired = co.org.hire("product_manager", by=co.s.ceo_name)
    assert naming_events(co) == [f"Ram (Product) named the new Product Manager {hired['name']}."]


def test_replacement_successor_is_named_by_a_teammate(co):
    successor = co.org.replace("Hari", "poor results", by=co.s.cto_name)
    assert successor["name"] not in NAME_POOL and successor["seat"] == "be-1"
    assert naming_events(co)


def test_names_must_be_new_single_first_names(tmp_path):
    provider = Replies("Hari", "Lucky")                     # a teammate's name, then the CTO's
    co = Company(tmp_path, provider=provider, create=True)
    assert co.org.hire("backend_engineer", by="CTO")["name"] == NAME_POOL[0]   # fell back to the pool
    assert len(provider.asked) == 2 and not naming_events(co)

    provider.replies = ["Dr. Ravi Kumar", "  \"anvitha\".  "]   # not one name, then fine once tidied
    assert co.org.hire("backend_engineer", by="CTO")["name"] == "Anvitha"


def test_unavailable_model_falls_back_to_the_pool(tmp_path):
    co = Company(tmp_path, provider=Replies(fail=True), create=True)
    assert co.org.hire("qa_engineer", by="CTO")["name"] == NAME_POOL[0]
    assert co.db.one("SELECT 1 FROM events WHERE kind='warn' AND message LIKE '%could not name%'")


def test_given_names_and_the_founding_team_are_not_renamed(tmp_path):
    provider = Replies("Kiran")
    co = Company(tmp_path, provider=provider, create=True)
    assert not provider.asked                               # seeding from org.yaml asks nobody
    assert co.org.hire("backend_engineer", name="Meera", by="CTO")["name"] == "Meera"
    assert not provider.asked


def test_approved_workload_hire_is_named_by_the_team(co):
    p = co.pipeline.create_project("Product", "Build a library")
    for i in range(6):
        co.tickets.create(p["id"], f"Ticket {i}", "", co.s.cto_name, status="todo", role="backend_engineer")
    co.pipeline._check_staffing(p["id"])
    [ask] = [a for a in co.pipeline.inbox("cto") if a["kind"] == "hire"]
    co.pipeline.decide(ask["id"], "cto", "approved")
    names = {a["name"] for a in co.org.staff(role="backend_engineer")}
    assert names == {"Hari", "Sandy", "Kiran"}
