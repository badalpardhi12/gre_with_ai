"""Validation gates V1-V14 for the Kaplan extractor.

Each gate inspects one parsed-and-post-processed item dict and returns
a list of `(severity, kind, detail)` issues. An item with any
"block"-severity issue is held in `status='draft'` and dumped to
`data/extracted/kaplan/rejects/`.

Items are passed in as plain dicts (the dataclass-asdict form of
`scripts.extract_kaplan.RawItem`) so this module has zero direct
dependencies on the extractor's internals.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List, Tuple

# (severity, kind, detail)
Issue = Tuple[str, str, str]


# Canonical option counts per subtype (per plan section 5, gate V1).
# Values are sets of acceptable option counts; multiple entries cover
# the natural variation (TC 1-blank: 5 / 2-blank: 6 / 3-blank: 9).
OPTION_COUNT_RULES: Dict[str, set] = {
    "tc": {5, 6, 9},
    "se": {6},
    "rc_single": {5},
    "rc_multi": {3},
    "qc": {4},
    "mcq_single": {4, 5},
    "mcq_multi": {3, 4, 5, 6, 7, 8},
    "mcq_short_answer": set(),    # publisher prints answer text, no choices
    "data_interp": {5},
    "rc_select_passage": set(),  # passage-select; no fixed count
    "numeric_entry": set(),       # no options expected
}

OPTION_BEARING_SUBTYPES = {
    "tc", "se", "rc_single", "rc_multi", "qc",
    "mcq_single", "mcq_multi", "data_interp",
}

SINGLE_CORRECT_SUBTYPES = {"mcq_single", "rc_single", "qc"}


# ── Helpers ─────────────────────────────────────────────────────────


def _strip_html(text: str) -> str:
    """Cheap HTML strip suitable for length / latex checks."""
    return re.sub(r"<[^>]+>", " ", text or "")


def _normalise_option_text(text: str) -> str:
    return re.sub(r"\s+", " ", _strip_html(text or "").lower()).strip()


def _has_orphan_glyph(html: str) -> bool:
    return bool(re.search(
        r'<img[^>]*class="[^"]*\binline\b[^"]*"[^>]*src="images/'
        r'(?!([a-f]\.jpg))', html or "", re.I,
    ))


def _has_money_artefact(text: str) -> bool:
    if not text:
        return False
    # `$$50$` (with no closing `$$`) is the bad form. The canonical
    # `$$ ... $$` display-math pair is fine.
    return bool(re.search(r"\$\$\d[\d,.\s{}\\]*\$(?!\$)", text))


def _balanced(text: str, opener: str, closer: str) -> bool:
    return text.count(opener) == text.count(closer)


# ── Individual gates ────────────────────────────────────────────────

def _v1_option_count_correct(item: Dict[str, Any]) -> List[Issue]:
    sub = item.get("subtype")
    if sub not in OPTION_BEARING_SUBTYPES:
        return []
    options = item.get("options") or []
    rules = OPTION_COUNT_RULES.get(sub, set())
    if not rules:
        return []
    if len(options) not in rules:
        # TC ambiguity: accept 5 (1-blank) / 6 (2-blank) / 9 (3-blank).
        return [("block", "option_count_correct",
                 f"{sub} got {len(options)} options, "
                 f"expected one of {sorted(rules)}")]
    return []


def _v2_distractor_uniqueness(item: Dict[str, Any]) -> List[Issue]:
    options = item.get("options") or []
    if not options:
        return []
    norms = [_normalise_option_text(o.get("text", "")) for o in options]
    counts = Counter([n for n in norms if n])
    dupes = [n for n, c in counts.items() if c > 1]
    if dupes:
        return [("block", "distractor_uniqueness",
                 f"duplicate option text: {dupes!r}")]
    return []


def _v3_at_least_one_correct(item: Dict[str, Any]) -> List[Issue]:
    sub = item.get("subtype")
    if sub not in OPTION_BEARING_SUBTYPES:
        return []
    options = item.get("options") or []
    if not options:
        return [("block", "at_least_one_correct_marked",
                 "no options on an option-bearing subtype")]
    correct = sum(1 for o in options if o.get("is_correct"))
    if correct == 0:
        return [("block", "at_least_one_correct_marked",
                 f"{sub} has 0 options marked correct")]
    return []


def _v4_single_correct_for_single(item: Dict[str, Any]) -> List[Issue]:
    sub = item.get("subtype")
    if sub not in SINGLE_CORRECT_SUBTYPES:
        return []
    options = item.get("options") or []
    correct = sum(1 for o in options if o.get("is_correct"))
    if correct != 1:
        return [("block", "single_correct_for_single_subtypes",
                 f"{sub} has {correct} correct options (expected 1)")]
    return []


def _v5_answer_key_cross_ref(item: Dict[str, Any]) -> List[Issue]:
    """Compare the answer-key label to the explanation header label."""
    ak = (item.get("correct_label") or "").strip().upper()
    expl = (item.get("explanation_label") or "").strip().upper()
    if not ak or not expl:
        return []
    # Tokenise and compare (order-insensitive).
    ak_set = {p.strip() for p in ak.split(",") if p.strip()}
    expl_set = {p.strip() for p in expl.split(",") if p.strip()}
    if ak_set and expl_set and ak_set != expl_set:
        return [("warn", "answer_key_cross_ref",
                 f"answer key {ak!r} != explanation header {expl!r}")]
    return []


def _v6_latex_well_formed(item: Dict[str, Any]) -> List[Issue]:
    issues: List[Issue] = []
    prompt = item.get("prompt") or ""
    expl = item.get("explanation") or ""
    for kind, text, severity in (
        ("prompt", prompt, "block"),
        ("explanation", expl, "warn"),
    ):
        if not text:
            continue
        for opener, closer in (("\\(", "\\)"), ("\\[", "\\]")):
            if not _balanced(text, opener, closer):
                issues.append((severity, "latex_well_formed",
                               f"{kind}: unbalanced {opener}/{closer}"))
                break
        if not _balanced(text, "{", "}"):
            issues.append((severity, "latex_well_formed",
                           f"{kind}: unbalanced curly braces"))
        for bad in ("\\f", "\\b"):
            if bad in text and bad + " " not in text:
                # Allow LaTeX commands like \frac (which start with \f)
                # by checking the next char isn't a letter.
                m = re.search(re.escape(bad) + r"(?![a-zA-Z])", text)
                if m:
                    issues.append((severity, "latex_well_formed",
                                   f"{kind}: stray {bad!r}"))
        if "\\\\n" in text:
            issues.append((severity, "latex_well_formed",
                           f"{kind}: literal '\\\\n' escape"))
    return issues


def _v7_rc_cluster_coherence(item: Dict[str, Any]) -> List[Issue]:
    """Items belonging to an explicit RC cluster header should have
    `rc_group_key` set. Solo-stimulus RC items are flagged per V7's
    intent (kept as a warn here; the persistence layer in Stage E will
    collapse cluster-keyed items into one Stimulus row)."""
    sub = item.get("subtype") or ""
    if not sub.startswith("rc_"):
        return []
    if not item.get("rc_group_key"):
        return [("warn", "rc_cluster_coherence",
                 "RC item has no cluster key (potential singleton)")]
    return []


def _v8_numeric_answer_parseable(item: Dict[str, Any]) -> List[Issue]:
    if item.get("subtype") != "numeric_entry":
        return []
    val = item.get("numeric_value") or item.get("correct_label") or ""
    val = val.strip() if isinstance(val, str) else val
    if not val:
        return [("block", "numeric_answer_parseable",
                 "numeric_entry has no answer value")]
    if isinstance(val, str) and val.startswith("@@GLYPH"):
        return [("warn", "numeric_answer_parseable",
                 "numeric_entry answer is an unresolved glyph image")]
    # Re-import the parser lazily to avoid a circular dep.
    from scripts.extract_kaplan import parse_numeric_value
    if parse_numeric_value(val) is None:
        return [("block", "numeric_answer_parseable",
                 f"numeric_entry value not parseable: {val!r}")]
    return []


def _v9_explanation_present(item: Dict[str, Any]) -> List[Issue]:
    expl = _strip_html(item.get("explanation") or "").strip()
    if len(expl) < 30:
        return [("warn", "explanation_present",
                 f"explanation too short: {len(expl)} chars")]
    return []


def _v10_figure_attached_when_referenced(item: Dict[str, Any]) -> List[Issue]:
    if item.get("measure") != "quant":
        return []
    if not item.get("has_figure"):
        return []
    # Phase 0 doesn't yet wire stimulus_id (that happens at persist
    # time). We only check that the parser captured a figure_image
    # filename when has_figure is true.
    if not item.get("figure_image"):
        return [("block", "figure_attached_when_referenced",
                 "has_figure=True but figure_image is empty")]
    return []


def _v11_qc_quantity_labels(item: Dict[str, Any]) -> List[Issue]:
    if item.get("subtype") != "qc":
        return []
    p = item.get("prompt") or ""
    has_a = bool(re.search(r"Quantity\s*A\s*:", p, re.I))
    has_b = bool(re.search(r"Quantity\s*B\s*:", p, re.I))
    if not (has_a and has_b):
        return [("block", "qc_quantity_labels_present",
                 "QC prompt missing 'Quantity A:' / 'Quantity B:' lines")]
    return []


def _v12_money_clean(item: Dict[str, Any]) -> List[Issue]:
    issues: List[Issue] = []
    for kind in ("prompt", "explanation"):
        text = item.get(kind) or ""
        if _has_money_artefact(text):
            issues.append(("block", "money_clean",
                           f"{kind} contains a $$N$ money artefact"))
    return issues


def _v13_no_orphan_glyph(item: Dict[str, Any]) -> List[Issue]:
    issues: List[Issue] = []
    for kind in ("prompt", "explanation"):
        if _has_orphan_glyph(item.get(kind) or ""):
            issues.append(("warn", "no_orphan_glyph",
                           f"{kind} contains an untranscribed inline glyph"))
    for opt in (item.get("options") or []):
        if _has_orphan_glyph(opt.get("text") or ""):
            issues.append(("warn", "no_orphan_glyph",
                           f"option {opt.get('label')} contains an "
                           f"untranscribed inline glyph"))
            break
    return issues


def _v14_prompt_nonempty(item: Dict[str, Any]) -> List[Issue]:
    p = _strip_html(item.get("prompt") or "").strip()
    if len(p) < 10:
        return [("block", "prompt_nonempty",
                 f"prompt too short ({len(p)} chars): {p!r}")]
    return []


GATES = [
    _v1_option_count_correct,
    _v2_distractor_uniqueness,
    _v3_at_least_one_correct,
    _v4_single_correct_for_single,
    _v5_answer_key_cross_ref,
    _v6_latex_well_formed,
    _v7_rc_cluster_coherence,
    _v8_numeric_answer_parseable,
    _v9_explanation_present,
    _v10_figure_attached_when_referenced,
    _v11_qc_quantity_labels,
    _v12_money_clean,
    _v13_no_orphan_glyph,
    _v14_prompt_nonempty,
]


def validate(item: Dict[str, Any]) -> List[Dict[str, str]]:
    """Run all 14 gates against `item` and return a flat list of issue
    dicts: `{severity, kind, detail}`."""
    issues: List[Dict[str, str]] = []
    for gate in GATES:
        try:
            for sev, kind, detail in gate(item):
                issues.append({
                    "severity": sev, "kind": kind, "detail": detail,
                })
        except Exception as exc:  # noqa: BLE001
            issues.append({
                "severity": "warn", "kind": gate.__name__,
                "detail": f"gate raised {type(exc).__name__}: {exc}",
            })
    return issues


def summarise(items: List[Dict[str, Any]], validate_fn=validate
              ) -> Tuple[List[List[Dict[str, str]]],
                         Dict[str, int], Dict[str, int]]:
    """Run validate over `items`; return per-item issue lists, plus
    aggregated counts by gate kind and by severity."""
    per_item: List[List[Dict[str, str]]] = []
    gate_counts: Counter = Counter()
    severity_counts: Counter = Counter()
    for item in items:
        issues = validate_fn(item)
        per_item.append(issues)
        for i in issues:
            gate_counts[i["kind"]] += 1
            severity_counts[i["severity"]] += 1
    return per_item, dict(gate_counts), dict(severity_counts)
