#!/usr/bin/env python3
"""
WS-A helper: reconstruct correct option sets for option-grafted mcq_multi items.

For each candidate qid, the stored ``questionoption`` rows were grafted from a
neighboring question (see scripts/audit_option_graft.py). The item's prompt,
explanation, and immutable ``provenance_json.judge_result`` are internally
consistent, so the ORIGINAL option set + correctness is recoverable. This
script asks the LLM (the project's only LLM backend, OpenRouter via
services.llm_service) to reconstruct it, then records both the proposal and a
deterministic self-check so a human can bake a verified static decision list
into migration 039.

This NEVER writes to any DB. Output: data/audits/option_repair_proposals_<date>.json

Usage:
    venv/bin/python scripts/reconstruct_grafted_options.py [--qids 5378,5384,...]
"""
import sys, os, re, json, argparse, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3
from services.llm_service import llm_service

DB = "data/gre_mock.db"

# High-confidence mcq_multi graft candidates from audit_option_graft.py
# (manifest 2026-06-01). 5394 included as a control — expected verdict: native
# owner, options already correct, no repair.
DEFAULT_QIDS = [5374, 5375, 5376, 5377, 5378, 5380, 5381, 5382,
                5384, 5386, 5388, 5389, 5394, 3863, 4808]

SYS = (
    "You are a meticulous GRE Quantitative item editor. You repair items whose "
    "stored answer choices were corrupted (grafted from a different question), "
    "using ONLY the item's own prompt, explanation, and generation-time judge "
    "notes as ground truth. You never invent facts not entailed by those. "
    "Return ONLY a single JSON object, no prose, no markdown fences."
)

USER_TMPL = """Item qid {qid} ({subtype}). Its stored options are suspected to be GRAFTED from another question.

PROMPT:
{prompt}

EXPLANATION (ground truth for the math + the correct answer set):
{explanation}

GENERATION-TIME JUDGE NOTES (provenance; enumerates the ORIGINAL correct and wrong option values):
{provenance}

CURRENTLY-STORED OPTIONS (likely grafted / wrong — do not trust):
{current_options}

TASK: Reconstruct the option set this item was authored with, as entailed by the PROMPT + EXPLANATION + JUDGE NOTES.
- Produce the full list of answer choices (for "indicate all" mcq_multi, typically 5-8 choices: every value the explanation/judge marks correct PLUS the plausible wrong values it discusses).
- Mark is_correct strictly per the EXPLANATION's stated correct set (NOT per the current stored flags).
- Each option text should be a clean GRE-style choice (a number, expression, or short statement) matching the kind the prompt asks for.

Return JSON exactly:
{{
  "qid": {qid},
  "is_grafted": true/false,          // false if the stored options actually match the prompt+explanation (no repair needed)
  "reconstructable": true/false,     // false if the original options cannot be confidently recovered -> recommend retire
  "options": [{{"text": "...", "is_correct": true/false}}, ...],
  "correct_count": <int>,
  "notes": "<one line: how you derived this / why retire>"
}}
"""


def robust_json(raw):
    t = raw.strip()
    if t.startswith("```"):
        t = "\n".join(l for l in t.split("\n") if not l.strip().startswith("```"))
    # grab the outermost {...}
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    return json.loads(t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--qids", default="")
    args = ap.parse_args()
    qids = ([int(x) for x in args.qids.split(",") if x.strip()]
            if args.qids else DEFAULT_QIDS)

    conn = sqlite3.connect(DB)
    proposals = []
    for qid in qids:
        row = conn.execute(
            "SELECT subtype, prompt, explanation, provenance_json FROM question WHERE id=?",
            (qid,)).fetchone()
        if not row:
            proposals.append({"qid": qid, "error": "not found"})
            continue
        subtype, prompt, expl, pj = row
        opts = conn.execute(
            "SELECT option_label, option_text, is_correct FROM questionoption "
            "WHERE question_id=? ORDER BY option_label", (qid,)).fetchall()
        cur = "; ".join(f"{l}={t}{'(correct)' if ic else ''}" for l, t, ic in opts)
        prov = ""
        if pj:
            try:
                jr = json.loads(pj).get("judge_result", {})
                prov = json.dumps(jr)[:2000]
            except Exception:
                prov = pj[:2000]
        user = USER_TMPL.format(qid=qid, subtype=subtype, prompt=prompt,
                                explanation=expl, provenance=prov or "(none)",
                                current_options=cur)
        try:
            raw = llm_service.generate(SYS, user, max_tokens=1200)
            prop = robust_json(raw)
        except Exception as e:
            prop = {"qid": qid, "error": f"llm/parse failed: {e}"}
        prop["_current_options"] = cur
        prop["_prompt"] = (prompt or "")[:200]
        proposals.append(prop)
        print(f"q{qid}: grafted={prop.get('is_grafted')} "
              f"reconstructable={prop.get('reconstructable')} "
              f"n_opts={len(prop.get('options', []))} correct={prop.get('correct_count')}")
    conn.close()

    os.makedirs("data/audits", exist_ok=True)
    out = f"data/audits/option_repair_proposals_{datetime.date.today().isoformat()}.json"
    with open(out, "w") as f:
        json.dump({"generated": datetime.datetime.now().isoformat(timespec="seconds"),
                   "proposals": proposals}, f, indent=2)
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
