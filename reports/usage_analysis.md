# RecallFlow usage analysis

> DEMO EVENT ANALYSIS — scripted, not user validation

Observation window: **[2026-10-26T00:00:00Z, 2026-10-27T00:00:00Z)** (UTC; start inclusive, end exclusive).

The label is supplied by the operator. Demo and pilot databases must remain separate. Counts describe surviving rows in one read-only SQLite transaction snapshot, not all historical behavior.

## Results

| Metric | Numerator | Denominator | Rate |
| --- | ---: | ---: | ---: |
| Save-operation success rate | 5 | 6 | 83.3% |
| Due-card action rate | 3 | 3 | 100.0% |
| Open-handoff success rate | 1 | 2 | 50.0% |

A zero denominator is unavailable (N/A), never a measured 0%.

## Metric definitions

1. **Save-operation success rate:** unique `(user_id, correlation_id)` operations with a first terminal `SYSTEM` `capture_succeeded` or `capture_failed` in the window; numerator is `capture_succeeded`. Repeated identical terminal outcomes count once. Conflicting outcomes or reused associations are excluded and flagged. A missing `USER` `capture_started` is reported as an instrumentation-coverage diagnostic; it does not erase a known terminal outcome.

2. **Due-card action rate:** unique `(user_id, resurfacing_event_id)` cards whose first valid `resurfacing_impression` (actor `CLIENT`) is in the window. Numerator is cards with at least one subsequent `USER` `open_attempt`, `later_confirmed`, or `dismissed` before the window ends. An unsuccessful Open attempt still demonstrates a user action. Undo does not erase the action. Multiple actions and refreshes never count a card twice; a reservation alone is not an impression.

3. **Open-handoff success rate:** unique `(user_id, correlation_id)` operations whose first `SYSTEM` terminal `source_opened` or `open_failed` is in the window, paired with an earlier or equal-time `USER` `open_attempt` for the same user, card, and item. Numerator is `source_opened`. Retries using the same operation correlation count once; a deliberate new attempt with a new correlation is a new operation. Attempts with no terminal result are excluded from the denominator and flagged.

**`source_opened` means safe-link checking completed and a navigation handoff was issued. It does not establish that the browser loaded the article, that anyone read it, or that learning occurred.**

## Capture instrumentation coverage

Earlier non-AI V1 builds defined capture event names but did not emit capture outcomes. The instrumented save route now records `capture_started` and a terminal `capture_succeeded` or `capture_failed` using an operation correlation ID. These new events cannot retroactively recover older uninstrumented submissions. If there are no terminal capture events in an observation window, the result is N/A, not 100%. Saved-item row counts cannot reconstruct rejected requests, duplicate saves, rollbacks, or retries. Success means the save operation completed; it does not imply successful article extraction. A completed save that returns an already-saved duplicate is a successful operation (`created=false`). Exact request retries that reuse the operation ID count once. Starts without a persisted terminal outcome remain incomplete, are flagged, and are excluded from the denominator. Requests rejected before the authenticated, CSRF-validated save operation and database outages that prevent event persistence are outside this event-based denominator. The analyzer reads actual terminal events only; it neither inserts missing events nor changes application logging.

## Extraction is a separate outcome

Surviving saved items whose `saved_at` is in the window: **4**. The following is their current `extraction_status` snapshot, not save-operation success and not necessarily the status at the historical event time.

| Extraction status | Saved items |
| --- | ---: |
| FAILED | 1 |
| INADEQUATE | 0 |
| PENDING | 0 |
| SUCCESS | 3 |
| UNKNOWN | 0 |
| UNSUPPORTED | 0 |

## Observed event counts

Counts below are valid, event-ID/idempotency-deduplicated rows inside the window. Metric-specific correlation and card deduplication is additional.

| Event | Rows |
| --- | ---: |
| `capture_failed` | 1 |
| `capture_started` | 6 |
| `capture_succeeded` | 5 |
| `dismiss_undone` | 1 |
| `dismissed` | 1 |
| `later_confirmed` | 1 |
| `open_attempt` | 2 |
| `open_failed` | 1 |
| `resurfacing_impression` | 3 |
| `source_opened` | 1 |

## Data quality

Checks use rows before the end boundary, including earlier history to identify the true first impression and pair completed operations. Rows at or after the end never affect this result. Invalid timestamps are excluded and flagged even when their window cannot be determined. Orphan or cross-user references and unsupported schemas are excluded. A repeated event ID or user-scoped idempotency key is collapsed; conflicting reuse is excluded.

| Diagnostic | Count |
| --- | ---: |
| No detected row-level anomalies | 0 |

## Interpretation and limits

- This is a fixed-window workflow analysis, not retention, revisit-habit, comprehension, or usability validation.
- Cards shown near the end have less follow-up time. This is not a uniform per-card 24-hour response rate; change the window explicitly for comparisons.
- Operations finishing after the cutoff and actions on cards first shown before the window are not in their respective denominators.
- Item/user hard deletion cascades related events. Historical counts are therefore subject to deletion/survivorship bias and cannot establish total traffic.
- Unsupported or missing instrumentation cannot be repaired by an analysis query. No failures are inferred from missing success events.
- Demo results show that the scripted workflow and measurement rules run. They are not real-user evidence or performance benchmarks.
- Outputs contain aggregate counts only; URLs, titles, notes, article snapshots, user/card/item IDs, codes, cookies, and event properties are not exported.

## Reproduce

```powershell
python scripts/analyze_usage.py --database <private-local-database> --start 2026-10-26T00:00:00Z --end 2026-10-27T00:00:00Z --data-kind demo --output-dir <report-directory>
```

Files: `usage_summary.csv`, `usage_metrics.svg`, and `usage_analysis.md`. Review aggregate pilot reports for disclosure before publishing; this script does not upload anything.
