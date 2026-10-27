"""Aggregate surviving RecallFlow events without importing or changing the app.

Run with --help. The input is opened with SQLite mode=ro and one read transaction.
Only aggregate counts leave the database; no identifiers or event properties are
written to the reports. This script is also importable for deterministic tests.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path
import sqlite3
import sys


UTC = timezone.utc
CAPTURE_TERMINALS = {"capture_succeeded", "capture_failed"}
OPEN_TERMINALS = {"source_opened", "open_failed"}
CARD_ACTIONS = {"open_attempt", "later_confirmed", "dismissed"}
KNOWN_EVENTS = CAPTURE_TERMINALS | OPEN_TERMINALS | CARD_ACTIONS | {
    "capture_started", "resurfacing_impression", "later_selected", "dismiss_undone",
    "pause_enabled", "pause_disabled", "no_action_recorded",
}
EXTRACTION_STATUSES = {"PENDING", "SUCCESS", "INADEQUATE", "FAILED", "UNSUPPORTED"}
OUTPUT_NAMES = ("usage_summary.csv", "usage_metrics.svg", "usage_analysis.md")


class AnalysisError(ValueError):
    """A safe, operator-readable error without private row values."""


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    """Require an explicit UTC offset for user-provided observation boundaries."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, AttributeError) as exc:
        raise AnalysisError("Use an ISO 8601 UTC time, for example 2026-10-26T00:00:00Z.") from exc


def _database_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # The application's UTCDateTime type persists naive UTC SQLite timestamps.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    numerator: int
    denominator: int

    @property
    def rate(self) -> float | None:
        return self.numerator / self.denominator if self.denominator else None


@dataclass(frozen=True)
class Event:
    id: str
    name: str
    at: datetime
    user: str
    correlation: str
    card: str
    item: str
    actor: str
    key: str

    def semantic_key(self) -> tuple[str, ...]:
        return (self.name, self.user, self.correlation, self.card, self.item, self.actor)


@dataclass
class Analysis:
    start: datetime
    end: datetime
    data_kind: str
    metrics: list[Metric]
    diagnostics: Counter[str] = field(default_factory=Counter)
    event_counts: Counter[str] = field(default_factory=Counter)
    extraction_counts: Counter[str] = field(default_factory=Counter)
    saved_items_in_window: int = 0


def _require_columns(connection: sqlite3.Connection, table: str, required: set[str]) -> None:
    # All names are fixed constants in this module, never user input.
    actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if not required <= actual:
        raise AnalysisError(f"Input does not contain the required RecallFlow {table} schema.")


def _load_snapshot(connection: sqlite3.Connection) -> tuple[list[sqlite3.Row], dict, dict, set]:
    _require_columns(connection, "action_events", {
        "id", "event_name", "schema_version", "occurred_at", "user_id", "correlation_id",
        "resurfacing_event_id", "item_id", "actor", "idempotency_key",
    })
    _require_columns(connection, "resurfacing_events", {"id", "user_id", "saved_item_id"})
    _require_columns(connection, "saved_items", {"id", "user_id", "saved_at", "extraction_status"})
    _require_columns(connection, "users", {"id"})
    actions = connection.execute(
        "SELECT id,event_name,schema_version,occurred_at,user_id,correlation_id,"
        "resurfacing_event_id,item_id,actor,idempotency_key FROM action_events"
    ).fetchall()
    cards = {row["id"]: (row["user_id"], row["saved_item_id"]) for row in connection.execute(
        "SELECT id,user_id,saved_item_id FROM resurfacing_events"
    )}
    items = {row["id"]: row for row in connection.execute(
        "SELECT id,user_id,saved_at,extraction_status FROM saved_items"
    )}
    users = {row[0] for row in connection.execute("SELECT id FROM users")}
    return actions, cards, items, users


