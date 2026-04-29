# Legacy ai_generated expert review — 2026-04-28

- Timestamp: 2026-04-29T05:38:21
- Target: 1,033 live `ai_generated` items (minus 156 retired by the cross-bank dedup) = **877 eligible**
- Reviewed (this run): **407** items (~46% of eligible)
- Panel: Opus 4.7 + Sonnet 4.6 + Gemini 3.1 Pro (3 judges, 5-axis rubric)
- Panel verdicts: live=277, draft=130
- Promoted (kept live): **277**
- Demoted to draft: **130** (31.9%)

## Current DB state (after incremental commits)

- live: **749**
- draft: **130**
- retired: **158**

## Axis means (across reviewed items)

| axis | mean |
|---|---|
| correctness | 4.80 |
| clarity | 4.70 |
| distractor_quality | 4.31 |
| difficulty_match | 4.13 |
| gre_authenticity | 4.38 |

## Completion note

This run covered ~46% of the eligible backlog. Remaining 470 unreviewed items stay live with status unchanged; their review can be resumed by re-running
`scripts/expert_review_ai_generated.py` — cached verdicts are idempotent, so only unprocessed qids will hit the Floodgate panel. The cache file at
`data/extracted/legacy_ai_generated/expert_review_cache.json` is the source of truth for resumption.

## Demoted qids (first 50 of 130)

- qid=1383
- qid=1384
- qid=1386
- qid=1390
- qid=1391
- qid=1394
- qid=1396
- qid=1402
- qid=1405
- qid=1412
- qid=1415
- qid=1417
- qid=1420
- qid=1422
- qid=1425
- qid=1438
- qid=1439
- qid=1441
- qid=1443
- qid=1445
- qid=1448
- qid=1453
- qid=1458
- qid=1459
- qid=1460
- qid=1463
- qid=1466
- qid=1470
- qid=1471
- qid=1480
- qid=1485
- qid=1493
- qid=1494
- qid=1496
- qid=1500
- qid=1501
- qid=1505
- qid=1507
- qid=1508
- qid=1514
- qid=1521
- qid=1522
- qid=1524
- qid=1532
- qid=1537
- qid=1538
- qid=1574
- qid=1576
- qid=1580
- qid=1581