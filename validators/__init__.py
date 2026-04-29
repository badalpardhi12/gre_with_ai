"""Validators package — currently houses the Kaplan extraction gates.

Per `.claude/plans/kaplan-extraction.md` Section 5, gates are returned
as `(severity, kind, detail)` tuples. Severities:

  - "block": persistence-time gate. Item must NOT be inserted live;
             held in `status='draft'` and dumped to a rejects log.
  - "warn":  persisted as draft and flagged in the audit.
  - "info":  logged only.
"""
