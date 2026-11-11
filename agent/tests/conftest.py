import pytest

from vf_agent import i18n


@pytest.fixture(autouse=True)
def english():
    """Tests read the agent's words in English, whatever language the machine running them has."""
    i18n.set_language("en")
    yield
    i18n.set_language("en")
