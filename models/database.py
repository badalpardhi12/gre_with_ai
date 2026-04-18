"""
Database models using Peewee ORM with SQLite backend.
Covers the full GRE mock platform data model: questions, sessions,
responses, scoring, AWA, and telemetry.
"""
import json
from datetime import datetime

from peewee import (
    Model, SqliteDatabase, AutoField, CharField, TextField,
    IntegerField, FloatField, BooleanField, DateTimeField,
    ForeignKeyField, Check,
)

from config import DB_PATH

# Ensure data directory exists
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

db = SqliteDatabase(str(DB_PATH), pragmas={
    "journal_mode": "wal",
    "cache_size": -1024 * 64,  # 64 MB
    "foreign_keys": 1,
    "busy_timeout": 5000,
})


class BaseModel(Model):
    class Meta:
        database = db


# ── Content Models ────────────────────────────────────────────────────

class Stimulus(BaseModel):
    """Shared passage, graph, or table that one or more questions reference."""
    id = AutoField()
    stimulus_type = CharField(choices=[
        ("passage", "Passage"),
        ("graph", "Graph"),
        ("table", "Table"),
    ])
    title = CharField(default="")
    content = TextField()  # HTML or plain text for passages; JSON for tables
    render_spec = TextField(default="")  # optional rendering hints (JSON)
    created_at = DateTimeField(default=datetime.now)


class Question(BaseModel):
    """A single GRE question item."""
    id = AutoField()
    version = IntegerField(default=1)
    measure = CharField(choices=[
        ("verbal", "Verbal Reasoning"),
        ("quant", "Quantitative Reasoning"),
        ("awa", "Analytical Writing"),
    ])
    subtype = CharField()  # rc_single, rc_multi, rc_select_passage,
                           # tc, se, qc, mcq_single, mcq_multi,
                           # numeric_entry, data_interp, awa_issue
    stimulus = ForeignKeyField(Stimulus, backref="questions", null=True)
    prompt = TextField()           # The question stem / prompt text (HTML OK)
    difficulty_target = IntegerField(default=3,
                                     constraints=[Check("difficulty_target BETWEEN 1 AND 5")])
    time_target_seconds = IntegerField(default=90)
    concept_tags = TextField(default="[]")  # JSON list of tags

    # NEW: Taxonomy-aware fields (Phase 0 schema upgrade)
    topic = CharField(default="", index=True)       # e.g. "algebra", "reading_comprehension"
    subtopic = CharField(default="", index=True)    # e.g. "linear_equations_systems"
    question_type = CharField(default="")           # for RC: "main_idea", "inference", etc.
    source = CharField(default="imported")          # kaplan_2024 | princeton_2012 |
                                                    # ai_generated | ets_official | seed
    quality_score = FloatField(null=True)           # LLM-validated quality 0-1
    mastery_difficulty = FloatField(null=True)      # IRT-style difficulty -3 to +3

    provenance = CharField(default="imported",
                           choices=[
                               ("expert", "Expert-authored"),
                               ("imported", "Imported from dataset"),
                               ("llm_generated", "LLM-generated"),
                               ("llm_reviewed", "LLM-generated then reviewed"),
                           ])
    status = CharField(default="live",
                       choices=[
                           ("draft", "Draft"),
                           ("review", "Under review"),
                           ("pilot", "Pilot / beta"),
                           ("live", "Live"),
                           ("retired", "Retired"),
                       ])
    explanation = TextField(default="")  # Explanation / solution text (HTML OK)
    created_at = DateTimeField(default=datetime.now)
    updated_at = DateTimeField(default=datetime.now)

    def get_tags(self):
        return json.loads(self.concept_tags) if self.concept_tags else []

    def set_tags(self, tags):
        self.concept_tags = json.dumps(tags)


class QuestionOption(BaseModel):
    """Answer choice for a question (MCQ, QC, SE, TC)."""
    id = AutoField()
    question = ForeignKeyField(Question, backref="options", on_delete="CASCADE")
    option_label = CharField()   # "A", "B", "C", etc.
    option_text = TextField()    # The choice text (HTML OK)
    is_correct = BooleanField(default=False)

    class Meta:
        indexes = (
            (("question", "option_label"), True),  # unique per question
        )


