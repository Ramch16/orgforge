import pytest

from vittics_builder.company import Company
from vittics_builder.llm import MockProvider


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """CLI commands build their own Company; keep them on the scripted mock, never the real API."""
    monkeypatch.setenv("VITTICS_PROVIDER", "mock")


@pytest.fixture
def make_company(tmp_path):
    def build(bad_agents=(), **hr):
        co = Company(tmp_path, provider=MockProvider(set(bad_agents)), create=True)
        for key, value in hr.items():
            setattr(co.s.hr, key, value)
        return co
    return build


@pytest.fixture
def co(make_company):
    return make_company()
