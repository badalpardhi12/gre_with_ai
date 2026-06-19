"""
Deterministic scoring engine for GRE questions.
Handles answer checking for all question types and scaled score estimation.
"""
import math
import re
from fractions import Fraction

from services.log import get_logger

logger = get_logger("scoring")


# ── Theta estimation facade (P3.S1) ───────────────────────────────────

def compute_theta(user_id: str = "local", window: int = 40) -> float:
    """Running user theta estimate — caller-friendly facade around
    ``services.rating_service.get_user_theta``.

    Returns a finite float. Graceful fallback: when rating_service is
    unavailable (fresh clone, import error) or its estimate returns a
    non-finite value, we return ``0.0`` so section-CAT routing treats
    the user as theta=0 rather than crashing selection.

    Phase 3 S1 wire-up: ``models.exam_session`` calls this after S1 to
    drive S2 section-level adaptive item selection.
    """
    try:
        from services import rating_service  # local import — keeps cycles clean
        theta = float(rating_service.get_user_theta(user_id=user_id, window=window))
    except Exception:
        logger.debug("compute_theta: rating_service unavailable, defaulting to 0.0",
                     exc_info=True)
        return 0.0
    if not math.isfinite(theta):
        return 0.0
    return theta


# ── Scaled Score Lookup (approximation) ───────────────────────────────
# Maps (measure, difficulty_band, raw_correct) -> (estimated_low, high)
# on the 130-170 scale. This is an APPROXIMATION — ETS does not publish
# its raw→scaled equating tables or its section-routing cutoffs.
#
# Real-GRE section-level forms set BOTH a ceiling AND a floor: only a HARD
# second-section form can reach 170, and being routed to a hard form also
# guarantees a minimum score (the "safety net" floor — you can't be routed
# to hard without already proving enough in section 1). An EASY form caps
# well below 170 no matter how many you then answer correctly. The legacy
# table modeled only the cap, letting a hard-routed taker sink toward 130
# and under-rewarding strong medium performers.
#
# The per-form [floor, ceiling] bands below are reverse-engineered from
# Magoosh's GRE score-calculator and Manhattan Prep's floor/ceiling
# description. They are TUNABLE — keep them here so future calibration data
# can shift them without touching the curve logic.
SCALED_SCORE_BANDS = {
    "quant":  {"easy": (130, 151), "medium": (136, 158), "hard": (146, 170)},
    "verbal": {"easy": (130, 155), "medium": (141, 164), "hard": (149, 170)},
}

# Within-band growth exponent applied to the normalized raw fraction. An
# exponent > 1 makes a given raw map to a LOWER point inside its band, so
# Quant is curved slightly harder than Verbal (matching the real GRE, where
# the same number-correct yields a marginally lower Quant scaled score).
SCALED_CURVE_GAMMA = {"verbal": 1.0, "quant": 1.2}

# Max raw across S1 (12) + S2 (15) for one measure.
RAW_MAX = 27

# Default measure used when a caller omits it (keeps the legacy
# single-argument ``estimate_scaled_score`` calls self-consistent).
_DEFAULT_MEASURE = "verbal"


def _build_score_table():
    """Build ``tables[measure][band][raw] -> (low, high)`` clamped into each
    form's [floor, ceiling] band.

    For raw 0..RAW_MAX we place a center score on a monotonic curve inside
    the band, then emit a ±1 range clamped to the band so the FLOOR and the
    CEILING are both honored (a hard form never reports below its floor; an
    easy form never reports above its cap).
    """
    tables = {}
    for measure, bands in SCALED_SCORE_BANDS.items():
        gamma = SCALED_CURVE_GAMMA.get(measure, 1.0)
        tables[measure] = {}
        for band, (floor, ceiling) in bands.items():
            span = ceiling - floor
            table = {}
            for raw in range(RAW_MAX + 1):
                pct = raw / float(RAW_MAX)
                base = floor + span * (pct ** gamma)
                center = int(round(base))
                low = max(floor, min(ceiling, center - 1))
                high = max(floor, min(ceiling, center + 1))
                table[raw] = (low, high)
            tables[measure][band] = table
    return tables


SCORE_TABLES = _build_score_table()