class NumericAnswer(BaseModel):
    """Correct answer metadata for Numeric Entry questions."""
    id = AutoField()
    question = ForeignKeyField(Question, backref="numeric_answers", on_delete="CASCADE")
    # For single-value answers
    exact_value = FloatField(null=True)
    # For fraction answers
    numerator = IntegerField(null=True)
    denominator = IntegerField(null=True)
    # Tolerance for rounding
    tolerance = FloatField(default=0.0)


# ── Session & Response Models ─────────────────────────────────────────

class Session(BaseModel):
    """One user test-taking session."""
    id = AutoField()
    test_type = CharField(choices=[
        ("full_mock", "Full Mock"),
        ("section", "Section Test"),
        ("drill", "Topic Drill"),
        ("custom", "Custom Test"),
    ])
    mode = CharField(default="simulation",
                     choices=[
                         ("simulation", "Simulation Mode"),
                         ("learning", "Learning Mode"),
                     ])
    # Section order: JSON list e.g. ["awa","verbal_s1","verbal_s2","quant_s1","quant_s2"]
    section_order = TextField(default="[]")
    current_section_index = IntegerField(default=0)
    state = CharField(default="not_started",
                      choices=[
                          ("not_started", "Not Started"),
                          ("in_progress", "In Progress"),
                          ("paused", "Paused"),
                          ("completed", "Completed"),
                          ("abandoned", "Abandoned"),
                      ])
    started_at = DateTimeField(null=True)
    ended_at = DateTimeField(null=True)
    created_at = DateTimeField(default=datetime.now)

    def get_section_order(self):
        return json.loads(self.section_order)

    def set_section_order(self, order):
        self.section_order = json.dumps(order)


class SectionResult(BaseModel):
    """Per-section data within a session."""
    id = AutoField()
    session = ForeignKeyField(Session, backref="sections", on_delete="CASCADE")
    section_name = CharField()   # "awa", "verbal_s1", "verbal_s2", "quant_s1", "quant_s2"
    measure = CharField()       # "verbal", "quant", "awa"
    section_index = IntegerField()  # 1 or 2 within that measure
    difficulty_band = CharField(default="medium",
                                choices=[
                                    ("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard"),
                                ])
    time_limit_seconds = IntegerField()
    time_used_seconds = IntegerField(default=0)
    started_at = DateTimeField(null=True)
    ended_at = DateTimeField(null=True)
    # Questions assigned to this section (JSON list of question IDs)
    question_ids = TextField(default="[]")

    def get_question_ids(self):
        return json.loads(self.question_ids)

    def set_question_ids(self, ids):
        self.question_ids = json.dumps(ids)


class Response(BaseModel):
    """User's answer to a single question within a session."""
    id = AutoField()
    session = ForeignKeyField(Session, backref="responses", on_delete="CASCADE")
    section_result = ForeignKeyField(SectionResult, backref="responses",
                                     on_delete="CASCADE")
    question = ForeignKeyField(Question, backref="responses")
    # JSON-encoded answer: {"selected": ["A"]} or {"value": 2.5}
    # or {"numerator": 5, "denominator": 2} or {"selected_sentence": 2}
    response_payload = TextField(default="{}")
    is_marked = BooleanField(default=False)
    is_correct = BooleanField(null=True)  # set after scoring
    time_spent_seconds = IntegerField(default=0)
    answered_at = DateTimeField(null=True)
    created_at = DateTimeField(default=datetime.now)

    def get_payload(self):
        return json.loads(self.response_payload)

    def set_payload(self, payload):
        self.response_payload = json.dumps(payload)


# ── Scoring Models ────────────────────────────────────────────────────

class ScoringResult(BaseModel):
    """Aggregate scores for a completed session."""
    id = AutoField()
    session = ForeignKeyField(Session, backref="scoring", on_delete="CASCADE")
    verbal_raw = IntegerField(null=True)
    quant_raw = IntegerField(null=True)
    verbal_estimated_low = IntegerField(null=True)
    verbal_estimated_high = IntegerField(null=True)
    quant_estimated_low = IntegerField(null=True)
    quant_estimated_high = IntegerField(null=True)
    awa_estimated = FloatField(null=True)
    awa_confidence_low = FloatField(null=True)
    awa_confidence_high = FloatField(null=True)
    created_at = DateTimeField(default=datetime.now)


