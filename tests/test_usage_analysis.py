"""Analytics tests use isolated SQLite fixtures, never the pilot database."""

from datetime import datetime, timezone
import csv
from hashlib import sha256
import sqlite3
import subprocess
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from scripts.analyze_usage import (
    AnalysisError, analyze_database, main, parse_utc, write_reports,
)


START = datetime(2026, 10, 26, tzinfo=timezone.utc)
END = datetime(2026, 10, 27, tzinfo=timezone.utc)


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "private usage.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE users (id TEXT PRIMARY KEY);
        CREATE TABLE saved_items (id TEXT PRIMARY KEY, user_id TEXT, saved_at TEXT,
            extraction_status TEXT, source_url TEXT, title TEXT, source_text TEXT);
        CREATE TABLE resurfacing_events (id TEXT PRIMARY KEY, user_id TEXT, saved_item_id TEXT);
        CREATE TABLE action_events (id TEXT, event_name TEXT, schema_version INTEGER,
            occurred_at TEXT, user_id TEXT, correlation_id TEXT, resurfacing_event_id TEXT,
            item_id TEXT, actor TEXT, idempotency_key TEXT, properties TEXT);
        INSERT INTO users VALUES ('private-user-one'), ('private-user-two');
    """)
    for i in range(1, 9):
        user = "private-user-one" if i < 7 else "private-user-two"
        connection.execute("INSERT INTO saved_items VALUES (?,?,?,?,?,?,?)", (
            f"private-item-{i}", user, "2026-10-26 00:00:00.000000",
            "SUCCESS" if i < 5 else "FAILED", "https://private.example/sensitive?q=secret",
            "PRIVATE_TITLE", "PRIVATE_SOURCE_SNAPSHOT",
        ))
        connection.execute("INSERT INTO resurfacing_events VALUES (?,?,?)", (
            f"private-card-{i}", user, f"private-item-{i}",
        ))
    connection.commit()
    counter = 0

    def event(name, *, when="2026-10-26T01:00:00Z", user="private-user-one", corr=None,
              card=None, item=None, actor=None, key=None, row_id=None, schema=1):
        nonlocal counter
        counter += 1
        row_id = row_id or f"event-{counter}"
        if card is not None and item is None:
            item = card.replace("card", "item")
        actor = actor or ("CLIENT" if name == "resurfacing_impression" else
                          "USER" if name in {"capture_started", "open_attempt", "later_confirmed", "dismissed", "dismiss_undone"} else "SYSTEM")
        connection.execute("INSERT INTO action_events VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            row_id, name, schema, when, user, corr if corr is not None else f"correlation-{counter}",
            card, item, actor, key or f"key-{counter}",
            '{"secret":"PRIVATE_EVENT_PROPERTY","cookie":"PRIVATE_COOKIE"}',
        ))
        connection.commit()
        return row_id

    yield path, connection, event
    connection.close()


def analyze(path, **kwargs):
    return analyze_database(path, start=START, end=END, data_kind="demo", **kwargs)


def metric(result, name):
    return next(m for m in result.metrics if m.name == name)


def test_completion_cohort_deduplicates_and_separates_capture_from_extraction(dataset):
    path, _, event = dataset
    event("capture_started", when="2026-10-25T23:59:59Z", corr="save-one")
    event("capture_succeeded", corr="save-one", item="private-item-1")
    event("capture_succeeded", corr="save-one", item="private-item-1", when="2026-10-26T02:00:00Z")
    event("capture_failed", corr="save-two")
    # Same operation correlation for a different user is a separate operation.
    event("capture_succeeded", corr="save-one", user="private-user-two", item="private-item-7")
    result = analyze(path)
    capture = metric(result, "capture_success_rate")
    assert (capture.numerator, capture.denominator, capture.rate) == (2, 3, 2 / 3)
    assert result.extraction_counts == {"SUCCESS": 4, "FAILED": 4}
    assert result.diagnostics["capture_success_rate_duplicate_terminal_rows"] == 1


def test_first_terminal_start_inclusive_end_exclusive_and_history_dedup(dataset):
    path, _, event = dataset
    event("capture_succeeded", corr="before", when="2026-10-25T23:59:59Z")
    event("capture_succeeded", corr="before", when="2026-10-26T04:00:00Z")
    event("capture_succeeded", corr="start", when="2026-10-26T00:00:00Z")
    event("capture_failed", corr="end", when="2026-10-27T00:00:00Z")
    event("capture_failed", corr="after", when="2026-10-27T00:00:01Z")
    result = analyze(path)
    assert metric(result, "capture_success_rate").denominator == 1
    assert metric(result, "capture_success_rate").numerator == 1


def test_card_actions_include_failed_open_and_count_once_after_undo(dataset):
    path, _, event = dataset
    for i in range(1, 5):
        event("resurfacing_impression", card=f"private-card-{i}")
    event("resurfacing_impression", card="private-card-1", when="2026-10-26T02:00:00Z")
    event("open_attempt", corr="failed-open", card="private-card-1", when="2026-10-26T03:00:00Z")
    event("open_failed", corr="failed-open", card="private-card-1", when="2026-10-26T03:00:01Z")
    event("later_confirmed", card="private-card-2", when="2026-10-26T03:00:00Z")
    event("dismissed", card="private-card-3", when="2026-10-26T03:00:00Z")
    event("dismiss_undone", card="private-card-3", when="2026-10-26T03:00:05Z")
    event("dismissed", card="private-card-3", when="2026-10-26T03:00:10Z")
    result = analyze(path)
    card_metric = metric(result, "card_action_rate")
    assert (card_metric.numerator, card_metric.denominator, card_metric.rate) == (3, 4, .75)
    assert result.diagnostics["duplicate_card_impression_rows"] == 1


def test_card_cohort_requires_impression_not_reservation_and_action_after_it(dataset):
    path, _, event = dataset
    event("resurfacing_impression", card="private-card-1", when="2026-10-25T23:00:00Z")
    event("resurfacing_impression", card="private-card-1", when="2026-10-26T02:00:00Z")
    event("later_confirmed", card="private-card-1", when="2026-10-26T03:00:00Z")
    event("resurfacing_impression", card="private-card-2", when="2026-10-26T02:00:00Z")
    event("later_confirmed", card="private-card-2", when="2026-10-26T01:00:00Z")
    event("later_confirmed", card="private-card-3", when="2026-10-26T03:00:00Z")
    event("dismissed", card="private-card-2", when="2026-10-26T03:00:00Z", actor="SYSTEM")
    event("dismissed", card="private-card-2", when="2026-10-27T00:00:00Z")
    result = analyze(path)
    assert metric(result, "card_action_rate").numerator == 0
    assert metric(result, "card_action_rate").denominator == 1
    assert result.diagnostics["actions_before_impression_rows"] == 1
    assert result.diagnostics["actions_outside_impression_cohort_rows"] == 2


def test_open_terminals_pair_to_attempt_dedup_retries_and_preserve_new_attempts(dataset):
    path, _, event = dataset
    event("open_attempt", corr="open-one", card="private-card-1", when="2026-10-25T23:50:00Z")
    event("source_opened", corr="open-one", card="private-card-1")
    event("source_opened", corr="open-one", card="private-card-1", when="2026-10-26T02:00:00Z")
    event("open_attempt", corr="open-two", card="private-card-2")
    event("open_failed", corr="open-two", card="private-card-2")
    event("open_attempt", corr="new-deliberate-retry", card="private-card-2", when="2026-10-26T02:00:00Z")
    event("source_opened", corr="new-deliberate-retry", card="private-card-2", when="2026-10-26T02:00:00Z")
    event("open_attempt", corr="pending", card="private-card-3")
    event("source_opened", corr="missing-attempt", card="private-card-4")
    result = analyze(path)
    handoff = metric(result, "open_handoff_success_rate")
    assert (handoff.numerator, handoff.denominator) == (2, 3)
    assert result.diagnostics["open_attempts_without_terminal_groups"] == 1
    assert result.diagnostics["open_handoff_orphan_terminal_groups"] == 1
    assert result.diagnostics["open_handoff_success_rate_duplicate_terminal_rows"] == 1


def test_conflicting_terminal_results_and_missing_correlation_are_excluded(dataset):
    path, _, event = dataset
    event("capture_succeeded", corr="conflict")
    event("capture_failed", corr="conflict")
    event("capture_succeeded", corr="")
    event("open_attempt", corr="open-conflict", card="private-card-1")
    event("source_opened", corr="open-conflict", card="private-card-1")
    event("open_failed", corr="open-conflict", card="private-card-1")
    result = analyze(path)
    assert metric(result, "capture_success_rate").rate is None
    assert metric(result, "open_handoff_success_rate").rate is None
    assert result.diagnostics["capture_success_rate_conflicting_terminal_groups"] == 1
    assert result.diagnostics["capture_success_rate_missing_correlation_rows"] == 1
    assert result.diagnostics["open_handoff_success_rate_conflicting_terminal_groups"] == 1


def test_no_future_leakage_into_conflict_detection(dataset):
    path, _, event = dataset
    event("capture_succeeded", corr="same")
    event("capture_failed", corr="same", when="2026-10-27T01:00:00Z")
    assert metric(analyze(path), "capture_success_rate").rate == 1


def test_duplicate_event_and_idempotency_rows_and_conflicts(dataset):
    path, _, event = dataset
    event("capture_succeeded", corr="same", row_id="same-id", key="same-key")
    event("capture_succeeded", corr="same", row_id="same-id", key="same-key")
    event("capture_succeeded", corr="same", key="same-key", when="2026-10-26T02:00:00Z")
    event("capture_succeeded", corr="bad", key="conflicting-key")
    event("capture_failed", corr="bad", key="conflicting-key")
    result = analyze(path)
    assert metric(result, "capture_success_rate").denominator == 1
    assert result.diagnostics["duplicate_event_id_rows"] == 1
    assert result.diagnostics["duplicate_idempotency_key_rows"] == 1
    assert result.diagnostics["conflicting_idempotency_key_groups"] == 1


def test_invalid_unknown_orphan_and_cross_user_rows_are_aggregate_diagnostics(dataset):
    path, _, event = dataset
    event("capture_succeeded", when="bad-date")
    event("capture_succeeded", user="missing-user")
    event("capture_succeeded", item="missing-item")
    event("resurfacing_impression", card="private-card-7")
    event("resurfacing_impression", card="missing-card", item="private-item-1")
    event("resurfacing_impression", card="private-card-1", item="private-item-2")
    event("resurfacing_impression")
    event("new-event-not-supported")
    event("capture_failed", schema=99)
    result = analyze(path)
    assert all(m.rate is None for m in result.metrics)
    assert result.diagnostics["invalid_event_timestamps"] == 1
    assert result.diagnostics["invalid_identity_rows"] == 1
    assert result.diagnostics["orphan_or_cross_user_item_rows"] == 2
    assert result.diagnostics["orphan_or_cross_user_card_rows"] == 2
    assert result.diagnostics["missing_card_association_rows"] == 1
    assert result.diagnostics["unknown_event_name_rows"] == 1
    assert result.diagnostics["unsupported_event_schema_rows"] == 1


def test_empty_event_data_is_unavailable_and_report_discloses_historical_capture_gap(dataset, tmp_path):
    path, _, _ = dataset
    result = analyze(path)
    assert all(m.rate is None for m in result.metrics)
    outputs = write_reports(result, tmp_path / "reports", database=path)
    text = outputs["usage_analysis.md"].read_text(encoding="utf-8")
    assert "Capture instrumentation coverage" in text
    assert "did not emit capture outcomes" in text
    assert "cannot retroactively recover" in text
    assert "N/A, not 100%" in text
    assert "not user validation" in text
    assert "does not establish" in text
    with outputs["usage_summary.csv"].open(encoding="utf-8", newline="") as stream:
        assert [row["rate_percent"] for row in csv.DictReader(stream)] == ["N/A"] * 3
    svg = ET.fromstring(outputs["usage_metrics.svg"].read_text(encoding="utf-8"))
    assert svg.tag == "{http://www.w3.org/2000/svg}svg"


def test_capture_terminal_actor_is_system_and_start_coverage_is_explicit(dataset):
    path, _, event = dataset
    event("capture_started", corr="complete", actor="USER", when="2026-10-26T00:00:00Z")
    event("capture_succeeded", corr="complete", item="private-item-1", actor="SYSTEM")
    event("capture_succeeded", corr="legacy-no-start", actor="SYSTEM")
    event("capture_failed", corr="wrong-actor-user", actor="USER")
    event("capture_succeeded", corr="wrong-actor-client", actor="CLIENT")
    event("capture_started", corr="pending", actor="USER")
    result = analyze(path)
    capture = metric(result, "capture_success_rate")
    assert (capture.numerator, capture.denominator) == (2, 2)
    assert result.diagnostics["capture_terminal_groups_without_start"] == 1
    assert result.diagnostics["capture_success_rate_invalid_terminal_actor_rows"] == 2
    assert result.diagnostics["capture_starts_without_terminal_groups"] == 1


def test_open_cannot_use_wrong_actor_terminal_or_start(dataset):
    path, _, event = dataset
    event("open_attempt", corr="wrong-terminal", card="private-card-1")
    event("source_opened", corr="wrong-terminal", card="private-card-1", actor="USER")
    event("open_attempt", corr="wrong-attempt", card="private-card-2", actor="SYSTEM")
    event("source_opened", corr="wrong-attempt", card="private-card-2")
    result = analyze(path)
    assert metric(result, "open_handoff_success_rate").rate is None
    assert result.diagnostics["open_handoff_success_rate_invalid_terminal_actor_rows"] == 1
    assert result.diagnostics["open_handoff_orphan_terminal_groups"] == 1


def test_outputs_never_export_private_content_or_identifiers(dataset, tmp_path):
    path, _, event = dataset
    event("resurfacing_impression", card="private-card-1")
    event("open_attempt", corr="PRIVATE_CORRELATION", card="private-card-1")
    event("source_opened", corr="PRIVATE_CORRELATION", card="private-card-1")
    result = analyze(path)
    outputs = write_reports(result, tmp_path / "safe-reports", database=path)
    all_text = "".join(output.read_text(encoding="utf-8") for output in outputs.values())
    for private in ("private-user-one", "private-card-1", "private-item-1", "PRIVATE_CORRELATION",
                    "PRIVATE_TITLE", "PRIVATE_SOURCE_SNAPSHOT", "PRIVATE_EVENT_PROPERTY", "PRIVATE_COOKIE",
                    "private.example", str(path)):
        assert private not in all_text


def test_analysis_does_not_change_database_or_schema(dataset, tmp_path):
    path, connection, event = dataset
    event("capture_failed")
    before_hash = sha256(path.read_bytes()).hexdigest()
    before_schema = connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    result = analyze(path)
    write_reports(result, tmp_path / "reports", database=path)
    assert sha256(path.read_bytes()).hexdigest() == before_hash
    assert connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before_schema


def test_reader_opens_mode_ro_with_query_only_and_consistent_transaction(dataset, monkeypatch):
    import scripts.analyze_usage as module
    path, _, _ = dataset
    actual_connect = sqlite3.connect
    traced = []

    def connection_spy(database_uri, **kwargs):
        assert database_uri.endswith("?mode=ro")
        assert kwargs["uri"] is True
        connection = actual_connect(database_uri, **kwargs)
        connection.set_trace_callback(traced.append)
        return connection

    monkeypatch.setattr(module.sqlite3, "connect", connection_spy)
    analyze(path)
    assert traced[0] == "PRAGMA query_only=ON"
    assert traced[1] == "BEGIN"
    assert not any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for statement in traced)


def test_missing_input_or_schema_never_creates_database(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(AnalysisError, match="existing file"):
        analyze(missing)
    assert not missing.exists()
    invalid = tmp_path / "not-recallflow.db"
    with sqlite3.connect(invalid):
        pass
    with pytest.raises(AnalysisError, match="schema"):
        analyze(invalid)


def test_output_cannot_overwrite_input_database(dataset, tmp_path):
    path, _, _ = dataset
    result = analyze(path)
    protected_dir = tmp_path / "protected"
    protected_dir.mkdir()
    protected_input = protected_dir / "usage_summary.csv"
    protected_input.write_bytes(path.read_bytes())
    before = protected_input.read_bytes()
    with pytest.raises(AnalysisError, match="overwrite"):
        write_reports(result, protected_dir, database=protected_input)
    assert protected_input.read_bytes() == before


@pytest.mark.parametrize("value", ["2026-10-26", "2026-10-26T00:00:00", "not-a-time", "2026-10-26T08:00:00+08:00"])
def test_cli_boundary_requires_explicit_utc(value):
    with pytest.raises(AnalysisError, match="UTC"):
        parse_utc(value)


def test_window_and_label_validation(dataset):
    path, _, _ = dataset
    with pytest.raises(AnalysisError, match="earlier"):
        analyze_database(path, start=END, end=START, data_kind="demo")
    with pytest.raises(AnalysisError, match="timezone-aware"):
        analyze_database(path, start=START.replace(tzinfo=None), end=END, data_kind="demo")
    with pytest.raises(AnalysisError, match="data kind"):
        analyze_database(path, start=START, end=END, data_kind="unlabeled")
    assert parse_utc("2026-10-26T00:00:00+00:00") == START


def test_cli_success_error_and_explicit_data_label(dataset, tmp_path, capsys):
    path, _, _ = dataset
    args = ["--database", str(path), "--start", "2026-10-26T00:00:00Z",
            "--end", "2026-10-27T00:00:00Z", "--output-dir", str(tmp_path / "cli-reports")]
    with pytest.raises(SystemExit) as missing_label:
        main(args)
    assert missing_label.value.code == 2
    assert main(args + ["--data-kind", "demo"]) == 0
    assert "input database was read-only" in capsys.readouterr().out
    assert main(args + ["--data-kind", "demo", "--start", "invalid"]) == 2
    assert "Analysis failed" in capsys.readouterr().err


def test_cli_standalone_needs_no_app_import_or_runtime_dependencies(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "analyze_usage.py"
    completed = subprocess.run([sys.executable, "-I", str(script), "--help"],
                               cwd=tmp_path, capture_output=True, text=True, check=False)
    assert completed.returncode == 0
    assert "--data-kind" in completed.stdout