def _events(rows: list[sqlite3.Row], cards: dict, items: dict, users: set,
            end: datetime, diagnostics: Counter) -> list[Event]:
    by_id: dict[str, list[Event]] = defaultdict(list)
    for row in rows:
        try:
            at = _database_time(row["occurred_at"])
        except (ValueError, TypeError, OverflowError):
            diagnostics["invalid_event_timestamps"] += 1
            continue
        if at >= end:
            continue
        if row["schema_version"] != 1:
            diagnostics["unsupported_event_schema_rows"] += 1
            continue
        if row["event_name"] not in KNOWN_EVENTS:
            diagnostics["unknown_event_name_rows"] += 1
            continue
        if not row["id"] or not row["idempotency_key"] or row["user_id"] not in users:
            diagnostics["invalid_identity_rows"] += 1
            continue
        card, item = row["resurfacing_event_id"], row["item_id"]
        if row["event_name"] in CARD_ACTIONS | OPEN_TERMINALS | {"resurfacing_impression"} and (not card or not item):
            diagnostics["missing_card_association_rows"] += 1
            continue
        if item and (item not in items or items[item]["user_id"] != row["user_id"]):
            diagnostics["orphan_or_cross_user_item_rows"] += 1
            continue
        if card and (card not in cards or cards[card][0] != row["user_id"]
                     or (item and cards[card][1] != item)):
            diagnostics["orphan_or_cross_user_card_rows"] += 1
            continue
        event = Event(row["id"], row["event_name"], at, row["user_id"],
                      row["correlation_id"] or "", card or "", item or "",
                      row["actor"], row["idempotency_key"])
        by_id[event.id].append(event)
    distinct_ids: list[Event] = []
    for same_id in by_id.values():
        if len({(e.semantic_key(), e.key) for e in same_id}) > 1:
            diagnostics["conflicting_event_id_groups"] += 1
        else:
            diagnostics["duplicate_event_id_rows"] += len(same_id) - 1
            distinct_ids.append(min(same_id, key=lambda e: (e.at, e.id)))
    by_key: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for event in distinct_ids:
        by_key[event.user, event.key].append(event)
    result = []
    for same_key in by_key.values():
        if len({e.semantic_key() for e in same_key}) > 1:
            diagnostics["conflicting_idempotency_key_groups"] += 1
        else:
            diagnostics["duplicate_idempotency_key_rows"] += len(same_key) - 1
            result.append(min(same_key, key=lambda e: (e.at, e.id)))
    return sorted(result, key=lambda e: (e.at, e.id))


def _operation_metric(events: list[Event], *, start: datetime, terminal_names: set[str],
                      success: str, name: str, label: str, diagnostics: Counter,
                      require_open_attempt: bool = False) -> Metric:
    groups: dict[tuple[str, str], list[Event]] = defaultdict(list)
    attempts: dict[tuple[str, str], list[Event]] = defaultdict(list)
    start_name = "open_attempt" if require_open_attempt else "capture_started"
    for event in events:
        if event.name == start_name and event.actor == "USER" and event.correlation:
            attempts[event.user, event.correlation].append(event)
        if event.name not in terminal_names:
            continue
        if event.actor != "SYSTEM":
            diagnostics[f"{name}_invalid_terminal_actor_rows"] += 1
            continue
        if not event.correlation:
            diagnostics[f"{name}_missing_correlation_rows"] += 1
            continue
        groups[event.user, event.correlation].append(event)
    good = total = 0
    for key, terminals in groups.items():
        first = min(terminals, key=lambda e: (e.at, e.id))
        if first.at < start:
            continue
        # Conflicting terminal results or association reuse are not two operations.
        if len({(e.name, e.card, e.item) for e in terminals}) > 1:
            diagnostics[f"{name}_conflicting_terminal_groups"] += 1
            continue
        if require_open_attempt:
            paired = [e for e in attempts.get(key, []) if e.actor == "USER"
                      and e.card == first.card and e.item == first.item and e.at <= first.at]
            if not first.card or not paired:
                diagnostics["open_handoff_orphan_terminal_groups"] += 1
                continue
        elif not any(e.at <= first.at for e in attempts.get(key, [])):
            # Older datasets can retain completed operations without starts.
            # Preserve the terminal-outcome denominator but expose coverage.
            diagnostics["capture_terminal_groups_without_start"] += 1
        diagnostics[f"{name}_duplicate_terminal_rows"] += len(terminals) - 1
        total += 1
        good += first.name == success
    for key, group in attempts.items():
        if any(start <= e.at for e in group) and key not in groups:
            missing_name = ("open_attempts_without_terminal_groups" if require_open_attempt
                            else "capture_starts_without_terminal_groups")
            diagnostics[missing_name] += 1
    return Metric(name, label, good, total)