# ── AWA Models ────────────────────────────────────────────────────────

class AWAPrompt(BaseModel):
    """An Analytical Writing Assessment Issue prompt."""
    id = AutoField()
    prompt_text = TextField()
    instructions = TextField(default="")
    source = CharField(default="ets")  # ets, custom, llm_generated
    created_at = DateTimeField(default=datetime.now)


class AWASubmission(BaseModel):
    """User's essay submission for an AWA prompt."""
    id = AutoField()
    session = ForeignKeyField(Session, backref="awa_submissions", on_delete="CASCADE")
    prompt = ForeignKeyField(AWAPrompt, backref="submissions")
    essay_text = TextField()
    word_count = IntegerField()
    submitted_at = DateTimeField(default=datetime.now)


class AWAResult(BaseModel):
    """LLM-generated score and feedback for an AWA essay."""
    id = AutoField()
    submission = ForeignKeyField(AWASubmission, backref="results", on_delete="CASCADE")
    score_estimate = FloatField()
    score_confidence_low = FloatField()
    score_confidence_high = FloatField()
    rubric_json = TextField(default="{}")  # structured rubric breakdown
    feedback_text = TextField(default="")
    model_used = CharField(default="")
    created_at = DateTimeField(default=datetime.now)

    def get_rubric(self):
        return json.loads(self.rubric_json)


# ── Telemetry ─────────────────────────────────────────────────────────

class TelemetryEvent(BaseModel):
    """Append-only event log for analytics and audit."""
    id = AutoField()
    session = ForeignKeyField(Session, backref="events", on_delete="CASCADE")
    event_type = CharField()  # answer_changed, mark_toggled, section_started,
                              # section_ended, timer_warning, etc.
    event_payload = TextField(default="{}")  # JSON
    created_at = DateTimeField(default=datetime.now)


# ── Item Statistics (post-calibration) ────────────────────────────────

class ItemStats(BaseModel):
    """Accumulated statistics for psychometric tracking."""
    id = AutoField()
    question = ForeignKeyField(Question, backref="stats", on_delete="CASCADE", unique=True)
    exposure_count = IntegerField(default=0)
    p_value = FloatField(null=True)        # proportion correct
    discrimination = FloatField(null=True)  # point-biserial
    time_median = FloatField(null=True)     # median seconds
    updated_at = DateTimeField(default=datetime.now)


# ── Vocabulary (for TC/SE generation + flashcard module) ─────────────

class VocabWord(BaseModel):
    """GRE vocabulary word for TC/SE question generation + flashcard study."""
    id = AutoField()
    word = CharField(unique=True)
    part_of_speech = CharField(default="")
    definition = TextField(default="")
    source = CharField(default="")  # gregmat, magoosh, barrons, kaplan, etc.
    difficulty = IntegerField(default=3,
                              constraints=[Check("difficulty BETWEEN 1 AND 5")])

    # NEW: Phase 4 vocab module
    frequency_tier = IntegerField(default=2)        # 1=must-know, 2=common, 3=rare
    example_sentences = TextField(default="[]")     # JSON list of sentences
    synonyms = TextField(default="[]")              # JSON list
    antonyms = TextField(default="[]")              # JSON list
    root_analysis = TextField(default="")           # plain text
    mnemonic = TextField(default="")                # memorable hook
    theme_tags = TextField(default="[]")            # JSON list of theme labels
    audio_url = CharField(default="")               # optional pronunciation URL

    def get_examples(self):
        return json.loads(self.example_sentences) if self.example_sentences else []

    def get_synonyms(self):
        return json.loads(self.synonyms) if self.synonyms else []

    def get_themes(self):
        return json.loads(self.theme_tags) if self.theme_tags else []


