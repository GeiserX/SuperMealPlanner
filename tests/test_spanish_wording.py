import re
from pathlib import Path

import yaml

import supermealplanner

LOCALES = Path(supermealplanner.__file__).parent / "locales"


def test_spanish_text_has_no_english_batch_word():
    es = yaml.safe_load((LOCALES / "es.yaml").read_text(encoding="utf-8"))
    offenders = [
        key for key, text in es.items() if "batch" in re.sub(r"\{\w+\}", "", str(text)).lower()
    ]
    assert offenders == []


def test_spanish_cooking_tab_and_reminder():
    es = yaml.safe_load((LOCALES / "es.yaml").read_text(encoding="utf-8"))
    assert es["tab_batch"] == "Cocinar"
    assert "cocinar por adelantado" in es["reminder_batch"]