def normalize_tc_options(options):
    """Group TC options by blank. Returns a list of (blank_name, choice,
    option_dict) tuples preserving input order within each blank.

    Three label conventions exist in the bank:

    1. Explicit prefix (``"blank1_A"``, ``"blank2_C"``): split on ``_``.
       Authoritative; used verbatim.
    2. Flat labels (A–F or A–I) for **multi-blank** TC — 6 or 9 one-letter
       labels with no underscore. These are two- or three-blank items
       where the authoring convention groups consecutive letters into
       blanks of 3 (A/B/C → blank1, D/E/F → blank2, G/H/I → blank3).
       The per-blank choice letter is renamed to A/B/C so each blank
       shows "A) … B) … C) …" in the UI instead of random mid-alphabet
       starts.
    3. Flat labels otherwise (5-option A–E, etc.): single-blank TC.
       All options go under ``blank1`` with their original letter.

    Both the UI (``screens/question_screen.py``) and the scoring check
    (``_check_text_completion``) call this so they stay in lock-step.
    Prior bug (GitHub #15, Q5257 + 15 other multi-blank flat-label items):
    the UI fell through to case 3 even for 6-option items, cramming all
    six radios under "Blank 1:" with no way to answer blank 2.
    """
    has_prefix = any("_" in (o.get("label") or "") for o in options)
    n = len(options)
    result = []

    if has_prefix:
        for o in options:
            parts = (o.get("label") or "").split("_", 1)
            if len(parts) == 2:
                result.append((parts[0], parts[1], o))
            else:
                result.append(("blank1", o.get("label", ""), o))
        return result

    is_flat_multi = (
        n in (6, 9)
        and all(
            isinstance(o.get("label"), str)
            and len(o["label"]) == 1
            and o["label"].isalpha()
            for o in options
        )
    )
    if is_flat_multi:
        sorted_opts = sorted(options, key=lambda o: o["label"])
        per_blank = 3
        choice_letters = ("A", "B", "C")
        for i, o in enumerate(sorted_opts):
            blank = f"blank{i // per_blank + 1}"
            result.append((blank, choice_letters[i % per_blank], o))
        return result

    for o in options:
        result.append(("blank1", o.get("label", ""), o))
    return result


