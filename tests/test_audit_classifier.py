"""Regression tests for the audit_data_corruption classifier improvements."""
import pytest

from scripts.audit_data_corruption import classify_verbal_answer_key


class FakeOption:
    def __init__(self, label, text, is_correct=False):
        self.option_label = label
        self.option_text = text
        self.is_correct = is_correct


class FakeQuestion:
    def __init__(self, prompt, explanation):
        self.prompt = prompt
        self.explanation = explanation


def test_correct_answer_regex_does_not_match_word_because():
    """``correct answer because`` used to wrongly grab 'B' as the stated answer."""
    q = FakeQuestion(
        prompt="In the last paragraph the author mentions all of the following EXCEPT",
        explanation=(
            "The four challenges from the passage are A, E, C, B. "
            "Therefore, D is the correct answer because it is the only "
            "option NOT mentioned as a challenge."
        ),
    )
    options = [
        FakeOption("A", "x"), FakeOption("B", "y"), FakeOption("C", "z"),
        FakeOption("D", "w", is_correct=True), FakeOption("E", "v"),
    ]
    cat, _ = classify_verbal_answer_key(q, options)
    assert cat != "Answer-key likely WRONG"


def test_multi_blank_tc_skips_explicit_letter_strategy():
    """Multi-blank TC items use blank1_X / blank2_Y labels, never single
    letters — Strategy 1's A-F regex must not run on them."""
    q = FakeQuestion(
        prompt="Franklin (i) ___ science. His contributions were (ii) ___.",
        explanation="Outweigh (blank1_C) and substantial (blank2_A) fit. "
                    "We follow standard form A through F when discussing.",
    )
    options = [
        FakeOption("blank1_A", "impede"),
        FakeOption("blank1_B", "replicate"),
        FakeOption("blank1_C", "outweigh", is_correct=True),
        FakeOption("blank2_A", "substantial", is_correct=True),
        FakeOption("blank2_B", "paltry"),
        FakeOption("blank2_C", "abhorrent"),
    ]
    cat, _ = classify_verbal_answer_key(q, options)
    assert cat != "Answer-key likely WRONG"


def test_quoted_clue_words_from_prompt_are_not_external():
    """If a quoted word in the explanation appears in the PROMPT, it's a
    clue word, not from another question."""
    q = FakeQuestion(
        prompt="The thriving health food company sells __________ meat "
               "products so meat-like that vegetarians sometimes call to "
               "make sure that the product is really animal-free.",
        explanation='It is clear from "vegetarians" and "animal-free" '
                    'that the meat products are fake, or ersatz.',
    )
    options = [
        FakeOption("A", "mendacious"),
        FakeOption("B", "nugatory"),
        FakeOption("C", "ersatz", is_correct=True),
        FakeOption("D", "mimetic"),
        FakeOption("E", "clandestine"),
    ]
    cat, _ = classify_verbal_answer_key(q, options)
    assert cat != "Explanation-from-other"


def test_quoted_word_with_trailing_punctuation_matches_prompt():
    """``"discovery."`` in the explanation should match ``discovery`` in
    the prompt (the period was just punctuation)."""
    q = FakeQuestion(
        prompt="They make the (ii) discovery that other people exist",
        explanation='Solipsism (blank1_A). The "discovery." in question '
                    'is that the world is not about them. The trap "selfish" '
                    'does not describe the "discovery."',
    )
    options = [
        FakeOption("blank1_A", "solipsistic", is_correct=True),
        FakeOption("blank1_B", "sophomoric"),
        FakeOption("blank1_C", "quixotic"),
        FakeOption("blank2_A", "arresting", is_correct=True),
        FakeOption("blank2_B", "selfish"),
        FakeOption("blank2_C", "undue"),
    ]
    cat, _ = classify_verbal_answer_key(q, options)
    assert cat != "Explanation-from-other"


def test_genuine_wrong_answer_still_flagged():
    """A genuinely wrong answer key should still trip the regex."""
    q = FakeQuestion(
        prompt="Pick the right one.",
        explanation="The correct answer is B because foo and bar.",
    )
    options = [
        FakeOption("A", "x", is_correct=True),  # marked A but explanation says B
        FakeOption("B", "y"), FakeOption("C", "z"),
    ]
    cat, _ = classify_verbal_answer_key(q, options)
    assert cat == "Answer-key likely WRONG"
