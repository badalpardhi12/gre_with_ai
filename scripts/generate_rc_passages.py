#!/usr/bin/env python3
"""
Reading Comprehension passage generator (Phase 4 · D7).

Builds RC items from public-domain prose. The pipeline:

    source fetch  →  LLM condense (GRE register, ~400 words)
                   →  LLM generate 3–4 linked questions
                   →  LLM judge (single, 1–5 rating; <4 rejects)
                   →  upsert Stimulus + Questions (status='candidate')

SOURCES
    gutenberg   curated list of 50 public-domain humanities books
                (Project Gutenberg plain-text mirrors). We grab a
                ~2000-word chunk starting at a deterministic offset.
    plos        PLOS ONE open-access API — abstract + intro of a
                recent paper. Registers cleanly as "formal science"
                prose.
    wikipedia   lead section of a featured article.

LLM WORKFLOW
    Three LLM calls per passage: condense, generate, judge. A single
    judge is intentional for RC — the hand-review queue catches the
    rest, and we want to conserve budget vs. the dual-judge quant
    pipeline (D6).

IDEMPOTENCY
    A per-source SHA-1 hash of the raw source text is persisted in
    ``Stimulus.render_spec`` (``source_hash`` key). Re-running the
    pipeline against the same source text short-circuits: no new
    Stimulus, no new LLM calls. Questions link to the reused Stimulus
    by ``stimulus_id``.

NETWORK SAFETY
    Tests mock every fetcher and every LLM call. A ``--dry-run`` on
    ``--count 0`` performs no I/O and exits cleanly.

USAGE
    # parse-only smoke test
    python scripts/generate_rc_passages.py --count 0 --dry-run

    # generate 5 Wikipedia-sourced passages
    python scripts/generate_rc_passages.py --count 5 --source wikipedia

    # dry-run against Gutenberg
    python scripts/generate_rc_passages.py --count 3 --source gutenberg --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# Make project root importable when run as a script.
_THIS = Path(__file__).resolve()
PROJECT_ROOT = _THIS.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Constants ─────────────────────────────────────────────────────────

PROMPT_VERSION = 1
SOURCE_TAG = "ai_generated_rc_v2"
DEFAULT_PASSAGE_LENGTH = 400
MIN_JUDGE_SCORE = 4  # inclusive; 1-5 scale — below this we reject.
NETWORK_TIMEOUT = 30

# Budget guardrail: even at default count=1 we cap the source chunk at
# this many characters before handing off to the LLM. Protects against
# a pathological 200 KB Gutenberg chunk blowing the context window.
MAX_SOURCE_CHARS = 12000


# ── Curated source catalog ────────────────────────────────────────────
#
# 50 public-domain humanities texts from Project Gutenberg — philosophy,
# history, literary criticism, political theory, natural history. All
# pre-1929 so unambiguously US public domain. The URL pattern is the
# plain-text UTF-8 mirror (``/files/<id>/<id>-0.txt``).

GUTENBERG_CATALOG: List[Dict[str, str]] = [
    {"id": "1232",   "title": "The Prince (Machiavelli)"},
    {"id": "3207",   "title": "Leviathan (Hobbes)"},
    {"id": "3300",   "title": "The Wealth of Nations (Smith)"},
    {"id": "4280",   "title": "Critique of Pure Reason (Kant)"},
    {"id": "5827",   "title": "The Problems of Philosophy (Russell)"},
    {"id": "10615",  "title": "The Analects (Confucius)"},
    {"id": "2680",   "title": "Meditations (Marcus Aurelius)"},
    {"id": "1497",   "title": "The Republic (Plato)"},
    {"id": "1998",   "title": "Thus Spake Zarathustra (Nietzsche)"},
    {"id": "4363",   "title": "Beyond Good and Evil (Nietzsche)"},
    {"id": "6456",   "title": "Discourse on the Method (Descartes)"},
    {"id": "4391",   "title": "An Enquiry Concerning Human Understanding (Hume)"},
    {"id": "34901",  "title": "On Liberty (Mill)"},
    {"id": "11224",  "title": "Utilitarianism (Mill)"},
    {"id": "46423",  "title": "The Federalist Papers"},
    {"id": "203",    "title": "Uncle Tom's Cabin (Stowe)"},
    {"id": "2000",   "title": "Don Quixote (Cervantes)"},
    {"id": "44133",  "title": "Walden (Thoreau)"},
    {"id": "205",    "title": "Walden, and On The Duty of Civil Disobedience"},
    {"id": "408",    "title": "The Souls of Black Folk (Du Bois)"},
    {"id": "23700",  "title": "The Communist Manifesto (Marx/Engels)"},
    {"id": "946",    "title": "A Modest Proposal (Swift)"},
    {"id": "45343",  "title": "The Interpretation of Dreams (Freud)"},
    {"id": "2451",   "title": "The Theory of the Leisure Class (Veblen)"},
    {"id": "132",    "title": "The Art of War (Sun Tzu)"},
    {"id": "1656",   "title": "The Varieties of Religious Experience (James)"},
    {"id": "11",     "title": "Alice's Adventures in Wonderland (Carroll)"},
    {"id": "43",     "title": "The Strange Case of Dr Jekyll and Mr Hyde"},
    {"id": "1184",   "title": "The Count of Monte Cristo (Dumas)"},
    {"id": "1399",   "title": "Anna Karenina (Tolstoy)"},
    {"id": "2554",   "title": "Crime and Punishment (Dostoyevsky)"},
    {"id": "100",    "title": "The Complete Works of Shakespeare"},
    {"id": "2542",   "title": "A Doll's House (Ibsen)"},
    {"id": "1260",   "title": "Jane Eyre (Brontë)"},
    {"id": "768",    "title": "Wuthering Heights (Brontë)"},
    {"id": "98",     "title": "A Tale of Two Cities (Dickens)"},
    {"id": "1400",   "title": "Great Expectations (Dickens)"},
    {"id": "174",    "title": "The Picture of Dorian Gray (Wilde)"},
    {"id": "4217",   "title": "A Portrait of the Artist as a Young Man (Joyce)"},
    {"id": "84",     "title": "Frankenstein (Shelley)"},
    {"id": "215",    "title": "The Call of the Wild (London)"},
    {"id": "1342",   "title": "Pride and Prejudice (Austen)"},
    {"id": "161",    "title": "Sense and Sensibility (Austen)"},
    {"id": "521",    "title": "The Life and Adventures of Robinson Crusoe"},
    {"id": "74",     "title": "The Adventures of Tom Sawyer (Twain)"},
    {"id": "76",     "title": "Adventures of Huckleberry Finn (Twain)"},
    {"id": "1661",   "title": "The Adventures of Sherlock Holmes (Doyle)"},
    {"id": "5200",   "title": "Metamorphosis (Kafka)"},
    {"id": "160",    "title": "The Awakening, and Selected Short Stories (Chopin)"},
    {"id": "2591",   "title": "Grimms' Fairy Tales"},
]


# ── Source sampling ───────────────────────────────────────────────────


def _fetch_url(url: str, timeout: int = NETWORK_TIMEOUT) -> str:
    """Fetch ``url`` and return decoded text. Raises on any error.

    Kept tiny and exception-friendly — callers wrap this in try/except
    and log failures. Never invoked in tests (all fetchers are mocked).
    """
    req = urllib.request.Request(
        url, headers={"User-Agent": "gre-mock-rc-gen/0.1"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    # Gutenberg mirrors serve UTF-8 with a BOM occasionally; decode
    # permissively so one mojibake byte doesn't tank an entire pull.
    return raw.decode("utf-8", errors="replace")


def fetch_gutenberg_chunk(
    book_id: Optional[str] = None,
    target_words: int = 2000,
    rng: Optional[random.Random] = None,
) -> Tuple[str, str]:
    """Return (source_label, ~target_words-word chunk) from a Gutenberg book.

    If ``book_id`` is None, picks one from ``GUTENBERG_CATALOG`` via the
    supplied RNG (deterministic in tests). Returns the label
    ``"gutenberg:<id> <title>"`` so provenance is recoverable.
    """
    rng = rng or random.Random()
    if book_id is None:
        entry = rng.choice(GUTENBERG_CATALOG)
    else:
        match = [e for e in GUTENBERG_CATALOG if e["id"] == book_id]
        entry = match[0] if match else {"id": book_id, "title": f"book-{book_id}"}

    # UTF-8 plain-text mirror pattern.
    url = f"https://www.gutenberg.org/files/{entry['id']}/{entry['id']}-0.txt"
    text = _fetch_url(url)

    # Strip the Project Gutenberg header/footer boilerplate if present.
    start_marker = "*** START OF"
    end_marker = "*** END OF"
    if start_marker in text:
        text = text.split(start_marker, 1)[1]
        # Skip past the rest of that header line.
        text = text.split("\n", 1)[1] if "\n" in text else text
    if end_marker in text:
        text = text.split(end_marker, 1)[0]

    # Pick a mid-book offset so we don't always land on the preface.
    words = text.split()
    if len(words) <= target_words:
        chunk = text
    else:
        max_start = max(0, len(words) - target_words)
        start = rng.randint(len(words) // 10, max_start)
        chunk = " ".join(words[start:start + target_words])

    label = f"gutenberg:{entry['id']} {entry['title']}"
    return label, chunk[:MAX_SOURCE_CHARS]


def fetch_plos_article() -> Tuple[str, str]:
    """Return (source_label, abstract+intro text) from a recent PLOS ONE paper.

    Uses the PLOS search API (``api.plos.org/search``) to pull a recent
    article, then formats the abstract + first few paragraphs. On any
    API-shape surprise, raises — caller logs and skips.
    """
    # Recent PLOS ONE papers, JSON response.
    query = urllib.parse.quote(
        'journal:"PLOS ONE" AND subject:"science"'
    )
    url = (
        f"https://api.plos.org/search?q={query}"
        "&fl=id,title,abstract,author_display"
        "&wt=json&rows=10&sort=publication_date%20desc"
    )
    raw = _fetch_url(url)
    payload = json.loads(raw)
    docs = payload.get("response", {}).get("docs", [])
    if not docs:
        raise RuntimeError("PLOS search returned no documents")

    doc = docs[0]
    title = doc.get("title", "untitled")
    abstract_raw = doc.get("abstract", "")
    if isinstance(abstract_raw, list):
        abstract_text = "\n\n".join(abstract_raw)
    else:
        abstract_text = str(abstract_raw)

    pid = doc.get("id", "unknown")
    label = f"plos:{pid} {title}"
    body = f"Title: {title}\n\n{abstract_text}"
    return label, body[:MAX_SOURCE_CHARS]


def fetch_wikipedia_featured() -> Tuple[str, str]:
    """Return (source_label, lead_section_text) from a featured article.

    Uses Wikipedia's REST summary API with a static list of featured
    article slugs — deterministic enough for production and trivial to
    mock in tests.
    """
    # Small rotating list; a full featured-article feed would be nicer
    # but adds a preflight call. This is intentional.
    slugs = [
        "Alan_Turing",
        "Albert_Einstein",
        "Marie_Curie",
        "Charles_Darwin",
        "Isaac_Newton",
        "Ada_Lovelace",
        "Nikola_Tesla",
        "Richard_Feynman",
        "Rosalind_Franklin",
        "Henrietta_Leavitt",
    ]
    slug = random.choice(slugs)
    url = (
        "https://en.wikipedia.org/api/rest_v1/page/summary/"
        + urllib.parse.quote(slug)
    )
    raw = _fetch_url(url)
    payload = json.loads(raw)
    extract = payload.get("extract", "")
    if not extract:
        raise RuntimeError(f"Wikipedia summary empty for {slug!r}")

    label = f"wikipedia:{slug}"
    return label, extract[:MAX_SOURCE_CHARS]


# Registry of fetchers — tests patch individual entries via monkeypatch.
SOURCE_FETCHERS: Dict[str, Callable[[], Tuple[str, str]]] = {
    "gutenberg": lambda: fetch_gutenberg_chunk(),
    "plos":      fetch_plos_article,
    "wikipedia": fetch_wikipedia_featured,
}


# ── LLM prompts ───────────────────────────────────────────────────────


_CONDENSE_SYSTEM = (
    "You are a GRE editorial assistant. Condense source prose into a "
    "GRE-register Reading Comprehension passage: formal, academic, "
    "argumentative in register, neither colloquial nor gratuitously "
    "technical. Preserve the core argument or finding of the source. "
    "The passage must stand alone — a reader with no prior familiarity "
    "with the source should follow it. Output plain text only: no "
    "headings, no lists, no markdown."
)


def _condense_user_prompt(source_text: str, target_length: int) -> str:
    return (
        f"Target length: ~{target_length} words.\n\n"
        "Source:\n"
        f"{source_text}\n\n"
        "Write the GRE-register RC passage now."
    )


_GENERATE_SYSTEM = (
    "You are a GRE item writer. Given a Reading Comprehension passage, "
    "write 3 or 4 high-quality, non-overlapping questions. Aim for a "
    "mix across: main-idea, inference, detail, and function/purpose. "
    "Every question must have exactly 5 options (labeled A–E), exactly "
    "one correct answer, and a concise explanation that cites evidence "
    "from the passage. Output valid JSON only — no prose, no markdown "
    "fences."
)


def _generate_user_prompt(passage: str) -> str:
    return (
        "Passage:\n"
        f"{passage}\n\n"
        "Output a JSON object with a single top-level key \"questions\" "
        "whose value is a list of 3 or 4 items. Each item must have:\n"
        '  "question_type": one of "main_idea" | "inference" | "detail" | "function"\n'
        '  "stem": the question stem (string)\n'
        '  "options": a list of exactly 5 strings (in A..E order)\n'
        '  "correct_label": one of "A","B","C","D","E"\n'
        '  "explanation": short evidence-based explanation (string)\n'
    )


_JUDGE_SYSTEM = (
    "You are a GRE item-quality judge. Rate a single RC item for GRE "
    "suitability on a 1–5 scale. 5 = publishable; 4 = minor polish; 3 = "
    "plausible but noticeably flawed; 2 = ambiguous or two-plausible; "
    "1 = trivial or broken. Penalize multiple-plausible answers, "
    "trivially-easy items, and off-passage trivia. Output valid JSON "
    "only — no prose, no markdown fences."
)


def _judge_user_prompt(passage: str, item: dict) -> str:
    return (
        "Passage:\n"
        f"{passage}\n\n"
        "Item:\n"
        f"  Stem: {item.get('stem', '')}\n"
        f"  Options: {json.dumps(item.get('options', []))}\n"
        f"  Correct: {item.get('correct_label', '')}\n"
        f"  Explanation: {item.get('explanation', '')}\n\n"
        "Output JSON:\n"
        '  "score": integer 1-5\n'
        '  "reason": short free-text rationale\n'
    )


# ── Validation ────────────────────────────────────────────────────────


def _validate_question_payload(payload) -> Optional[str]:
    """Return an error string if the LLM question payload is malformed."""
    if not isinstance(payload, dict):
        return f"expected dict, got {type(payload).__name__}"
    questions = payload.get("questions")
    if not isinstance(questions, list) or not (3 <= len(questions) <= 4):
        return "questions must be a list of 3 or 4 items"
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            return f"question #{i} must be a dict"
        for key in ("question_type", "stem", "options", "correct_label", "explanation"):
            if key not in q:
                return f"question #{i} missing key {key!r}"
        opts = q["options"]
        if not isinstance(opts, list) or len(opts) != 5:
            return f"question #{i} options must be a list of 5 strings"
        for j, o in enumerate(opts):
            if not isinstance(o, str) or not o.strip():
                return f"question #{i} option #{j} empty"
        if q["correct_label"] not in ("A", "B", "C", "D", "E"):
            return f"question #{i} correct_label invalid"
        for skey in ("stem", "explanation", "question_type"):
            if not isinstance(q[skey], str) or not q[skey].strip():
                return f"question #{i} {skey} empty"
    return None


def _validate_judge_payload(payload) -> Optional[str]:
    if not isinstance(payload, dict):
        return f"expected dict, got {type(payload).__name__}"
    score = payload.get("score")
    if not isinstance(score, int) or not (1 <= score <= 5):
        return "score must be int 1..5"
    return None


# ── Source hashing ────────────────────────────────────────────────────


def _source_hash(source_label: str, source_text: str) -> str:
    """SHA-1 over (label, text). Stable across runs → idempotency key."""
    h = hashlib.sha1()
    h.update(source_label.encode("utf-8"))
    h.update(b"\x00")
    h.update(source_text.encode("utf-8"))
    return h.hexdigest()


# ── Pipeline record ───────────────────────────────────────────────────


@dataclass
class RCCandidate:
    """Intermediate pipeline artifact — a passage with its questions."""
    source_label: str
    source_hash: str
    passage_text: str
    questions: List[dict] = field(default_factory=list)
    judge_scores: List[int] = field(default_factory=list)


# ── LLM orchestration ────────────────────────────────────────────────


def _llm_condense(llm, source_text: str, target_length: int) -> str:
    raw = llm.generate(
        _CONDENSE_SYSTEM,
        _condense_user_prompt(source_text, target_length),
    )
    return (raw or "").strip()


def _llm_generate_questions(llm, passage: str) -> dict:
    return llm.generate_json(
        _GENERATE_SYSTEM,
        _generate_user_prompt(passage),
    )


def _llm_judge(llm, passage: str, item: dict) -> dict:
    return llm.generate_json(
        _JUDGE_SYSTEM,
        _judge_user_prompt(passage, item),
    )


def build_candidate(
    llm,
    source_label: str,
    source_text: str,
    passage_length: int = DEFAULT_PASSAGE_LENGTH,
) -> Optional[RCCandidate]:
    """Run condense → generate → judge for one source text.

    Returns None if any stage fails (LLM exception, validation failure,
    no questions survive the judge). Never raises — callers just count
    the ``None`` results as skips.
    """
    from services.log import get_logger
    logger = get_logger("generate_rc_passages")

    src_hash = _source_hash(source_label, source_text)

    # Stage 2 — condense
    try:
        passage = _llm_condense(llm, source_text, passage_length)
    except Exception as exc:
        logger.warning("condense failed for %s: %s", source_label, exc)
        return None
    if not passage or len(passage.split()) < passage_length // 2:
        logger.warning("condense output too short for %s (%d words)",
                       source_label, len(passage.split()) if passage else 0)
        return None

    # Stage 3 — generate questions
    try:
        q_payload = _llm_generate_questions(llm, passage)
    except Exception as exc:
        logger.warning("generate failed for %s: %s", source_label, exc)
        return None
    err = _validate_question_payload(q_payload)
    if err is not None:
        logger.warning("generate payload invalid for %s: %s", source_label, err)
        return None

    # Stage 4 — judge each question; keep only those ≥ MIN_JUDGE_SCORE.
    kept: List[dict] = []
    scores: List[int] = []
    for item in q_payload["questions"]:
        try:
            verdict = _llm_judge(llm, passage, item)
        except Exception as exc:
            logger.warning("judge failed for %s: %s", source_label, exc)
            continue
        if _validate_judge_payload(verdict) is not None:
            logger.warning("judge payload invalid for %s", source_label)
            continue
        score = int(verdict["score"])
        if score < MIN_JUDGE_SCORE:
            logger.info("judge rejected item for %s (score=%d, reason=%s)",
                        source_label, score, verdict.get("reason", ""))
            continue
        kept.append(item)
        scores.append(score)

    # Need at least 3 surviving questions for rc_multi to be meaningful.
    if len(kept) < 3:
        logger.info("only %d items survived judging for %s — skipping",
                    len(kept), source_label)
        return None

    return RCCandidate(
        source_label=source_label,
        source_hash=src_hash,
        passage_text=passage,
        questions=kept,
        judge_scores=scores,
    )


# ── DB upsert ─────────────────────────────────────────────────────────


def upsert_candidate(candidate: RCCandidate) -> Tuple[int, int, bool]:
    """Insert a Stimulus + linked Questions. Idempotent by source_hash.

    Returns (stimulus_id, questions_inserted, was_reused).
    ``was_reused=True`` if a Stimulus with the same source_hash already
    existed — in that case we do NOT insert new Questions (the prior
    run already did).

    Phase 1.4 hook: each candidate question is run through the dedup
    service before insert. If every question in a passage is dropped
    as a duplicate, the Stimulus is rolled back too — RC passages with
    zero live questions have no purpose and would just orphan the row.
    Note that DI/RC siblings often share a stimulus and would normally
    look identical to a coarse cosine measure, but the cross-encoder
    rerank handles that — see services/dedup/embedding_stage.py.
    """
    from models.database import db, init_db, Stimulus, Question, QuestionOption
    from services.dedup import get_dedup_service
    init_db()
    db.connect(reuse_if_open=True)

    dedup_svc = get_dedup_service()

    # Idempotency: look up existing Stimulus by source_hash in render_spec.
    # render_spec is a JSON blob, so we scan narrowly (RC passages are
    # <1000 rows — a LIKE probe is fine, and cheaper than a migration).
    for existing in Stimulus.select().where(
        (Stimulus.stimulus_type == "passage")
        & (Stimulus.render_spec.contains(candidate.source_hash))
    ):
        try:
            spec = json.loads(existing.render_spec) if existing.render_spec else {}
        except (ValueError, TypeError):
            spec = {}
        if spec.get("source_hash") == candidate.source_hash:
            return existing.id, 0, True

    render_spec = json.dumps({
        "source_label": candidate.source_label,
        "source_hash": candidate.source_hash,
        "pipeline": "ai_generated_rc_v2",
        "prompt_version": PROMPT_VERSION,
    })

    with db.atomic():
        stim = Stimulus.create(
            stimulus_type="passage",
            title=candidate.source_label[:240],
            content=candidate.passage_text,
            render_spec=render_spec,
        )

        inserted = 0
        for idx, item in enumerate(candidate.questions):
            # Phase 1.4: dedup against the live bank.
            dup_qid = dedup_svc.find_dup_for(
                prompt=item["stem"],
                stimulus_content=candidate.passage_text or "",
                options=list(item.get("options", []) or []),
                source=SOURCE_TAG,
            )
            if dup_qid is not None:
                continue

            provenance_payload = {
                "pipeline": "ai_generated_rc_v2",
                "source_label": candidate.source_label,
                "source_hash": candidate.source_hash,
                "judge_score": (
                    candidate.judge_scores[idx]
                    if idx < len(candidate.judge_scores) else None
                ),
                "prompt_version": PROMPT_VERSION,
            }
            q = Question.create(
                measure="verbal",
                subtype="rc_multi",
                stimulus=stim,
                prompt=item["stem"],
                difficulty_target=3,
                time_target_seconds=90,
                topic="reading_comprehension",
                question_type=item.get("question_type", ""),
                source=SOURCE_TAG,
                source_anchor=f"{candidate.source_hash[:10]}_q{idx+1:02d}",
                provenance="llm_generated",
                status="candidate",
                explanation=item.get("explanation", ""),
                provenance_json=json.dumps(provenance_payload),
            )
            for label, text in zip(("A", "B", "C", "D", "E"), item["options"]):
                QuestionOption.create(
                    question=q,
                    option_label=label,
                    option_text=text,
                    is_correct=(label == item["correct_label"]),
                )
            inserted += 1

        return stim.id, inserted, False


# ── Top-level orchestration ───────────────────────────────────────────


def run_pipeline(
    count: int,
    source: str,
    passage_length: int = DEFAULT_PASSAGE_LENGTH,
    dry_run: bool = False,
    llm=None,
    fetcher: Optional[Callable[[], Tuple[str, str]]] = None,
) -> Dict[str, int]:
    """Generate ``count`` passages end-to-end and upsert each.

    Args:
        count: how many passages to attempt.
        source: one of ``"gutenberg" | "plos" | "wikipedia"``.
        passage_length: target word count for the condensed passage.
        dry_run: if True, skip DB writes entirely.
        llm: optional LLM client override (tests inject a MagicMock);
            falls back to ``services.llm_service.llm_service``.
        fetcher: optional callable returning ``(label, text)`` —
            overrides the registered source fetcher (tests inject a
            deterministic fixture).

    Returns a summary dict: {attempted, built, inserted, reused, skipped}.
    """
    if source not in SOURCE_FETCHERS:
        raise ValueError(
            f"unknown source {source!r}; "
            f"expected one of {sorted(SOURCE_FETCHERS)}"
        )
    _fetcher = fetcher or SOURCE_FETCHERS[source]

    if llm is None:
        from services.llm_service import llm_service
        llm = llm_service

    from services.log import get_logger
    logger = get_logger("generate_rc_passages")

    summary = {"attempted": 0, "built": 0,
               "inserted": 0, "reused": 0, "skipped": 0}

    for _ in range(max(0, int(count))):
        summary["attempted"] += 1
        try:
            label, text = _fetcher()
        except Exception as exc:
            logger.warning("fetcher failed: %s", exc)
            summary["skipped"] += 1
            continue
        if not text or not text.strip():
            logger.warning("empty source text for %s", label)
            summary["skipped"] += 1
            continue

        candidate = build_candidate(llm, label, text, passage_length)
        if candidate is None:
            summary["skipped"] += 1
            continue
        summary["built"] += 1

        if dry_run:
            continue

        _, inserted, reused = upsert_candidate(candidate)
        if reused:
            summary["reused"] += 1
        else:
            summary["inserted"] += inserted

    return summary


# ── CLI ───────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate GRE Reading Comprehension items from "
                    "public-domain prose via a 3-stage LLM + judge pipeline.",
    )
    parser.add_argument("--count", type=int, default=0,
                        help="Number of passages to generate (default: 0).")
    parser.add_argument("--source",
                        choices=sorted(SOURCE_FETCHERS),
                        default="gutenberg",
                        help="Source bucket to sample from.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the pipeline but skip DB writes.")
    parser.add_argument("--passage-length", type=int,
                        default=DEFAULT_PASSAGE_LENGTH,
                        help="Target word count for each condensed passage.")
    args = parser.parse_args(argv)

    print(f"RC generation — count={args.count} source={args.source} "
          f"dry_run={args.dry_run} passage_length={args.passage_length}")

    if args.count <= 0:
        print("[no-op] count=0 — nothing to do")
        return 0

    summary = run_pipeline(
        count=args.count,
        source=args.source,
        passage_length=args.passage_length,
        dry_run=args.dry_run,
    )
    print(f"\nSummary: {json.dumps(summary)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
