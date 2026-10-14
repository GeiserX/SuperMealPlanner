"""The shared assistant principles: shipped with the app, unchanged, and read first, once."""

import hashlib

import pytest

from supermealplanner import context

# The shared assistant principles, byte for byte. A change to them lands here on purpose, never by
# accident: copy the new text into supermealplanner/principles/ and update the hash.
SHA256 = {
    "en": "713784a662236621bf63709f07ac811199b2bf779e95b986a475e2087ab1713a",
    "es": "dbd6efd497a264a8dd43e6f5b0ceeb78d3b97c5d61536fceb03505c970937bd1",
}
HEADING = {"en": "## Principles", "es": "## Principios"}


@pytest.mark.parametrize("lang", sorted(SHA256))
def test_the_shipped_principles_are_the_shared_text(lang):
    raw = (context.PRINCIPLES / f"{lang}.md").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SHA256[lang]


@pytest.mark.parametrize("lang", sorted(SHA256))
def test_the_rules_start_with_the_twelve_principles_exactly_once(data, lang):
    data.settings.locale = lang
    rules = context.rules(data)
    text = (context.PRINCIPLES / f"{lang}.md").read_text(encoding="utf-8").rstrip()
    assert text.count("**") == 24  # twelve bold titles
    assert rules.startswith(text + "\n")
    assert rules.count(HEADING[lang]) == 1 and rules.count(text) == 1
    assert rules.index("You are the household's meal-planning assistant") > len(text)


def test_a_language_without_a_copy_reads_the_english_principles(data):
    data.settings.locale = "de"
    assert context.rules(data).startswith(HEADING["en"])
    data.settings.locale = "es-ES"
    assert context.rules(data).startswith(HEADING["es"])


def test_the_apps_rules_no_longer_repeat_what_the_principles_say(data):
    own = context.rules(data).split(HEADING["en"], 1)[1].split("You are the household's", 1)[1]
    own = " ".join(own.split()).lower()  # a reflowed or recased sentence is the same sentence
    for gone in (
        'Never answer "ask your doctor"',
        "Act only on what is addressed to you",
        "is not a request",
        "never ask yes or no for it",
        "never claim it is done",
        "A label that declines",
        "A yes-or-no offer gets",
        "Never «Nada» on a button",
        "Be proactive with reminders",
        "without an answer nothing is done",
    ):
        assert gone.lower() not in own, gone