def _card_metric(events: list[Event], start: datetime, diagnostics: Counter) -> Metric:
    impressions: dict[tuple[str, str], Event] = {}
    for event in events:
        if event.name != "resurfacing_impression":
            continue
        if not event.card or event.actor != "CLIENT":
            diagnostics["invalid_impression_rows"] += 1
            continue
        key = event.user, event.card
        if key in impressions:
            diagnostics["duplicate_card_impression_rows"] += 1
        else:
            impressions[key] = event
    cohort = {key: event for key, event in impressions.items() if event.at >= start}
    acted: set[tuple[str, str]] = set()
    for event in events:
        if event.name not in CARD_ACTIONS or event.actor != "USER" or event.at < start:
            continue
        key = event.user, event.card
        impression = cohort.get(key)
        if impression is None:
            diagnostics["actions_outside_impression_cohort_rows"] += 1
        elif event.at < impression.at:
            diagnostics["actions_before_impression_rows"] += 1
        else:
            acted.add(key)
    return Metric("card_action_rate", "Due-card action rate", len(acted), len(cohort))


def analyze_database(database: str | Path, *, start: datetime, end: datetime,
                     data_kind: str) -> Analysis:
    """Read one consistent surviving-data snapshot; never initialize a database."""
    if data_kind not in {"demo", "pilot"}:
        raise AnalysisError("Choose an explicit data kind: demo or pilot.")
    if start.tzinfo is None or end.tzinfo is None:
        raise AnalysisError("Observation boundaries must be timezone-aware.")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if start >= end:
        raise AnalysisError("Observation start must be earlier than end.")
    database = Path(database).resolve()
    if not database.is_file():
        raise AnalysisError("The input database must be an existing file.")
    diagnostics: Counter[str] = Counter()
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            rows, cards, items, users = _load_snapshot(connection)
    except sqlite3.Error as exc:
        raise AnalysisError("Could not read a valid, available RecallFlow SQLite database.") from exc
    events = _events(rows, cards, items, users, end, diagnostics)
    metrics = [
        _operation_metric(events, start=start, terminal_names=CAPTURE_TERMINALS,
                          success="capture_succeeded", name="capture_success_rate",
                          label="Save-operation success rate", diagnostics=diagnostics),
        _card_metric(events, start, diagnostics),
        _operation_metric(events, start=start, terminal_names=OPEN_TERMINALS,
                          success="source_opened", name="open_handoff_success_rate",
                          label="Open-handoff success rate", diagnostics=diagnostics,
                          require_open_attempt=True),
    ]
    result = Analysis(start, end, data_kind, metrics, diagnostics,
                      Counter(e.name for e in events if e.at >= start))
    for item in items.values():
        try:
            saved_at = _database_time(item["saved_at"])
        except (ValueError, TypeError, OverflowError):
            diagnostics["invalid_saved_item_timestamps"] += 1
            continue
        if start <= saved_at < end:
            result.saved_items_in_window += 1
            status = item["extraction_status"]
            result.extraction_counts[status if status in EXTRACTION_STATUSES else "UNKNOWN"] += 1
    return result


def _rate_text(metric: Metric) -> str:
    return "N/A" if metric.rate is None else f"{100 * metric.rate:.1f}%"


