import importlib.util
import json
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("audition.py")
SPEC = importlib.util.spec_from_file_location("openrouter_crowdbench", MODULE_PATH)
crowdbench = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = crowdbench
SPEC.loader.exec_module(crowdbench)


def test_free_model_filter_requires_zero_price_or_free_suffix():
    base = {"id": "vendor/model", "architecture": {"output_modalities": ["text"]}}
    assert crowdbench.is_free_text_model({**base, "pricing": {"prompt": "0", "completion": "0"}})
    assert crowdbench.is_free_text_model({**base, "id": "vendor/model:free", "pricing": {"prompt": "1", "completion": "1"}})
    assert not crowdbench.is_free_text_model({**base, "pricing": {"prompt": "0.1", "completion": "0"}})
    assert not crowdbench.is_free_text_model({**base, "id": "openrouter/free", "pricing": {"prompt": "0", "completion": "0"}})


def test_smoke_probe_checks_visible_instruction_following():
    assert crowdbench.grade_output("smoke", "READY 48 4") == (True, 1.0)
    valid, quality = crowdbench.grade_output("smoke", "ready")
    assert valid is False
    assert 0 < quality < 1


def test_request_payload_is_fixed_and_role_neutral():
    payload = crowdbench.request_payload({"id": "vendor/model"}, "smoke")
    assert payload["messages"][0]["content"] == crowdbench.SMOKE_PROMPT
    assert "response_format" not in payload


def test_rate_limited_result_is_not_recommended():
    result = crowdbench.ModelResult(model_id="x:free", model_name="x", role="smoke")
    result.probes = [
        crowdbench.Probe(attempt=1, ok=False, status=429, latency_ms=5),
        crowdbench.Probe(attempt=2, ok=False, status=429, latency_ms=5),
    ]
    crowdbench.finalize_result(result)
    assert result.status == "rate_limited"
    assert "route availability" in result.verdict_explanation


def test_partial_output_is_flagged_as_potentially_usable():
    result = crowdbench.ModelResult(model_id="x", model_name="x", role="smoke")
    result.probes = [
        crowdbench.Probe(attempt=1, ok=True, status=200, latency_ms=100, valid_format=False, role_quality=.4),
        crowdbench.Probe(attempt=2, ok=False, status=500, latency_ms=100),
    ]
    crowdbench.finalize_result(result)
    assert result.status == "incompatible"
    assert result.potentially_usable is True


def test_privacy_badges_are_precise_about_zdr():
    models = [{"id": "vendor/private"}, {"id": "vendor/other"}]
    checked = crowdbench.attach_privacy_status(models, {"vendor/private"})
    assert checked[0]["privacy"]["status"] == "zdr_available"
    assert "only when requests explicitly require ZDR" in checked[0]["privacy"]["description"]
    assert checked[1]["privacy"]["status"] == "no_zdr"
    assert "does not prove training occurs" in checked[1]["privacy"]["description"]


def test_history_prioritizes_working_then_untested_then_failed(monkeypatch):
    models = [{"id": "failed"}, {"id": "untested"}, {"id": "worked"}]
    monkeypatch.setattr(
        crowdbench,
        "historical_model_stats",
        lambda: {
            "failed": {"tests": 2, "working": 0, "score_total": 80, "rate_limits": 2},
            "worked": {"tests": 1, "working": 1, "score_total": 90, "rate_limits": 0},
        },
    )
    ordered = crowdbench.apply_history_priority(models)
    assert [item["id"] for item in ordered] == ["worked", "untested", "failed"]


def test_smoke_job_tests_each_model_once(monkeypatch):
    observed = []
    monkeypatch.setattr(
        crowdbench,
        "run_probe",
        lambda _gate, _key, model, role, attempt: (
            observed.append((model["id"], role, attempt))
            or crowdbench.Probe(attempt=attempt, ok=True, status=200, latency_ms=10, valid_format=True, role_quality=1.0)
        ),
    )
    monkeypatch.setattr(crowdbench, "save_job", lambda _job: None)
    ids = ["one", "two"]
    job = crowdbench.Job(id="smoke", role="smoke", test_mode="smoke", probes_per_model=1, requests_per_minute=12, model_ids=ids)
    crowdbench.execute_job(job, {item: {"id": item, "name": item} for item in ids}, "secret")
    assert observed == [("one", "smoke", 1), ("two", "smoke", 1)]
    assert job.status == "completed"


def test_reliability_runs_requested_probe_count_despite_failures(monkeypatch):
    attempts = []
    monkeypatch.setattr(
        crowdbench,
        "run_probe",
        lambda _gate, _key, _model, _role, attempt: (
            attempts.append(attempt) or crowdbench.Probe(attempt=attempt, ok=False, status=429, latency_ms=10)
        ),
    )
    monkeypatch.setattr(crowdbench, "save_job", lambda _job: None)
    job = crowdbench.Job(id="reliability", role="smoke", test_mode="reliability", probes_per_model=5, requests_per_minute=12, model_ids=["one"], max_requests=5)
    crowdbench.execute_job(job, {"one": {"id": "one", "name": "one"}}, "secret")
    assert attempts == [1, 2, 3, 4, 5]


def test_serialized_job_and_report_schema_never_contain_api_key():
    job = crowdbench.Job(id="safe", role="smoke", probes_per_model=1, requests_per_minute=1, model_ids=["one"])
    serialized = json.dumps(crowdbench.serialize_job(job))
    assert "api_key" not in serialized
    assert "sk-or" not in serialized


def test_model_history_reads_legacy_and_current_results(monkeypatch, tmp_path):
    monkeypatch.setattr(crowdbench, "RESULTS_DIR", tmp_path)
    report = {"id": "run-one", "completed_at": "2026-09-07T10:00:00+00:00", "test_mode": "smoke", "results": [{"model_id": "vendor/model:free", "model_name": "Model", "status": "recommended", "overall_score": 91, "success_rate": 1, "format_rate": 1, "median_latency_ms": 1200, "probes": [{"status": 200}, {"status": 429}]}]}
    (tmp_path / "report.json").write_text(json.dumps(report), encoding="utf-8")
    entries = crowdbench.model_history_entries()
    assert len(entries) == 1
    assert entries[0]["rate_limits"] == 1
    assert "role" not in entries[0]


def test_seed_history_is_copied_once_without_overwriting(monkeypatch, tmp_path):
    seed_dir = tmp_path / "seed"
    data_dir = tmp_path / "data"
    results_dir = data_dir / "results"
    seed_dir.mkdir()
    (seed_dir / "sample.json").write_text('{"source":"seed"}', encoding="utf-8")
    monkeypatch.setattr(crowdbench, "SEED_RESULTS_DIR", seed_dir)
    monkeypatch.setattr(crowdbench, "DATA_DIR", data_dir)
    monkeypatch.setattr(crowdbench, "RESULTS_DIR", results_dir)

    assert crowdbench.bootstrap_seed_results() == 1
    (results_dir / "sample.json").write_text('{"source":"community"}', encoding="utf-8")
    assert crowdbench.bootstrap_seed_results() == 0
    assert json.loads((results_dir / "sample.json").read_text())["source"] == "community"
