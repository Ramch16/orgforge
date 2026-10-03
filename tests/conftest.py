import pytest

from orgforge.company import Company
from orgforge.llm import MockProvider


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