class VocabRoot(BaseModel):
    """Latin/Greek root for vocab word grouping (from Kaplan Appendix B)."""
    id = AutoField()
    root = CharField(unique=True)
    language = CharField(default="latin")  # latin, greek, other
    meaning = TextField()
    example_words = TextField(default="[]")  # JSON list of GRE words using this root

    def get_example_words(self):
        return json.loads(self.example_words) if self.example_words else []


class FlashcardReview(BaseModel):
    """SRS scheduling state for a single user-word pair."""
    id = AutoField()
    word = ForeignKeyField(VocabWord, backref="reviews", on_delete="CASCADE")
    user_id = CharField(default="local")  # single-user app for now
    review_count = IntegerField(default=0)
    ease_factor = FloatField(default=2.5)         # SM-2 ease
    interval_days = IntegerField(default=1)
    stability = FloatField(default=1.0)           # FSRS stability
    difficulty = FloatField(default=5.0)          # FSRS difficulty 0-10
    last_response = IntegerField(null=True)       # 1=again, 2=hard, 3=good, 4=easy
    last_reviewed_at = DateTimeField(null=True)
    next_review_at = DateTimeField(default=datetime.now)
    created_at = DateTimeField(default=datetime.now)


# ── Lessons ──────────────────────────────────────────────────────────

class Lesson(BaseModel):
    """One per-subtopic lesson, generated from textbook content."""
    id = AutoField()
    subtopic = CharField(unique=True, index=True)  # e.g. "linear_equations_systems"
    measure = CharField()  # quant | verbal | strategy
    title = CharField()
    body_html = TextField()                 # full lesson HTML (math via KaTeX)
    examples_json = TextField(default="[]") # JSON list of worked examples
    formulas_json = TextField(default="[]") # JSON list of key formulas
    pitfalls_json = TextField(default="[]") # JSON list of common pitfalls
    source = CharField(default="generated") # generated | kaplan | princeton | hand_written
    created_at = DateTimeField(default=datetime.now)
    updated_at = DateTimeField(default=datetime.now)


# ── Mastery & Personalization ───────────────────────────────────────

class MasteryRecord(BaseModel):
    """Per-user, per-subtopic mastery tracking."""
    id = AutoField()
    user_id = CharField(default="local")
    subtopic = CharField(index=True)
    attempts = IntegerField(default=0)
    correct = IntegerField(default=0)
    mastery_score = FloatField(default=0.0)  # 0-1, EWMA of weighted accuracy
    last_attempt_at = DateTimeField(null=True)
    last_updated_at = DateTimeField(default=datetime.now)

    class Meta:
        indexes = (
            (("user_id", "subtopic"), True),
        )


class StudyPlan(BaseModel):
    """AI-generated study plan for a user."""
    id = AutoField()
    user_id = CharField(default="local")
    target_score = IntegerField()           # target combined Q+V (260-340)
    test_date = DateTimeField()
    hours_per_week = IntegerField(default=10)
    plan_json = TextField(default="{}")      # week-by-week schedule
    created_at = DateTimeField(default=datetime.now)
    last_replanned_at = DateTimeField(default=datetime.now)
    is_active = BooleanField(default=True)


class DiagnosticResult(BaseModel):
    """User's diagnostic test results - feeds study plan generator."""
    id = AutoField()
    user_id = CharField(default="local")
    session_id = IntegerField(null=True)  # link to Session if used the test infra
    scores_per_topic_json = TextField(default="{}")   # {topic: accuracy}
    weakness_ranking_json = TextField(default="[]")   # ordered list of weakest subtopics
    predicted_verbal_band = CharField(default="")     # e.g. "150-160"
    predicted_quant_band = CharField(default="")
    completed_at = DateTimeField(default=datetime.now)


# ── Database initialization ──────────────────────────────────────────

ALL_TABLES = [
    Stimulus, Question, QuestionOption, NumericAnswer,
    Session, SectionResult, Response,
    ScoringResult,
    AWAPrompt, AWASubmission, AWAResult,
    TelemetryEvent, ItemStats,
    VocabWord, VocabRoot, FlashcardReview,
    Lesson,
    MasteryRecord, StudyPlan, DiagnosticResult,
]


def init_db():
    """Create all tables if they don't exist."""
    db.connect(reuse_if_open=True)
    db.create_tables(ALL_TABLES, safe=True)
    db.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
