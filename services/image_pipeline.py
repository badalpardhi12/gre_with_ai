"""Glue between :mod:`extract_princeton`, :mod:`image_classifier`, and
:mod:`vision_render`.

For every extracted question:
  1. Collect every image reference (figure_refs, inline_gif_targets,
     answer_table_image, [img:...] placeholders in option text).
  2. Pull the bytes from the EPUB.
  3. Classify into a bucket via :mod:`services.image_classifier`.
  4. Vision-render inline_math + answer_table buckets via
     :mod:`services.vision_render`.
  5. Substitute results back into the question dict and stamp every
     surviving image with its ``kind`` field so the new validation gate
     ``G13_image_buckets_resolved`` passes.

Usage::

    from services.image_pipeline import resolve_question_images
    epub = zipfile.ZipFile(EPUB_PATH)
    resolve_question_images(question, epub_zip=epub)

The pipeline is idempotent — re-running on a question whose images have
already been resolved is a no-op (figure_refs already have ``kind``,
inline_gif_targets is empty, answer_table_image is None).
"""
from __future__ import annotations

import os
import re
import sys
import zipfile
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, "/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-a9405213")

from services.image_classifier import (
    BUCKET_ANSWER_TABLE, BUCKET_BULLET, BUCKET_CHART, BUCKET_DIAGRAM,
    BUCKET_INLINE_MATH, BUCKET_NUMERIC_BOX, BUCKET_UNKNOWN,
    classify_image, get_cache as get_classify_cache,
)
from services.vision_render import (
    apply_answer_table, get_render_cache, render_answer_table,
    render_inline_math, substitute_inline_math,
)


def _read_epub_image(zf: zipfile.ZipFile, filename: str) -> Optional[bytes]:
    if not filename:
        return None
    bare = filename.rsplit("/", 1)[-1]
    for path in ("OEBPS/images/" + bare, "images/" + bare,
                 "OEBPS/" + bare):
        try:
            return zf.read(path)
        except KeyError:
            continue
    return None


def _all_image_refs(question: Dict[str, Any]):
    """Yield every image reference attached to a question."""
    for fr in question.get("figure_refs") or []:
        yield {"role": "figure", "filename": fr.get("filename"),
               "width": fr.get("width"), "height": fr.get("height"),
               "html_class": None, "ref": fr}
    for ig in question.get("inline_gif_targets") or []:
        yield {"role": "inline_gif", "filename": ig.get("filename"),
               "width": ig.get("width"), "height": ig.get("height"),
               "html_class": "inline", "ref": ig}
    ati = question.get("answer_table_image")
    if ati:
        yield {"role": "answer_table_img", "filename": ati,
               "width": None, "height": None, "html_class": None,
               "ref": None}
    # Placeholders inside option text — filename only, no dimensions.
    for o in question.get("options") or []:
        for m in re.finditer(r"\[img:([^\]]+)\]", o.get("text", "")):
            yield {"role": "option_placeholder", "filename": m.group(1),
                   "width": None, "height": None,
                   "html_class": None, "ref": o}


