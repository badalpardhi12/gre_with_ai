"""
Generate explanations for questions missing them using Claude (Anthropic API).

Uses the Anthropic SDK with configurable endpoint. Supports:
- Standard Anthropic API (set ANTHROPIC_API_KEY)
- Custom proxy/endpoint via ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN

Saves progress incrementally so you can resume if interrupted.

Usage:
    # Configure environment first:
    export ANTHROPIC_BASE_URL=...           # Optional: custom endpoint
    export ANTHROPIC_AUTH_TOKEN=...         # OAuth/proxy token
    # OR
    export ANTHROPIC_API_KEY=sk-ant-...     # Standard Anthropic API key

    python scripts/generate_explanations.py                    # generate for all
    python scripts/generate_explanations.py --limit 10         # only first 10
    python scripts/generate_explanations.py --measure verbal   # only verbal
    python scripts/generate_explanations.py --dry-run          # preview only
    python scripts/generate_explanations.py --model MODEL_ID   # override model
    python scripts/generate_explanations.py --workers 4        # parallel requests
"""
import argparse
import concurrent.futures
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.database import db, init_db, Question, QuestionOption, NumericAnswer


SYSTEM_PROMPT = """You are an expert GRE tutor explaining why answers are correct or incorrect.

Your job: given a GRE question, its answer choices, and the correct answer(s), write a clear, concise explanation.

GUIDELINES:
- Start by identifying the correct answer.
- Explain WHY it's correct, walking through the reasoning step by step.
- For verbal: explain the relationship to context clues, vocabulary meaning, or passage evidence.
- For quant: show the math steps clearly, including formulas used.
- Briefly explain why 1-2 of the most tempting wrong answers are incorrect (when relevant).
- Keep it 3-6 sentences. Concise but complete.
- Use plain text, no markdown or formatting.

Output ONLY the explanation text. No preamble, no headers."""


def get_anthropic_client(model_id):
    """Create an Anthropic client. Supports custom base URL and auth token."""
    from anthropic import Anthropic

    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    kwargs = {}
    if base_url:
        kwargs["base_url"] = base_url
    if auth_token:
        kwargs["auth_token"] = auth_token
    elif api_key:
        kwargs["api_key"] = api_key
    else:
        raise RuntimeError(
            "No authentication configured. Set ANTHROPIC_API_KEY for the standard "
            "Anthropic API, or ANTHROPIC_AUTH_TOKEN (and optionally ANTHROPIC_BASE_URL) "
            "for a custom endpoint."
        )

    return Anthropic(**kwargs)


def format_question_for_llm(q_data):
    """Format question data into a clear prompt for the LLM."""
    parts = []

    if q_data.get("stimulus"):
        parts.append(f"PASSAGE:\n{q_data['stimulus']}\n")

    parts.append(f"QUESTION TYPE: {q_data['subtype']}")
    parts.append(f"PROMPT:\n{q_data['prompt']}\n")

    options = q_data.get("options", [])
    if options:
        parts.append("ANSWER CHOICES:")
        for opt in options:
            marker = " (CORRECT)" if opt["is_correct"] else ""
            parts.append(f"  {opt['label']}) {opt['text']}{marker}")

    if q_data.get("numeric_answer"):
        na = q_data["numeric_answer"]
        if na.get("exact_value") is not None:
            parts.append(f"\nCORRECT ANSWER: {na['exact_value']}")
        elif na.get("numerator") is not None:
            parts.append(f"\nCORRECT ANSWER: {na['numerator']}/{na['denominator']}")

    parts.append("\nWrite a clear explanation of the correct answer.")
    return "\n".join(parts)


def build_question_data(q):
    """Build a dict from a Question model instance with all needed data."""
    data = {
        "id": q.id,
        "subtype": q.subtype,
        "measure": q.measure,
        "prompt": q.prompt,
        "options": [],
    }

    if q.stimulus:
        data["stimulus"] = q.stimulus.content

    for opt in QuestionOption.select().where(QuestionOption.question == q):
        data["options"].append({
            "label": opt.option_label,
            "text": opt.option_text,
            "is_correct": opt.is_correct,
        })

    na = NumericAnswer.get_or_none(NumericAnswer.question == q)
    if na:
        data["numeric_answer"] = {
            "exact_value": na.exact_value,
            "numerator": na.numerator,
            "denominator": na.denominator,
            "tolerance": na.tolerance,
        }

    return data


