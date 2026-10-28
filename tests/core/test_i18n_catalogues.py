"""AC-10: the English and Chinese catalogues must stay in lockstep.

A chat string that exists in only one language silently degrades to the raw key for every
user of the other language, which is how `binding.unavailable` shipped as literal text.
This test turns that class of mistake into a build failure.
"""

import pytest
from lockbot.core.i18n import t
from lockbot.core.i18n.en import MESSAGES as EN
from lockbot.core.i18n.zh import MESSAGES as ZH


def test_catalogues_have_the_same_keys():
    missing_in_en = sorted(set(ZH) - set(EN))
    missing_in_zh = sorted(set(EN) - set(ZH))
    assert not missing_in_en, f"keys missing from en.py: {missing_in_en}"
    assert not missing_in_zh, f"keys missing from zh.py: {missing_in_zh}"


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_no_catalogue_entry_is_empty(lang):
    catalogue = EN if lang == "en" else ZH
    empty = sorted(k for k, v in catalogue.items() if not isinstance(v, str) or not v.strip())
    assert not empty, f"empty entries in {lang}.py: {empty}"


@pytest.mark.parametrize("key", ["binding.unavailable", "binding.failed"])
@pytest.mark.parametrize("lang", ["en", "zh"])
def test_binding_strings_resolve_to_real_text(key, lang):
    """A translated string must never resolve to its own key."""
    rendered = t(key, lang=lang)
    assert rendered != key
    assert rendered.strip()