def resolve_question_images(
    question: Dict[str, Any],
    *,
    epub_zip: zipfile.ZipFile,
    classify_cache=None,
    render_cache=None,
    client=None,
    enable_vision: bool = True,
    cross_check_inline: bool = False,
    cross_check_table: bool = False,
    use_form: str = "plain",
) -> Dict[str, Any]:
    """Run classify + render + substitute for one question.

    Returns a *report dict* describing what happened (one entry per
    image ref). The question dict is mutated in place.
    """
    classify_cache = classify_cache or get_classify_cache()
    render_cache = render_cache or get_render_cache()
    report = {"qst_id": question.get("qst_id"), "images": []}

    refs = list(_all_image_refs(question))
    rendered_inline_by_filename: Dict[str, Dict[str, Any]] = {}
    answer_table_render: Optional[Dict[str, Any]] = None

    # Classification context — helps Sonnet disambiguate.
    ctx_pieces = [
        question.get("subtype") or "",
        ("DI / data interp" if (question.get("base_slug") or "").startswith("cgd")
         or (question.get("base_slug") or "").startswith("figd") else ""),
    ]
    context = " | ".join(p for p in ctx_pieces if p)

    seen_files = set()  # avoid re-classifying the same filename twice per Q.
    for ref in refs:
        fname = ref["filename"]
        if not fname or fname in seen_files:
            continue
        seen_files.add(fname)
        image_bytes = _read_epub_image(epub_zip, fname)
        verdict = classify_image(
            filename=fname,
            image_bytes=image_bytes,
            width=ref.get("width"),
            height=ref.get("height"),
            html_class=ref.get("html_class"),
            context=context,
            cache=classify_cache,
            client=client,
            enable_vision=enable_vision,
        )
        bucket = verdict.get("bucket")
        report["images"].append({
            "filename": fname, "role": ref["role"],
            "bucket": bucket, "source": verdict.get("source"),
            "confidence": verdict.get("confidence"),
        })

        if bucket == BUCKET_INLINE_MATH and image_bytes is not None:
            rendered = render_inline_math(
                image_bytes, fname,
                client=client, cache=render_cache,
                cross_check_with_opus=cross_check_inline,
            )
            rendered_inline_by_filename[fname] = rendered
        elif bucket == BUCKET_ANSWER_TABLE and image_bytes is not None:
            expected = "tc" if (question.get("subtype") or "") == "tc" else (
                "se" if (question.get("subtype") or "") == "se" else None)
            answer_table_render = render_answer_table(
                image_bytes, fname,
                client=client, cache=render_cache,
                cross_check_with_opus=cross_check_table,
                expected_kind=expected,
            )

    # Apply substitutions.
    if rendered_inline_by_filename:
        substitute_inline_math(
            question,
            rendered_by_filename=rendered_inline_by_filename,
            use_form=use_form,
        )
        # ALSO drop any figure_refs whose filename rendered as inline math
        # (these slipped in because they weren't wrapped in <p
        # class="img_hang"> at parse time — file 351 / fraction GIFs, or
        # large stem-only equation GIFs that exceeded the inline-size
        # heuristic at parse time but Sonnet then classified as inline
        # math). For each one, append the rendered text to the prompt so
        # the equation isn't lost (the parser left no [img:...] hook for
        # ``substitute_inline_math`` to fill in).
        kept = []
        prompt_appendix_parts = []
        for fr in question.get("figure_refs") or []:
            fn = fr.get("filename")
            r = rendered_inline_by_filename.get(fn)
            if r and "error" not in r:
                rendered_text = r.get(use_form) or r.get("plain") or ""
                if rendered_text and "[img:" + fn + "]" not in (
                        question.get("prompt") or ""):
                    prompt_appendix_parts.append(rendered_text)
                continue
            kept.append(fr)
        question["figure_refs"] = kept
        if prompt_appendix_parts:
            existing = (question.get("prompt") or "").strip()
            appendix = "\n\n".join(prompt_appendix_parts).strip()
            if existing:
                question["prompt"] = existing + "\n\n" + appendix
            else:
                question["prompt"] = appendix

        # Demote inline_math classifications whose render returned
        # ``error: not_inline_math`` (Sonnet vision second-guessed the
        # deterministic rule). Those are real diagrams the deterministic
        # filter would otherwise silently drop. We rewrite the
        # classification report so the downstream filter keeps them.
        for entry in report["images"]:
            r = rendered_inline_by_filename.get(entry["filename"])
            if r and r.get("error") == "not_inline_math":
                entry["bucket"] = BUCKET_DIAGRAM
                entry["source"] = entry.get("source", "deterministic") + "+sonnet_demoted"
                # Make sure this filename is in figure_refs so the kind
                # stamping below catches it.
                existing = {fr.get("filename") for fr in question.get("figure_refs") or []}
                if entry["filename"] not in existing:
                    question.setdefault("figure_refs", []).append({
                        "filename": entry["filename"],
                        "src": "OEBPS/images/" + entry["filename"],
                    })

    # Promote option-placeholder diagrams/charts to a real figure_ref so
    # the renderer can show them. Some questions have one diagram per
    # option (e.g. "which of the following is the graph of y = -|-x|?"),
    # which the deterministic parser left as `[img:...]` in the option
    # text. We rewrite the placeholder to a markdown-style ``![diagram](...)``
    # link AND attach it to ``figure_refs`` with the appropriate kind.
    promoted_in_options = False
    for entry in report["images"]:
        if entry["role"] != "option_placeholder":
            continue
        bucket = entry["bucket"]
        # Anything that isn't inline_math / answer_table is a diagram or
        # chart that has to be kept as an image. Treat ``unknown``
        # conservatively as ``diagram``.
        if bucket in (BUCKET_INLINE_MATH, BUCKET_ANSWER_TABLE,
                      BUCKET_BULLET, BUCKET_NUMERIC_BOX):
            continue
        marker_kind = "chart" if bucket == BUCKET_CHART else "diagram"
        fname = entry["filename"]
        # Add to figure_refs (avoid duplicates).
        existing_fnames = {fr.get("filename") for fr in question.get("figure_refs") or []}
        if fname not in existing_fnames:
            question.setdefault("figure_refs", []).append({
                "filename": fname,
                "src": "OEBPS/images/" + fname,
                "kind": marker_kind,
                "attached_to": "option",
            })
        # Replace the placeholder text with a short marker the runtime
        # renderer (and the markdown sample) can spot.
        marker = "[" + marker_kind + ":" + fname + "]"
        new_opts = []
        for o in question.get("options") or []:
            txt = o.get("text", "")
            new_txt = txt.replace("[img:" + fname + "]", marker)
            new_o = dict(o)
            new_o["text"] = new_txt
            new_opts.append(new_o)
        question["options"] = new_opts
        promoted_in_options = True

    if answer_table_render is not None and "error" not in answer_table_render:
        apply_answer_table(question, answer_table_render)
        question["needs_vision"] = False

    # Stamp surviving figure_refs with their bucket as `kind`.
    classify_lookup = {im["filename"]: im for im in report["images"]}
    for fr in question.get("figure_refs") or []:
        fn = fr.get("filename")
        v = classify_lookup.get(fn)
        if v is None:
            continue
        bucket = v.get("bucket")
        # Drop refs to images that classify as inline_math, numeric_box,
        # bullet, or answer_table — they should never live in figure_refs.
        if bucket in (BUCKET_INLINE_MATH, BUCKET_NUMERIC_BOX,
                      BUCKET_BULLET, BUCKET_ANSWER_TABLE):
            continue
        if bucket == BUCKET_DIAGRAM:
            fr["kind"] = "diagram"
        elif bucket == BUCKET_CHART:
            fr["kind"] = "chart"
        else:
            fr["kind"] = "unknown"

    # Re-filter figure_refs to remove anything we dropped above.
    question["figure_refs"] = [
        fr for fr in question.get("figure_refs") or []
        if classify_lookup.get(fr.get("filename"), {}).get("bucket")
        not in (BUCKET_INLINE_MATH, BUCKET_NUMERIC_BOX,
                BUCKET_BULLET, BUCKET_ANSWER_TABLE)
    ]
    # Ensure surviving refs all have a kind.
    for fr in question.get("figure_refs") or []:
        if "kind" not in fr:
            fr["kind"] = "diagram"  # safe default for kept refs

    # Final report fields.
    report["unresolved_image_placeholders"] = [
        m.group(1)
        for o in question.get("options") or []
        for m in re.finditer(r"\[img:([^\]]+)\]", o.get("text", ""))
    ]
    return report


# ── Validation gate ──────────────────────────────────────────────────


def gate_image_buckets_resolved(question: Dict[str, Any]):
    """G13: every surviving image ref must have a recognised ``kind`` and
    no raw ``[img:...]`` placeholder may live in a stem or option."""
    # Surviving figure_refs: every one must declare diagram or chart.
    for fr in question.get("figure_refs") or []:
        kind = fr.get("kind")
        if kind not in ("diagram", "chart"):
            return False, "figure_kind_unresolved:" + str(kind)
    # No leftover inline_gif_targets — they should all have been rendered.
    if question.get("inline_gif_targets"):
        return False, "inline_gif_target_unrendered:" + str(
            len(question["inline_gif_targets"]))
    # No answer_table_image — should have been rendered into options.
    if question.get("answer_table_image"):
        return False, "answer_table_image_unrendered"
    # No `[img:...]` placeholders left in options or stem.
    blob = (question.get("prompt") or "")
    for o in question.get("options") or []:
        blob += "\n" + (o.get("text") or "")
    if "[img:" in blob:
        return False, "raw_img_placeholder"
    return True, "ok"
