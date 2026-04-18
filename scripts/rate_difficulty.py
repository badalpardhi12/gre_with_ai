"""
Use AI to rate question difficulty (1-5) and update the database.

Difficulty scale:
1 = Easy — straightforward, basic vocab/concepts, low time pressure
2 = Medium-Easy — requires some thought, common vocab/formulas
3 = Medium — typical GRE difficulty, requires careful reasoning
4 = Hard — challenging vocab or multi-step reasoning, common in 160+ scoring
5 = Very Hard — expert-level vocab, complex multi-step problems, 165+ scoring

Sends questions in batches of 10 to a fast model to rate.

Usage:
    python scripts/rate_difficulty.py                          # rate all
    python scripts/rate_difficulty.py --limit 50               # batch test
    python scripts/rate_difficulty.py --measure quant          # just quant
    python scripts/rate_difficulty.py --only-default           # only ones at level 3 default
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.database import db, init_db, Question, QuestionOption


SYSTEM_PROMPT = """You are an expert GRE test prep instructor rating question difficulty.

Rate each GRE question on a 1-5 scale:
1 = Easy — straightforward, basic vocab/concepts, beginner GRE level (would expect <130-140 scorer to handle)
2 = Medium-Easy — requires some thought, common vocab/formulas (140-150 level)
3 = Medium — typical GRE difficulty, requires careful reasoning (150-160 level)
4 = Hard — challenging vocab or multi-step reasoning (160-165 scoring)
5 = Very Hard — expert vocab, complex multi-step problems (165-170 scoring)

Consider:
- Vocabulary difficulty (TC/SE): obscure words = harder
- Math complexity (Quant): more steps, advanced topics = harder
- Reading comprehension: dense text, subtle distinctions = harder
- Common trap answers: more = slightly harder
- Calculation/symbol manipulation complexity

Output ONLY a JSON object mapping question IDs (as strings) to difficulty ratings (integers 1-5):
{"123": 3, "124": 4, "125": 2}

Output ONLY the JSON, no other text, no markdown fences."""


def get_anthropic_client():
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
        raise RuntimeError("Set ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN")
    return Anthropic(**kwargs)


def format_question(q_id, q):
    """Format a question for the rating prompt."""
    options_str = ""
    if q.get("options"):
        options_str = "\n  Options: " + "; ".join(
            f"{o['label']}) {o['text'][:50]}" for o in q["options"][:6]
        )
    return f"""Q{q_id} ({q['subtype']}, {q['measure']}): {q['prompt'][:300]}{options_str}"""


def build_question_data(q):
    """Build a dict from a Question instance."""
    options = []
    for opt in QuestionOption.select().where(QuestionOption.question == q):
        options.append({
            "label": opt.option_label,
            "text": opt.option_text,
        })
    return {
        "subtype": q.subtype,
        "measure": q.measure,
        "prompt": q.prompt,
        "options": options,
    }


def rate_batch(client, model_id, questions_batch, max_retries=8):
    """Rate a batch of questions. Returns dict of qid -> rating."""
    user_parts = ["Rate the difficulty (1-5) of each of these GRE questions. Return JSON {id: rating}.\n"]
    for q_id, q_data in questions_batch:
        user_parts.append(format_question(q_id, q_data))
        user_parts.append("")

    user_prompt = "\n".join(user_parts)

    for attempt in range(max_retries):
        try:
            message = client.messages.create(
                model=model_id,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )

            text_parts = []
            for block in message.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
            raw = "".join(text_parts).strip()

            if raw.startswith("```"):
                lines = raw.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                raw = "\n".join(lines).strip()

            ratings = json.loads(raw)
            # Normalize keys: strip "Q" prefix if present, ensure string keys → int values
            normalized = {}
            for k, v in ratings.items():
                key = str(k).lstrip("Q").lstrip("q").strip()
                try:
                    normalized[key] = max(1, min(5, int(v)))
                except (TypeError, ValueError):
                    pass
            return normalized
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "rate limit" in err_str.lower() or "TOO_MANY_REQUEST" in err_str:
                if attempt == max_retries - 1:
                    raise
                wait = 30 * (attempt + 1)
                time.sleep(wait)
                continue
            if "JSON" in str(e) or "json" in str(e):
                # Parsing failed - try once more
                if attempt < 2:
                    time.sleep(2)
                    continue
            raise
    raise RuntimeError("Max retries exceeded")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--measure", choices=["verbal", "quant"], default=None)
    parser.add_argument("--only-default", action="store_true",
                        help="Only re-rate questions at default difficulty 3")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--model", default="anthropic.claude-haiku-4-5-20251001-v1:0")
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()

    init_db()
    db.connect(reuse_if_open=True)

    q = Question.select()
    if args.measure:
        q = q.where(Question.measure == args.measure)
    if args.only_default:
        q = q.where(Question.difficulty_target == 3)
    questions = list(q)
    if args.limit:
        questions = questions[:args.limit]

    print(f"Found {len(questions)} questions to rate")

    if not questions:
        return 0

    try:
        client = get_anthropic_client()
    except Exception as e:
        print(f"\nERROR: {e}")
        return 1

    print(f"Using model: {args.model}, batch size: {args.batch_size}\n")

    success = 0
    failed = 0
    start = time.time()

    # Process in batches
    for batch_start in range(0, len(questions), args.batch_size):
        batch_end = min(batch_start + args.batch_size, len(questions))
        batch = questions[batch_start:batch_end]
        batch_data = [(str(q.id), build_question_data(q)) for q in batch]

        try:
            ratings = rate_batch(client, args.model, batch_data)

            with db.atomic():
                for q in batch:
                    rating = ratings.get(str(q.id))
                    if rating:
                        q.difficulty_target = rating
                        q.save()
                        success += 1

            elapsed = time.time() - start
            rate = (batch_end) / elapsed if elapsed > 0 else 0
            sample_ratings = ",".join(str(ratings.get(str(q.id), "?")) for q in batch[:5])
            print(f"  [{batch_end}/{len(questions)}] ({rate:.2f}/s) — sample: {sample_ratings}...")

            if args.delay > 0:
                time.sleep(args.delay)
        except KeyboardInterrupt:
            print(f"\nInterrupted at batch {batch_start}")
            break
        except Exception as e:
            failed += len(batch)
            print(f"  [{batch_end}/{len(questions)}] BATCH FAILED: {e}")
            continue

    print(f"\nDone. {success} rated, {failed} failed.")
    print(f"Time: {time.time() - start:.1f}s")

    # Show new distribution
    from collections import Counter
    diffs = Counter()
    for q in Question.select():
        diffs[q.difficulty_target] += 1
    total = sum(diffs.values())
    print(f"\nNew difficulty distribution:")
    for d in sorted(diffs.keys()):
        print(f"  Level {d}: {diffs[d]} ({100*diffs[d]//total}%)")


if __name__ == "__main__":
    sys.exit(main() or 0)
