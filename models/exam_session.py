"""
Exam session state machine and test assembly logic.
Implements the post-September 2023 GRE format.
"""
import json
import random
from datetime import datetime
from enum import Enum
from pathlib import Path

from config import (
    AWA_TIME, VERBAL_S1_TIME, VERBAL_S2_TIME,
    QUANT_S1_TIME, QUANT_S2_TIME,
    VERBAL_S1_COUNT, VERBAL_S2_COUNT,
    QUANT_S1_COUNT, QUANT_S2_COUNT,
    ADAPT_EASY_THRESHOLD, ADAPT_HARD_THRESHOLD,
    DATA_DIR,
)


class ExamState(Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class SectionType(Enum):
    AWA = "awa"
    VERBAL_S1 = "verbal_s1"
    VERBAL_S2 = "verbal_s2"
    QUANT_S1 = "quant_s1"
    QUANT_S2 = "quant_s2"


# Section metadata: (measure, section_index, time_limit, question_count)
SECTION_META = {
    SectionType.AWA:       ("awa",    1, AWA_TIME,       1),
    SectionType.VERBAL_S1: ("verbal", 1, VERBAL_S1_TIME, VERBAL_S1_COUNT),
    SectionType.VERBAL_S2: ("verbal", 2, VERBAL_S2_TIME, VERBAL_S2_COUNT),
    SectionType.QUANT_S1:  ("quant",  1, QUANT_S1_TIME,  QUANT_S1_COUNT),
    SectionType.QUANT_S2:  ("quant",  2, QUANT_S2_TIME,  QUANT_S2_COUNT),
}


class SectionState:
    """Tracks the state of a single exam section."""

    def __init__(self, section_type, question_ids, time_limit):
        self.section_type = section_type
        self.question_ids = list(question_ids)
        self.time_limit = time_limit
        self.time_used = 0
        self.current_index = 0
        self.marked = set()
        self.responses = {}    # question_id -> payload dict
        self.started_at = None
        self.ended_at = None
        self._per_question_time = {}  # question_id -> seconds spent
        self._last_question_tick = None  # timestamp of last tick for current question

    @property
    def total_questions(self):
        return len(self.question_ids)

    @property
    def time_remaining(self):
        return max(0, self.time_limit - self.time_used)

    @property
    def is_complete(self):
        return self.ended_at is not None

    @property
    def current_question_id(self):
        if 0 <= self.current_index < len(self.question_ids):
            return self.question_ids[self.current_index]
        return None

    def navigate_to(self, index):
        """Navigate to a specific question index (within-section only)."""
        if 0 <= index < len(self.question_ids):
            self.current_index = index
            return True
        return False

    def go_next(self):
        return self.navigate_to(self.current_index + 1)

    def go_prev(self):
        return self.navigate_to(self.current_index - 1)

    def toggle_mark(self, question_id=None):
        qid = question_id or self.current_question_id
        if qid in self.marked:
            self.marked.discard(qid)
        else:
            self.marked.add(qid)

    def set_response(self, question_id, payload):
        self.responses[question_id] = payload

    def get_response(self, question_id):
        return self.responses.get(question_id)

    def tick(self, elapsed_seconds=1):
        """Advance the timer by elapsed_seconds. Returns True if time expired."""
        self.time_used += elapsed_seconds
        # Track per-question time
        qid = self.current_question_id
        if qid is not None:
            self._per_question_time[qid] = self._per_question_time.get(qid, 0) + elapsed_seconds
        return self.time_remaining <= 0

    def finish(self):
        self.ended_at = datetime.now()

    def get_review_data(self):
        """Return a list of dicts for the review screen."""
        result = []
        for i, qid in enumerate(self.question_ids):
            resp = self.responses.get(qid)
            result.append({
                "index": i,
                "question_id": qid,
                "answered": resp is not None and resp != {},
                "marked": qid in self.marked,
            })
        return result

    def count_answered(self):
        return sum(1 for qid in self.question_ids
                   if self.responses.get(qid) not in (None, {}))

    def count_correct(self, correctness_map):
        """Given {question_id: bool}, count correct answers."""
        return sum(1 for qid in self.question_ids
                   if correctness_map.get(qid, False))


class ExamSession:
    """
    Top-level state machine for a full GRE mock exam.
    AWA is always first; Verbal and Quant appear in random order.
    """

    def __init__(self, test_type="full_mock", mode="simulation"):
        self.test_type = test_type
        self.mode = mode
        self.state = ExamState.NOT_STARTED
        self.section_order = []
        self.sections = {}       # section_name -> SectionState
        self.current_section_idx = 0
        self.started_at = None
        self.ended_at = None
        self._journal_path = DATA_DIR / "autosave_journal.jsonl"

    def build_full_mock(self, question_bank):
        """
        Assemble a full mock exam from the question bank service.
        Args:
            question_bank: a QuestionBankService instance with .select_questions()
        """
        # AWA always first
        self.section_order = [SectionType.AWA]

        # Randomize Verbal/Quant order
        if random.random() < 0.5:
            self.section_order += [
                SectionType.VERBAL_S1, SectionType.VERBAL_S2,
                SectionType.QUANT_S1, SectionType.QUANT_S2,
            ]
        else:
            self.section_order += [
                SectionType.QUANT_S1, SectionType.QUANT_S2,
                SectionType.VERBAL_S1, SectionType.VERBAL_S2,
            ]

        # Store question bank for deferred S2 loading
        self._question_bank = question_bank

        # Assemble questions for each section (S2 deferred until S1 completes)
        for sec_type in self.section_order:
            measure, sec_idx, time_limit, q_count = SECTION_META[sec_type]

            if measure == "awa":
                q_ids = question_bank.select_awa_prompt()
            elif sec_idx == 1:
                q_ids = question_bank.select_questions(
                    measure=measure,
                    count=q_count,
                    difficulty_band="medium",
                )
            else:
                # S2 questions are deferred — loaded after S1 adaptation
                q_ids = []

            self.sections[sec_type] = SectionState(
                section_type=sec_type,
                question_ids=q_ids,
                time_limit=time_limit,
            )

    def build_section_test(self, measure, question_bank):
        """Build a section-only test (Verbal or Quant)."""
        self._question_bank = question_bank
        if measure == "verbal":
            self.section_order = [SectionType.VERBAL_S1, SectionType.VERBAL_S2]
        else:
            self.section_order = [SectionType.QUANT_S1, SectionType.QUANT_S2]

        for sec_type in self.section_order:
            m, sec_idx, time_limit, q_count = SECTION_META[sec_type]
            if sec_idx == 1:
                q_ids = question_bank.select_questions(
                    measure=m, count=q_count, difficulty_band="medium",
                )
            else:
                q_ids = []  # deferred until S1 completes
            self.sections[sec_type] = SectionState(
                section_type=sec_type,
                question_ids=q_ids,
                time_limit=time_limit,
            )

    def build_drill(self, measure, topic, count, question_bank):
        """Build a topic drill."""
        sec_type = SectionType.VERBAL_S1 if measure == "verbal" else SectionType.QUANT_S1
        _, _, time_limit, _ = SECTION_META[sec_type]

        q_ids = question_bank.select_questions(
            measure=measure, count=count, topic=topic,
        )
        self.section_order = [sec_type]
        self.sections[sec_type] = SectionState(
            section_type=sec_type,
            question_ids=q_ids,
            time_limit=count * 90,  # ~90s per question
        )

    def start(self):
        self.state = ExamState.IN_PROGRESS
        self.started_at = datetime.now()
        current = self.current_section
        if current:
            current.started_at = datetime.now()

    @property
    def current_section(self):
        if 0 <= self.current_section_idx < len(self.section_order):
            return self.sections[self.section_order[self.current_section_idx]]
        return None

    @property
    def current_section_type(self):
        if 0 <= self.current_section_idx < len(self.section_order):
            return self.section_order[self.current_section_idx]
        return None

    def end_current_section(self):
        """Finish current section and determine next section difficulty if adaptive."""
        current = self.current_section
        if current is None:
            return

        current.finish()

        current_type = self.current_section_type
        # Section-level adaptation: after S1, adapt S2 difficulty
        if current_type in (SectionType.VERBAL_S1, SectionType.QUANT_S1):
            self._adapt_next_section(current_type)

    def _adapt_next_section(self, completed_type):
        """Adapt S2 difficulty based on S1 correctness, then load S2 questions."""
        s1 = self.sections[completed_type]
        correctness = getattr(s1, '_correctness', {})
        total = s1.total_questions
        if total == 0:
            return

        correct_count = sum(1 for v in correctness.values() if v)
        pct_correct = correct_count / total

        if completed_type == SectionType.VERBAL_S1:
            s2_type = SectionType.VERBAL_S2
            measure = "verbal"
        else:
            s2_type = SectionType.QUANT_S2
            measure = "quant"

        if s2_type not in self.sections:
            return

        # Determine difficulty band
        if pct_correct < ADAPT_EASY_THRESHOLD:
            band = "easy"
        elif pct_correct > ADAPT_HARD_THRESHOLD:
            band = "hard"
        else:
            band = "medium"

        self.sections[s2_type].difficulty_band = band

        # Now load S2 questions with the adapted difficulty
        _, _, _, q_count = SECTION_META[s2_type]
        qb = getattr(self, '_question_bank', None)
        if qb and not self.sections[s2_type].question_ids:
            s1_ids = s1.question_ids
            q_ids = qb.select_questions(
                measure=measure,
                count=q_count,
                difficulty_band=band,
                exclude_ids=s1_ids,
            )
            self.sections[s2_type].question_ids = q_ids

    def advance_section(self):
        """Move to the next section. Returns True if exam continues, False if done."""
        self.current_section_idx += 1
        if self.current_section_idx >= len(self.section_order):
            self.state = ExamState.COMPLETED
            self.ended_at = datetime.now()
            return False
        # Start the next section timer
        next_sec = self.current_section
        if next_sec:
            next_sec.started_at = datetime.now()
        return True

    def is_finished(self):
        return self.state in (ExamState.COMPLETED, ExamState.ABANDONED)

    # ── Autosave journal ──────────────────────────────────────────────

    def log_event(self, event_type, payload=None):
        """Append an event to the local autosave journal."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "section": self.current_section_type.value if self.current_section_type else None,
            "payload": payload or {},
        }
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._journal_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def clear_journal(self):
        if self._journal_path.exists():
            self._journal_path.unlink()