def generate_explanation(client, model_id, q_data, max_tokens=512, max_retries=8):
    """Generate an explanation for a single question. Retries on rate limits."""
    user_prompt = format_question_for_llm(q_data)

    for attempt in range(max_retries):
        try:
            message = client.messages.create(
                model=model_id,
                max_tokens=max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            # Extract text from response
            parts = []
            for block in message.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
            return "".join(parts).strip()
        except Exception as e:
            err_str = str(e)
            # Detect rate limit errors and back off
            if "429" in err_str or "rate limit" in err_str.lower() or "TOO_MANY_REQUEST" in err_str:
                if attempt == max_retries - 1:
                    raise
                # Exponential backoff: 30s, 60s, 90s, 120s, 150s, 180s, 210s, 240s
                wait = 30 * (attempt + 1)
                time.sleep(wait)
                continue
            raise

    raise RuntimeError("Max retries exceeded")


def process_question(args):
    """Worker function for parallel processing. Returns (q_id, explanation, error)."""
    q_id, q_data, client, model_id = args
    try:
        explanation = generate_explanation(client, model_id, q_data)
        return (q_id, explanation, None)
    except Exception as e:
        return (q_id, None, str(e))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Max number of questions to process")
    parser.add_argument("--measure", choices=["verbal", "quant"], default=None,
                        help="Only process specific measure")
    parser.add_argument("--subtype", default=None,
                        help="Only process specific subtype")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without calling LLM or updating DB")
    parser.add_argument("--model", default=os.environ.get("CLAUDE_MODEL", "claude-opus-4-5-20251101"),
                        help="Model ID to use (default: claude-opus-4-5-20251101)")
    parser.add_argument("--workers", type=int, default=1,
                        help="Number of parallel workers (default: 1)")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Delay between sequential requests (seconds)")
    args = parser.parse_args()

    init_db()
    db.connect(reuse_if_open=True)

    # Build query for questions without explanations
    q = Question.select().where(
        (Question.explanation == "") | (Question.explanation.is_null())
    )
    if args.measure:
        q = q.where(Question.measure == args.measure)
    if args.subtype:
        q = q.where(Question.subtype == args.subtype)

    questions = list(q)
    if args.limit:
        questions = questions[:args.limit]

    print(f"Found {len(questions)} questions needing explanations")

    if args.dry_run:
        print("\nDry run — showing first 3 question previews:\n")
        for q_obj in questions[:3]:
            data = build_question_data(q_obj)
            print(f"--- Q{q_obj.id} ({q_obj.subtype}) ---")
            print(format_question_for_llm(data))
            print()
        return

    if not questions:
        print("No questions to process.")
        return

    # Initialize Anthropic client
    try:
        client = get_anthropic_client(args.model)
    except Exception as e:
        print(f"\nERROR: {e}")
        return 1

    print(f"Using model: {args.model}")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com (default)")
    print(f"Base URL: {base_url}")
    print(f"Workers: {args.workers}\n")

    success = 0
    failed = 0
    start = time.time()

    if args.workers == 1:
        # Sequential processing with incremental saves
        for i, q_obj in enumerate(questions):
            try:
                data = build_question_data(q_obj)
                explanation = generate_explanation(client, args.model, data)

                q_obj.explanation = explanation
                q_obj.save()

                success += 1
                elapsed = time.time() - start
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                preview = explanation[:60].replace("\n", " ")
                print(f"  [{i+1}/{len(questions)}] Q{q_obj.id} ({q_obj.subtype}) "
                      f"({rate:.2f}/s) — {preview}...")

                if args.delay > 0:
                    time.sleep(args.delay)
            except KeyboardInterrupt:
                print(f"\nInterrupted at {i+1}/{len(questions)}")
                break
            except Exception as e:
                failed += 1
                print(f"  [{i+1}/{len(questions)}] Q{q_obj.id} FAILED: {e}")
                continue
    else:
        # Parallel processing
        # Build work items
        work_items = []
        question_map = {}
        for q_obj in questions:
            data = build_question_data(q_obj)
            work_items.append((q_obj.id, data, client, args.model))
            question_map[q_obj.id] = q_obj

        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process_question, item): item[0] for item in work_items}
            try:
                for future in concurrent.futures.as_completed(futures):
                    q_id, explanation, error = future.result()
                    completed += 1

                    if error:
                        failed += 1
                        print(f"  [{completed}/{len(questions)}] Q{q_id} FAILED: {error}")
                    else:
                        # Save to DB
                        q_obj = question_map[q_id]
                        q_obj.explanation = explanation
                        q_obj.save()
                        success += 1

                        elapsed = time.time() - start
                        rate = completed / elapsed if elapsed > 0 else 0
                        preview = explanation[:50].replace("\n", " ")
                        print(f"  [{completed}/{len(questions)}] Q{q_id} "
                              f"({rate:.2f}/s) — {preview}...")
            except KeyboardInterrupt:
                print(f"\nInterrupted, cancelling pending tasks...")
                for f in futures:
                    f.cancel()

    print(f"\nDone. {success} succeeded, {failed} failed.")
    print(f"Total time: {time.time() - start:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
