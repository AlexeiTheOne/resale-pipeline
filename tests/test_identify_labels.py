"""Identification reads sewn-in labels for style codes when there is no hang
tag (Person B, 2026-11-02). The model call itself needs Gemini, so these
tests pin the prompt and the local code extraction; the live result on
test_images/label_ed_hardy_slipper.jpg is in docs/check-command-design.md."""
from pathlib import Path

import config
import identify


def test_research_prompt_asks_for_sewn_in_labels():
    p = identify.RESEARCH_PROMPT
    assert "SEWN-IN LABELS" in p and "no hang tag" in p
    assert "Factory or lot lines" in p and "also from a sewn-in label" in p
    assert "sewn-in label" in identify.FORMAT_INSTRUCTIONS


def test_style_code_on_a_label_line_is_extracted():
    # the Ed Hardy slipper's label, as printed
    text = "POLYESTER LINING\nEH06521655/EH SLIPPER CUP W\nUSA 7/8"
    assert identify._extract_style_codes(text) == ["EH06521655"]


def test_the_label_photo_is_a_fixture():
    assert (Path(config.BASE_DIR) / "test_images" / "label_ed_hardy_slipper.jpg").stat().st_size > 50_000
