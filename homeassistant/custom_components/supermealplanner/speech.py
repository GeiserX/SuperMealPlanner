"""What the voice loop says around the chef's answer.

Home Assistant keeps the microphone open after a reply that ends in a question, so every answer
ends in one: the chef's own ("Yes or no?"), or "Anything else?" added here. A short "no" or
"that's all" after that added question ends the conversation without asking the chef. A "no" after
the chef's own question still goes to the chef, which is waiting for it. No Home Assistant import
here, so this file can be tested alone.
"""

from __future__ import annotations

import re
import unicodedata

MORE = {"es": "¿Algo más?", "en": "Anything else?"}
BYE = {"es": "¡Hasta luego!", "en": "Bye!"}
SLOW = {
    "es": "El chef sigue pensando. Te dejo la respuesta en Telegram.",
    "en": "The chef is still thinking. The answer will be in Telegram.",
}
DOWN = {
    "es": "Ahora no puedo hablar con el chef.",
    "en": "I can't reach the chef right now.",
}
CLOSING = {
    "es": {
        "no", "nada", "no gracias", "nada gracias", "nada mas", "nada mas gracias", "no nada",
        "eso es todo", "eso es todo gracias", "es todo", "ya esta", "ya", "listo", "vale gracias",
        "gracias", "muchas gracias", "adios", "hasta luego", "chao", "fin", "terminar", "para",
    },
    "en": {
        "no", "nope", "nothing", "nothing else", "no thanks", "no thank you", "thats all",
        "that is all", "thats it", "thanks", "thank you", "bye", "goodbye", "done", "stop",
    },
}  # fmt: skip
QUESTION_END = ("?", "？", ";")  # what Home Assistant reads as a question


def lang(language: str | None) -> str:
    """The two-letter language this file has words for: Spanish or English."""
    code = (language or "es").split("-")[0].split("_")[0].lower()
    return code if code in MORE else "en"


def normal(text: str) -> str:
    """Lower case, no accents, no punctuation, single spaces: "¡Nada más, gracias!" is "nada mas gracias"."""
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    plain = re.sub(r"['’]", "", plain)  # "that's" is "thats"
    return " ".join(re.sub(r"[^\w\s]", " ", plain).split())


def with_more(answer: str, language: str | None) -> str:
    """The answer, ending in a question so the microphone opens again."""
    answer = answer.strip()
    if answer.endswith(QUESTION_END):
        return answer
    more = MORE[lang(language)]
    return f"{answer} {more}" if answer else more


def asked_more(previous: str | None, language: str | None) -> bool:
    """Whether the last thing said was our own "Anything else?", not the chef's question."""
    return bool(previous) and previous.rstrip().endswith(MORE[lang(language)])


def is_closing(text: str, language: str | None) -> bool:
    """Whether what was said only ends the conversation."""
    return normal(text) in CLOSING[lang(language)]