def _report(result: Analysis) -> str:
    kind_label = "DEMO EVENT ANALYSIS — scripted, not user validation" if result.data_kind == "demo" else "PILOT EVENT ANALYSIS — observational, not causal"
    lines = ["# RecallFlow usage analysis", "", f"> {kind_label}", "",
             f"Observation window: **[{utc_text(result.start)}, {utc_text(result.end)})** (UTC; start inclusive, end exclusive).", "",
             "The label is supplied by the operator. Demo and pilot databases must remain separate. "
             "Counts describe surviving rows in one read-only SQLite transaction snapshot, not all historical behavior.", "",
             "## Results", "", "| Metric | Numerator | Denominator | Rate |",
             "| --- | ---: | ---: | ---: |"]
    lines += [f"| {m.label} | {m.numerator} | {m.denominator} | {_rate_text(m)} |" for m in result.metrics]
    lines += ["", "A zero denominator is unavailable (N/A), never a measured 0%.", "",
              "## Metric definitions", "",
              "1. **Save-operation success rate:** unique `(user_id, correlation_id)` operations with a first terminal "
              "`SYSTEM` `capture_succeeded` or `capture_failed` in the window; numerator is `capture_succeeded`. "
              "Repeated identical terminal outcomes count once. Conflicting outcomes or reused associations are excluded and flagged. "
              "A missing `USER` `capture_started` is reported as an instrumentation-coverage diagnostic; it does not erase a known terminal outcome.", "",
              "2. **Due-card action rate:** unique `(user_id, resurfacing_event_id)` cards whose first valid "
              "`resurfacing_impression` (actor `CLIENT`) is in the window. Numerator is cards with at least one "
              "subsequent `USER` `open_attempt`, `later_confirmed`, or `dismissed` before the window ends. "
              "An unsuccessful Open attempt still demonstrates a user action. Undo does not erase the action. "
              "Multiple actions and refreshes never count a card twice; a reservation alone is not an impression.", "",
              "3. **Open-handoff success rate:** unique `(user_id, correlation_id)` operations whose first "
              "`SYSTEM` terminal `source_opened` or `open_failed` is in the window, paired with an earlier or equal-time "
              "`USER` `open_attempt` for the same user, card, and item. Numerator is `source_opened`. "
              "Retries using the same operation correlation count once; a deliberate new attempt with a new "
              "correlation is a new operation. Attempts with no terminal result are excluded from the denominator and flagged.", "",
              "**`source_opened` means safe-link checking completed and a navigation handoff was issued. "
              "It does not establish that the browser loaded the article, that anyone read it, or that learning occurred.**", "",
              "## Capture instrumentation coverage", "",
              "Earlier non-AI V1 builds defined capture event names but did not emit capture outcomes. "
              "The instrumented save route now records `capture_started` and a terminal "
              "`capture_succeeded` or `capture_failed` using an operation correlation ID. "
              "These new events cannot retroactively recover older uninstrumented submissions. "
              "If there are no terminal capture events in an observation window, the result is N/A, not 100%. "
              "Saved-item row counts cannot reconstruct rejected requests, duplicate saves, rollbacks, or retries. "
              "Success means the save operation completed; it does not imply successful article extraction. "
              "A completed save that returns an already-saved duplicate is a successful operation (`created=false`). "
              "Exact request retries that reuse the operation ID count once. "
              "Starts without a persisted terminal outcome remain incomplete, are flagged, and are excluded from the denominator. "
              "Requests rejected before the authenticated, CSRF-validated save operation and database outages "
              "that prevent event persistence are outside this event-based denominator. "
              "The analyzer reads actual terminal events only; it neither inserts missing events nor changes application logging.", "",
              "## Extraction is a separate outcome", "",
              f"Surviving saved items whose `saved_at` is in the window: **{result.saved_items_in_window}**. "
              "The following is their current `extraction_status` snapshot, not save-operation success and "
              "not necessarily the status at the historical event time.", "",
              "| Extraction status | Saved items |", "| --- | ---: |"]
    lines += [f"| {status} | {result.extraction_counts.get(status, 0)} |" for status in sorted(EXTRACTION_STATUSES | {"UNKNOWN"})]
    lines += ["", "## Observed event counts", "",
              "Counts below are valid, event-ID/idempotency-deduplicated rows inside the window. "
              "Metric-specific correlation and card deduplication is additional.", "",
              "| Event | Rows |", "| --- | ---: |"]
    lines += [f"| `{name}` | {result.event_counts[name]} |" for name in sorted(result.event_counts)] or ["| No observed events | 0 |"]
    lines += ["", "## Data quality", "",
              "Checks use rows before the end boundary, including earlier history to identify the true first "
              "impression and pair completed operations. Rows at or after the end never affect this result. "
              "Invalid timestamps are excluded and flagged even when their window cannot be determined. "
              "Orphan or cross-user references and unsupported schemas are excluded. "
              "A repeated event ID or user-scoped idempotency key is collapsed; conflicting reuse is excluded.", "",
              "| Diagnostic | Count |", "| --- | ---: |"]
    nonzero = {key: value for key, value in result.diagnostics.items() if value}
    lines += [f"| `{name}` | {count} |" for name, count in sorted(nonzero.items())] or ["| No detected row-level anomalies | 0 |"]
    lines += ["", "## Interpretation and limits", "",
              "- This is a fixed-window workflow analysis, not retention, revisit-habit, comprehension, or usability validation.",
              "- Cards shown near the end have less follow-up time. This is not a uniform per-card 24-hour response rate; change the window explicitly for comparisons.",
              "- Operations finishing after the cutoff and actions on cards first shown before the window are not in their respective denominators.",
              "- Item/user hard deletion cascades related events. Historical counts are therefore subject to deletion/survivorship bias and cannot establish total traffic.",
              "- Unsupported or missing instrumentation cannot be repaired by an analysis query. No failures are inferred from missing success events.",
              "- Demo results show that the scripted workflow and measurement rules run. They are not real-user evidence or performance benchmarks.",
              "- Outputs contain aggregate counts only; URLs, titles, notes, article snapshots, user/card/item IDs, codes, cookies, and event properties are not exported.", "",
              "## Reproduce", "",
              "```powershell", "python scripts/analyze_usage.py --database <private-local-database> "
              f'--start {utc_text(result.start)} --end {utc_text(result.end)} '
              f"--data-kind {result.data_kind} --output-dir <report-directory>", "```", "",
              "Files: `usage_summary.csv`, `usage_metrics.svg`, and `usage_analysis.md`. "
              "Review aggregate pilot reports for disclosure before publishing; this script does not upload anything.", ""]
    return "\n".join(lines)


