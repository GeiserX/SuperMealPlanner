"""The Home Assistant integration's voice-loop rules, loaded alone: no Home Assistant needed."""

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "smp_ha_speech",
    Path(__file__).parent.parent / "homeassistant/custom_components/supermealplanner/speech.py",
)
speech = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(speech)


def test_every_answer_ends_in_a_question_so_the_mic_opens_again():
    assert speech.with_more("Tonight it is hake.", "es") == "Tonight it is hake. ¿Algo más?"
    assert speech.with_more("Tonight it is hake.", "en-GB") == "Tonight it is hake. Anything else?"
    assert speech.with_more("Shall I swap it? Yes or no?", "en") == "Shall I swap it? Yes or no?"
    assert speech.with_more("  ", "es") == "¿Algo más?"
    assert speech.with_more("Bonjour.", "fr") == "Bonjour. Anything else?"


def test_a_short_no_after_our_question_ends_the_conversation():
    assert speech.asked_more("Tonight it is hake. ¿Algo más?", "es")
    assert speech.is_closing("¡Nada más, gracias!", "es")
    assert speech.is_closing("No.", "es")
    assert speech.is_closing("That's all", "en")


def test_a_no_to_the_chefs_own_question_still_goes_to_the_chef():
    assert not speech.asked_more("¿Hago este cambio? ¿Sí o no?", "es")
    assert not speech.asked_more(None, "es")
    assert not speech.is_closing("No, cámbialo por pescado", "es")
