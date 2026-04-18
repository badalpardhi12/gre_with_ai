"""
Deterministic scoring engine for GRE questions.
Handles answer checking for all question types and scaled score estimation.
"""
import math
import re
from fractions import Fraction

from services.log import get_logger

logger = get_logger("scoring")


# ── Scaled Score Lookup (approximation) ───────────────────────────────
# Maps (raw_correct, difficulty_band) -> (estimated_low, estimated_high)
# Based on publicly available ETS percentile data and score ranges 130-170.
# This is an approximation — the real ETS formula is not public.

def _build_score_table():
    """Build lookup tables for Verbal and Quant scaled score estimation."""
    # Raw scores for S1 (12 questions) + S2 (15 questions) = 27 max
    # This maps total_correct -> (low_estimate, high_estimate) for each difficulty band

    tables = {}
    for band in ("easy", "medium", "hard"):
        table = {}
        for raw in range(28):  # 0 to 27
            pct = raw / 27.0
            if band == "easy":
                # Easy S2: scores capped lower
                base = 130 + pct * 25  # max ~155
            elif band == "hard":
                # Hard S2: scores reach higher
                base = 140 + pct * 30  # max 170
            else:
                # Medium S2: balanced
                base = 135 + pct * 30  # max ~165

            low = max(130, min(170, int(math.floor(base)) - 2))
            high = max(130, min(170, int(math.ceil(base)) + 2))
            table[raw] = (low, high)
        tables[band] = table
    return tables


SCORE_TABLES = _build_score_table()


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
        """TC: all blanks must be correct. options grouped per blank."""
        selected = response.get("selected", {})
        if not isinstance(selected, dict):
            return False
        correct = {}
        for o in options:
            # TC options have label like "blank1_A", "blank1_B", etc.
            parts = o["label"].split("_", 1)
            if len(parts) == 2 and o["is_correct"]:
                correct[parts[0]] = parts[1]
        if not correct:
            # Data corruption guard: a TC question with zero is_correct options
            # used to silently credit any answer because all() over an empty
            # iterable returns True.
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
    def estimate_scaled_score(raw_correct, difficulty_band="medium"):
        """
        Estimate a GRE scaled score range from raw correct count.

        Returns:
            (low, high) tuple of estimated scaled scores (130-170).
        """
        try:
            raw = max(0, min(27, int(raw_correct)))
        except (TypeError, ValueError):
            return (130, 135)
        table = SCORE_TABLES.get(difficulty_band, SCORE_TABLES["medium"])
        return table.get(raw, (130, 135))

    @staticmethod
    def compute_session_scores(verbal_raw, verbal_band,
                                quant_raw, quant_band):
        """
        Compute full session scores.

        Returns dict with raw and estimated scores.
        """
        v_low, v_high = ScoringEngine.estimate_scaled_score(verbal_raw, verbal_band)
        q_low, q_high = ScoringEngine.estimate_scaled_score(quant_raw, quant_band)
        return {
            "verbal_raw": verbal_raw,
            "quant_raw": quant_raw,
            "verbal_estimated_low": v_low,
            "verbal_estimated_high": v_high,
            "quant_estimated_low": q_low,
            "quant_estimated_high": q_high,
        }