class ScoringEngine:
    """Deterministic scoring for all GRE question types."""

    # ── Answer Checking ───────────────────────────────────────────────

    @staticmethod
    def check_answer(question_data, user_response):
        """
        Check if a user's response is correct.

        Args:
            question_data: dict from QuestionBankService.get_question()
            user_response: dict e.g. {"selected": ["A"]} or {"value": "2.5"}

        Returns:
            bool — True if correct
        """
        if not isinstance(question_data, dict) or "subtype" not in question_data:
            return False
        if not isinstance(user_response, dict):
            return False

        subtype = question_data["subtype"]

        if subtype == "numeric_entry":
            return ScoringEngine._check_numeric(
                question_data["numeric_answer"], user_response
            )
        elif subtype in ("qc", "mcq_single", "rc_single", "data_interp"):
            return ScoringEngine._check_single_select(
                question_data["options"], user_response
            )
        elif subtype in ("mcq_multi", "rc_multi"):
            return ScoringEngine._check_multi_select(
                question_data["options"], user_response
            )
        elif subtype == "se":
            return ScoringEngine._check_sentence_equivalence(
                question_data["options"], user_response
            )
        elif subtype == "tc":
            return ScoringEngine._check_text_completion(
                question_data["options"], user_response
            )
        elif subtype == "rc_select_passage":
            return ScoringEngine._check_select_in_passage(
                question_data["options"], user_response
            )
        else:
            return False

    @staticmethod
    def _check_single_select(options, response):
        """Single correct option. User selects one."""
        selected = response.get("selected", [])
        if len(selected) != 1:
            return False
        correct = [o["label"] for o in options if o["is_correct"]]
        return selected[0] in correct

    @staticmethod
    def _check_multi_select(options, response):
        """All-or-nothing: user must select exactly the correct set."""
        selected = set(response.get("selected", []))
        correct = set(o["label"] for o in options if o["is_correct"])
        return selected == correct

    @staticmethod
    def _check_sentence_equivalence(options, response):
        """SE: exactly 2 correct answers, no partial credit."""
        selected = set(response.get("selected", []))
        correct = set(o["label"] for o in options if o["is_correct"])
        if len(correct) != 2:
            logger.warning(
                "SE question has %d correct option(s); expected exactly 2", len(correct)
            )
            return False
        return selected == correct

    @staticmethod
    def _check_text_completion(options, response):
        """TC: all blanks must be correct. Options are grouped per blank by
        ``normalize_tc_options`` so the same blank→choice mapping is used
        here and in the UI (``screens/question_screen.py``).

        Earlier versions only handled the ``blank1_A`` prefix form, which
        silently marked every single-blank TC question wrong (~93 items).
        A later fix added fallback for flat 5-option labels. This version
        additionally handles flat multi-blank labels (6 / 9 options)
        where the authoring convention groups consecutive letters into
        blanks of 3 — previously the scorer folded all correct options
        under ``blank1``, overwriting later blanks (GitHub #15, Q5257).
        """
        selected = response.get("selected", {})
        if not isinstance(selected, dict):
            return False
        correct = {}
        for blank, choice, opt in normalize_tc_options(options):
            if opt.get("is_correct"):
                correct[blank] = choice
        if not correct:
            # No is_correct option marked at all. True data-corruption case;
            # distinct from a label-format mismatch (handled above).
            logger.warning("TC question has no is_correct options; treating as wrong")
            return False
        return all(selected.get(blank) == ans for blank, ans in correct.items())

    @staticmethod
    def _check_select_in_passage(options, response):
        """Select-in-passage: user selects a sentence index."""
        selected = response.get("selected_sentence")
        correct = [o for o in options if o["is_correct"]]
        if not correct or selected is None:
            return False
        return str(selected) == str(correct[0]["label"])

    @staticmethod
    def _check_numeric(numeric_answer, response):
        """
        Numeric entry: accept equivalent decimal/fraction forms.
        - 2.5 == 2.50 == 5/2
        """
        if numeric_answer is None:
            return False

        user_value = response.get("value")
        user_num = response.get("numerator")
        user_den = response.get("denominator")

        # Determine user's numeric value
        if user_num is not None and user_den is not None:
            try:
                user_frac = Fraction(int(user_num), int(user_den))
            except (ValueError, ZeroDivisionError, TypeError):
                return False
        elif user_value is not None:
            try:
                user_frac = Fraction(str(user_value))
            except (ValueError, ZeroDivisionError, TypeError):
                return False
            # Reject NaN / Inf even though Fraction() rejects them, in case the
            # caller passed a pre-parsed float.
            if not math.isfinite(float(user_frac)):
                return False
        else:
            return False

        # Determine correct value, defending against malformed DB rows.
        try:
            if numeric_answer.get("exact_value") is not None:
                correct_frac = Fraction(str(numeric_answer["exact_value"]))
            elif (numeric_answer.get("numerator") is not None and
                  numeric_answer.get("denominator") is not None):
                correct_frac = Fraction(
                    int(numeric_answer["numerator"]),
                    int(numeric_answer["denominator"]),
                )
            else:
                logger.warning("Numeric answer has neither exact_value nor numerator/denominator")
                return False
        except (ValueError, ZeroDivisionError, TypeError) as e:
            logger.warning("Malformed numeric answer %r: %s", numeric_answer, e)
            return False

        # Tolerance can legitimately be missing/None on legacy rows.
        tolerance = numeric_answer.get("tolerance") or 0
        try:
            tolerance = float(tolerance)
        except (TypeError, ValueError):
            tolerance = 0
        if tolerance > 0:
            # Compare in Fraction space to avoid float-precision artifacts
            # (e.g. Fraction('1.05') as a float is 1.0500000000000000444...,
            # so float subtraction can mis-classify a value that is exactly
            # at the tolerance boundary).
            try:
                tol_frac = Fraction(str(tolerance))
            except (ValueError, ZeroDivisionError):
                tol_frac = Fraction(0)
            return abs(user_frac - correct_frac) <= tol_frac
        return user_frac == correct_frac

    # ── Scaled Score Estimation ───────────────────────────────────────

    @staticmethod
    def estimate_scaled_score(raw_correct, difficulty_band="medium",
                              measure=_DEFAULT_MEASURE):
        """
        Estimate a GRE scaled score range from raw correct count.

        ``measure`` selects the per-measure band table (Verbal is curved
        slightly easier than Quant). An unknown band falls back to
        ``medium``; an unknown measure falls back to the default measure.

        Returns:
            (low, high) tuple of estimated scaled scores (130-170).
        """
        try:
            raw = max(0, min(RAW_MAX, int(raw_correct)))
        except (TypeError, ValueError):
            return (130, 135)
        measure_tables = SCORE_TABLES.get(measure, SCORE_TABLES[_DEFAULT_MEASURE])
        table = measure_tables.get(difficulty_band, measure_tables["medium"])
        return table.get(raw, (130, 135))

    @staticmethod
    def compute_session_scores(verbal_raw, verbal_band,
                                quant_raw, quant_band):
        """
        Compute full session scores.

        Returns dict with raw and estimated scores.
        """
        v_low, v_high = ScoringEngine.estimate_scaled_score(
            verbal_raw, verbal_band, measure="verbal")
        q_low, q_high = ScoringEngine.estimate_scaled_score(
            quant_raw, quant_band, measure="quant")
        return {
            "verbal_raw": verbal_raw,
            "quant_raw": quant_raw,
            "verbal_estimated_low": v_low,
            "verbal_estimated_high": v_high,
            "quant_estimated_low": q_low,
            "quant_estimated_high": q_high,
        }