def _chart(result: Analysis) -> str:
    title = "RecallFlow — demo events" if result.data_kind == "demo" else "RecallFlow — pilot events"
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1040" height="430" viewBox="0 0 1040 430" role="img" aria-labelledby="title desc">',
             f'<title id="title">{escape(title)}</title>',
             '<desc id="desc">Three aggregate workflow rates. N/A indicates no measurable denominator. Source opened is a navigation handoff, not reading. Historical uninstrumented requests cannot be reconstructed.</desc>',
             '<rect width="1040" height="430" fill="#f5f7fb"/>',
             '<g font-family="Arial, sans-serif" fill="#172033">',
             f'<text x="32" y="42" font-size="25" font-weight="700">{escape(title)}</text>',
             f'<text x="32" y="70" font-size="13">{escape(utc_text(result.start))} ≤ time &lt; {escape(utc_text(result.end))}</text>']
    for i, metric in enumerate(result.metrics):
        y = 115 + i * 80
        parts += [f'<text x="32" y="{y}" font-size="17">{escape(metric.label)}</text>',
                  f'<rect x="330" y="{y-20}" width="480" height="28" rx="6" fill="#dbe3ee"/>']
        if metric.rate is not None:
            parts.append(f'<rect x="330" y="{y-20}" width="{480*metric.rate:.2f}" height="28" rx="6" fill="#2765cc"/>')
        parts.append(f'<text x="830" y="{y}" font-size="17">{_rate_text(metric)} ({metric.numerator}/{metric.denominator})</text>')
    parts += ['<text x="32" y="360" font-size="14">N/A = no measurable denominator. Historical uninstrumented requests cannot be reconstructed.</text>',
              '<text x="32" y="385" font-size="14">Handoff ≠ reading. Demo events ≠ user validation. See the report for definitions and limitations.</text>',
              '</g></svg>']
    return "\n".join(parts) + "\n"


def write_reports(result: Analysis, output_dir: str | Path, *, database: str | Path) -> dict[str, Path]:
    destination = Path(output_dir).resolve()
    database = Path(database).resolve()
    targets = {name: destination / name for name in OUTPUT_NAMES}
    # Never overwrite the input, its SQLite journal sidecars, or symlinked files.
    protected = {database, Path(str(database) + "-wal"), Path(str(database) + "-shm"), Path(str(database) + "-journal")}
    for target in targets.values():
        if (target.is_symlink() or target.resolve() in protected
                or (target.exists() and database.exists() and target.samefile(database))):
            raise AnalysisError("Report destination would overwrite a database or symlink target.")
    if destination.exists() and not destination.is_dir():
        raise AnalysisError("Report output must be a directory.")
    destination.mkdir(parents=True, exist_ok=True)
    with targets["usage_summary.csv"].open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["metric", "numerator", "denominator", "rate_percent", "data_kind", "window_start_utc", "window_end_utc"])
        for metric in result.metrics:
            writer.writerow([metric.name, metric.numerator, metric.denominator,
                             "N/A" if metric.rate is None else f"{100*metric.rate:.4f}",
                             result.data_kind, utc_text(result.start), utc_text(result.end)])
    targets["usage_metrics.svg"].write_text(_chart(result), encoding="utf-8")
    targets["usage_analysis.md"].write_text(_report(result), encoding="utf-8")
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="Existing private SQLite file (read-only).")
    parser.add_argument("--start", required=True, help="Inclusive ISO 8601 UTC time with Z or +00:00.")
    parser.add_argument("--end", required=True, help="Exclusive ISO 8601 UTC time with Z or +00:00.")
    parser.add_argument("--data-kind", required=True, choices=("demo", "pilot"))
    parser.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = analyze_database(arguments.database, start=parse_utc(arguments.start),
                                  end=parse_utc(arguments.end), data_kind=arguments.data_kind)
        paths = write_reports(result, arguments.output_dir, database=arguments.database)
    except (AnalysisError, OSError) as exc:
        print(f"Analysis failed: {exc}", file=sys.stderr)
        return 2
    for metric in result.metrics:
        print(f"{metric.name}: {_rate_text(metric)} ({metric.numerator}/{metric.denominator})")
    print(f"Wrote {len(paths)} aggregate report files; input database was read-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
