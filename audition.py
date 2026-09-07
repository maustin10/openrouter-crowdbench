#!/usr/bin/env python3
"""OpenRouter CrowdBench: community-oriented route reliability testing.

Contributor API keys are accepted per request, retained only by the active worker
thread, and never serialized into reports, logs, cookies, or browser storage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("CROWDBENCH_DATA_DIR", str(ROOT))).expanduser().resolve()
RESULTS_DIR = DATA_DIR / "results"
USAGE_LEDGER = DATA_DIR / "usage-ledger.jsonl"
SEED_RESULTS_DIR = ROOT / "seed-results"
SEED_VERSION = "20260907"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_RPM = 12
MAX_SELECTED_MODELS = 500
TEST_TYPES = ("smoke", "reliability")


def bootstrap_seed_results() -> int:
    """Copy the public starter history into a new data volume exactly once."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    marker = DATA_DIR / f".seeded-{SEED_VERSION}"
    if marker.exists() or not SEED_RESULTS_DIR.exists():
        return 0
    copied = 0
    for source in sorted(SEED_RESULTS_DIR.glob("*.json")):
        destination = RESULTS_DIR / source.name
        if not destination.exists():
            destination.write_bytes(source.read_bytes())
            copied += 1
    marker.write_text(utc_now(), encoding="utf-8")
    return copied


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_zero_price(value: Any) -> bool:
    if value is None or value == "":
        return False
    try:
        return float(value) == 0
    except (TypeError, ValueError):
        return False


def is_free_text_model(model: dict[str, Any]) -> bool:
    model_id = str(model.get("id", ""))
    pricing = model.get("pricing") or {}
    architecture = model.get("architecture") or {}
    outputs = architecture.get("output_modalities") or []
    modality = str(architecture.get("modality", ""))
    non_text_output = any(output != "text" for output in outputs)
    text_output = ("text" in outputs or modality.endswith("->text")) and not non_text_output
    explicitly_free = model_id.endswith(":free")
    zero_token_price = is_zero_price(pricing.get("prompt")) and is_zero_price(
        pricing.get("completion")
    )
    attributable_route = model_id != "openrouter/free"
    return bool(model_id and attributable_route and text_output and (explicitly_free or zero_token_price))


def is_text_model(model: dict[str, Any]) -> bool:
    architecture = model.get("architecture") or {}
    outputs = architecture.get("output_modalities") or []
    modality = str(architecture.get("modality", ""))
    return bool(model.get("id") and ("text" in outputs or modality.endswith("->text")))


def model_family(model_id: str, model_name: str = "") -> str:
    text = f"{model_id} {model_name}".lower()
    families = (
        ("GPT-OSS", r"gpt[-_ ]?oss"), ("Qwen", r"qwen"),
        ("Llama", r"llama"), ("DeepSeek", r"deepseek"),
        ("Gemma", r"gemma"), ("Mistral", r"mistral|mixtral|ministral"),
        ("Claude", r"claude"), ("GPT", r"(?:^|[/ -])gpt[-_ ]"),
        ("MiniMax", r"minimax"), ("GLM", r"(?:^|[/ -])glm"),
        ("Kimi", r"kimi|moonshot"), ("Grok", r"grok"),
        ("Command", r"command[-_ ]|cohere"), ("Nova", r"nova"),
        ("Phi", r"(?:^|[/ -])phi[-_ ]"),
    )
    for family, pattern in families:
        if re.search(pattern, text):
            return family
    provider = model_id.split("/", 1)[0] if "/" in model_id else "Other"
    return provider.replace("-", " ").title()


def normalize_model(model: dict[str, Any]) -> dict[str, Any]:
    pricing = model.get("pricing") or {}
    supported = sorted(set(model.get("supported_parameters") or []))
    item = {
        "id": model.get("id"),
        "name": model.get("name") or model.get("id"),
        "context_length": model.get("context_length"),
        "supported_parameters": supported,
        "structured_output": "structured_outputs" in supported
        or "response_format" in supported,
        "reasoning": "reasoning" in supported,
        "pricing": {
            "prompt": pricing.get("prompt", "0"),
            "completion": pricing.get("completion", "0"),
        },
        "per_request_limits": model.get("per_request_limits"),
    }
    item["is_free"] = str(item["id"]).endswith(":free") or (
        is_zero_price(pricing.get("prompt")) and is_zero_price(pricing.get("completion"))
    )
    item["family"] = model_family(str(item["id"]), str(item["name"]))
    return item


class RateGate:
    def __init__(self, rpm: int):
        self.interval = 60.0 / max(1, rpm)
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
        if delay:
            time.sleep(delay)


@dataclass
class Probe:
    attempt: int
    ok: bool
    status: int
    latency_ms: int
    provider: str | None = None
    generation_id: str | None = None
    finish_reason: str | None = None
    valid_format: bool = False
    role_quality: float = 0.0
    retry_after: float | None = None
    error: str | None = None
    output_preview: str | None = None


@dataclass
class ModelResult:
    model_id: str
    model_name: str
    role: str
    status: str = "queued"
    probes: list[Probe] = field(default_factory=list)
    success_rate: float = 0.0
    format_rate: float = 0.0
    median_latency_ms: int | None = None
    p95_latency_ms: int | None = None
    role_quality: float = 0.0
    reliability_score: float = 0.0
    overall_score: float = 0.0
    recommendation: str = "Not tested"
    verdict_explanation: str = ""
    next_step: str = ""
    potentially_usable: bool = False


@dataclass
class Job:
    id: str
    role: str
    probes_per_model: int
    requests_per_minute: int
    model_ids: list[str]
    contributor_id: str = "legacy-pre-counter"
    test_mode: str = "legacy"
    assignments: dict[str, list[str]] = field(default_factory=dict)
    selected_roles: list[str] = field(default_factory=list)
    target_working: int = 0
    max_requests: int = 1000
    max_models: int = MAX_SELECTED_MODELS
    working_counts: dict[str, int] = field(default_factory=dict)
    exhausted_roles: list[str] = field(default_factory=list)
    usage_tracked: bool = True
    stop_requested: bool = False
    status: str = "queued"
    created_at: str = field(default_factory=utc_now)
    completed_at: str | None = None
    current_model: str | None = None
    completed_models: int = 0
    total_requests: int = 0
    message: str = "Waiting to start"
    results: list[ModelResult] = field(default_factory=list)
    report_file: str | None = None


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
LEDGER_LOCK = threading.Lock()
CATALOG_CACHE: tuple[float, list[dict[str, Any]]] | None = None
ZDR_CACHE: tuple[float, set[str] | None] | None = None


def api_request(
    path: str,
    api_key: str | None,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 90,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost/openrouter-crowdbench",
        "X-Title": "OpenRouter CrowdBench",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(
        f"{OPENROUTER_BASE}{path}",
        data=body,
        method=method,
        headers=headers,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw), dict(response.headers.items())
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"error": {"message": raw[:500] or str(exc)}}
        return exc.code, data, dict(exc.headers.items())
    except (URLError, TimeoutError) as exc:
        return 0, {"error": {"message": str(exc)}}, {}


def historical_model_stats() -> dict[str, dict[str, Any]]:
    aggregate: dict[str, dict[str, Any]] = {}
    if not RESULTS_DIR.exists():
        return aggregate
    for path in RESULTS_DIR.glob("*.json"):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if report.get("test_mode") not in TEST_TYPES:
            continue
        tested_at = report.get("completed_at") or report.get("created_at")
        for result in report.get("results") or []:
            model_id = result.get("model_id")
            if not model_id:
                continue
            item = aggregate.setdefault(
                model_id,
                {"tests": 0, "working": 0, "score_total": 0.0, "rate_limits": 0, "last_tested": None},
            )
            item["tests"] += 1
            item["working"] += int(result.get("status") == "recommended")
            item["score_total"] += float(result.get("overall_score") or 0)
            item["rate_limits"] += sum(
                probe.get("status") == 429 for probe in result.get("probes") or []
            )
            if tested_at and (not item["last_tested"] or tested_at > item["last_tested"]):
                item["last_tested"] = tested_at
    return aggregate


def apply_history_priority(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stats = historical_model_stats()
    enriched: list[dict[str, Any]] = []
    for catalog_index, model in enumerate(models):
        item = dict(model)
        history = dict(stats.get(item["id"], {}))
        tests = int(history.get("tests", 0))
        working = int(history.get("working", 0))
        history.update(
            {
                "tests": tests,
                "working": working,
                "working_rate": working / tests if tests else None,
                "mean_score": history.get("score_total", 0.0) / tests if tests else None,
                "catalog_index": catalog_index,
            }
        )
        item["history"] = history
        enriched.append(item)
    return sorted(
        enriched,
        key=lambda item: (
            0 if (item["history"].get("working") or 0) > 0 else 1 if not item["history"].get("tests") else 2,
            -(item["history"].get("working_rate") or 0),
            -(item["history"].get("mean_score") or 0),
            item["history"].get("rate_limits") or 0,
            item["history"]["catalog_index"],
        ),
    )


def get_free_models(api_key: str, force: bool = False) -> list[dict[str, Any]]:
    return get_catalog_models(api_key, include_paid=False, force=force)


def get_zdr_model_ids(api_key: str, force: bool = False) -> set[str] | None:
    """Return model IDs with at least one advertised ZDR endpoint.

    None means the endpoint inventory could not be checked. An empty set is a
    successful check that found no ZDR-capable model routes.
    """
    global ZDR_CACHE
    if not force and ZDR_CACHE and time.time() - ZDR_CACHE[0] < 300:
        return ZDR_CACHE[1]
    status, payload, _ = api_request("/endpoints/zdr", api_key)
    if status != 200:
        ZDR_CACHE = (time.time(), None)
        return None
    ids = {
        str(endpoint.get("model_id") or endpoint.get("model") or "")
        for endpoint in payload.get("data", [])
        if isinstance(endpoint, dict)
    }
    ids.discard("")
    ZDR_CACHE = (time.time(), ids)
    return ids


def attach_privacy_status(
    models: list[dict[str, Any]], zdr_model_ids: set[str] | None
) -> list[dict[str, Any]]:
    """Attach endpoint-level privacy availability without overstating policy."""
    enriched = []
    for model in models:
        item = dict(model)
        if zdr_model_ids is None:
            status = "unknown"
            label = "Privacy unknown"
            description = "OpenRouter's zero-data-retention endpoint inventory could not be checked."
        elif item.get("id") in zdr_model_ids:
            status = "zdr_available"
            label = "ZDR route available"
            description = (
                "At least one current provider endpoint advertises zero data retention. "
                "This is guaranteed only when requests explicitly require ZDR routing."
            )
        else:
            status = "no_zdr"
            label = "No ZDR route advertised"
            description = (
                "No current endpoint advertises zero data retention for this model. "
                "Provider retention and training policies may vary; this does not prove training occurs."
            )
        item["privacy"] = {
            "status": status,
            "label": label,
            "description": description,
        }
        enriched.append(item)
    return enriched


def get_catalog_models(api_key: str, include_paid: bool = False, force: bool = False) -> list[dict[str, Any]]:
    global CATALOG_CACHE
    if not force and CATALOG_CACHE and time.time() - CATALOG_CACHE[0] < 300:
        models = CATALOG_CACHE[1]
        selected = models if include_paid else [m for m in models if m["is_free"]]
        return attach_privacy_status(apply_history_priority(selected), get_zdr_model_ids(api_key))
    status, payload, _ = api_request("/models?sort=throughput-high-to-low", api_key)
    if status != 200:
        message = (payload.get("error") or {}).get("message", "Catalog request failed")
        raise RuntimeError(f"OpenRouter catalog returned {status}: {message}")
    models = [normalize_model(m) for m in payload.get("data", []) if is_text_model(m)]
    CATALOG_CACHE = (time.time(), models)
    selected = models if include_paid else [m for m in models if m["is_free"]]
    return attach_privacy_status(apply_history_priority(selected), get_zdr_model_ids(api_key, force=force))


def get_current_prices() -> tuple[str, list[dict[str, Any]]]:
    """Fetch the public catalog so every page load receives current token prices."""
    status, payload, _ = api_request("/models", None, timeout=30)
    if status != 200:
        message = (payload.get("error") or {}).get("message", "Price catalog request failed")
        raise RuntimeError(f"OpenRouter price catalog returned {status}: {message}")
    models = [normalize_model(model) for model in payload.get("data", []) if is_text_model(model)]
    return utc_now(), models


SMOKE_PROMPT = "This is a connectivity and basic instruction-following check. Reply with exactly: READY 48 4"


def request_payload(model: dict[str, Any], role: str) -> dict[str, Any]:
    model_id = str(model["id"])
    return {
        "model": model_id,
        "messages": [{"role": "user", "content": SMOKE_PROMPT}],
        "max_tokens": 100,
        "provider": {"allow_fallbacks": True, "require_parameters": False},
    }


def visible_content(payload: dict[str, Any]) -> tuple[str, str | None]:
    choices = payload.get("choices") or []
    if not choices:
        return "", None
    choice = choices[0] or {}
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return str(content or "").strip(), choice.get("finish_reason")


def parse_json_object(text: str) -> dict[str, Any] | None:
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
    if fenced:
        candidates.append(fenced.group(1))
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    return None


def grade_output(role: str, text: str) -> tuple[bool, float]:
    if not text:
        return False, 0.0
    normalized = " ".join(text.upper().split())
    quality = (
        int("READY" in normalized)
        + int("48" in normalized)
        + int(re.search(r"\b4\b", normalized) is not None)
    ) / 3
    return quality == 1.0, quality


def error_message(payload: dict[str, Any]) -> str:
    error = payload.get("error") or {}
    if isinstance(error, dict):
        return str(error.get("message") or error.get("metadata") or "Provider error")[:500]
    return str(error)[:500]


def run_probe(
    gate: RateGate, api_key: str, model: dict[str, Any], role: str, attempt: int
) -> Probe:
    model_id = str(model["id"])
    gate.wait()
    started = time.monotonic()
    status, payload, headers = api_request(
        "/chat/completions", api_key, method="POST", payload=request_payload(model, role)
    )
    if model.get("is_free", True):
        record_free_request(model_id, role, status)
    latency_ms = round((time.monotonic() - started) * 1000)
    retry_header = headers.get("Retry-After") or headers.get("retry-after")
    try:
        retry_after = float(retry_header) if retry_header else None
    except ValueError:
        retry_after = None
    if status != 200:
        return Probe(
            attempt=attempt,
            ok=False,
            status=status,
            latency_ms=latency_ms,
            retry_after=retry_after,
            error=error_message(payload),
        )
    text, finish_reason = visible_content(payload)
    valid_format, role_quality = grade_output(role, text)
    ok = bool(text) and finish_reason not in {"error", "length"}
    return Probe(
        attempt=attempt,
        ok=ok,
        status=status,
        latency_ms=latency_ms,
        provider=payload.get("provider"),
        generation_id=payload.get("id"),
        finish_reason=finish_reason,
        valid_format=valid_format,
        role_quality=role_quality,
        error=None if ok else "Empty or incomplete visible output",
        output_preview=text[:240] or None,
    )


def record_free_request(model_id: str, role: str, status: int) -> None:
    entry = json.dumps(
        {"at": utc_now(), "model_id": model_id, "role": role, "status": status},
        separators=(",", ":"),
    )
    with LEDGER_LOCK:
        with USAGE_LEDGER.open("a", encoding="utf-8") as handle:
            handle.write(entry + "\n")


def local_free_requests_today() -> int:
    today = datetime.now(timezone.utc).date()
    used = 0
    if USAGE_LEDGER.exists():
        for line in USAGE_LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                if datetime.fromisoformat(entry["at"]).astimezone(timezone.utc).date() == today:
                    used += 1
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
    if RESULTS_DIR.exists():
        for path in RESULTS_DIR.glob("*.json"):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                completed = datetime.fromisoformat(report["completed_at"]).astimezone(timezone.utc).date()
                if completed == today and not report.get("usage_tracked", False):
                    used += int(report.get("total_requests", 0))
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
    return used


def free_quota_status(api_key: str) -> dict[str, Any]:
    status, payload, _ = api_request("/key", api_key)
    local_used = local_free_requests_today()
    if status != 200:
        return {
            "daily_limit": None,
            "locally_observed_used": local_used,
            "estimated_remaining": None,
            "authoritative": False,
            "note": "OpenRouter key status was unavailable; only this utility's observed usage is known.",
        }
    key_data = payload.get("data") or {}
    is_free_tier = bool(key_data.get("is_free_tier", True))
    daily_limit = 50 if is_free_tier else 1000
    return {
        "daily_limit": daily_limit,
        "locally_observed_used": local_used,
        "estimated_remaining": max(0, daily_limit - local_used),
        "authoritative": False,
        "tier": "50-request free tier" if is_free_tier else "1,000-request credited free-model tier",
        "note": "Upper-bound estimate: OpenRouter does not expose a real-time free-request count for a standard key. Calls made by other apps are not included.",
        "resets": "midnight UTC",
    }


def percentile_95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95 + 0.999) - 1))
    return ordered[index]


def finalize_result(result: ModelResult) -> None:
    count = len(result.probes)
    successes = [p for p in result.probes if p.ok]
    result.success_rate = len(successes) / count if count else 0.0
    result.format_rate = sum(p.valid_format for p in result.probes) / count if count else 0.0
    result.role_quality = sum(p.role_quality for p in result.probes) / count if count else 0.0
    latencies = [p.latency_ms for p in successes]
    result.median_latency_ms = round(median(latencies)) if latencies else None
    result.p95_latency_ms = percentile_95(latencies)
    rate_limits = sum(p.status == 429 for p in result.probes)
    empty = sum(p.status == 200 and not p.ok for p in result.probes)
    latency_score = max(0.0, 1.0 - ((result.median_latency_ms or 30000) / 30000))
    consistency = 1.0
    quality_values = [p.role_quality for p in successes]
    if len(quality_values) > 1:
        consistency = max(0.0, 1.0 - (max(quality_values) - min(quality_values)))
    result.reliability_score = round(
        100 * (0.65 * result.success_rate + 0.25 * result.format_rate + 0.10 * latency_score), 1
    )
    result.overall_score = round(
        100
        * (
            0.35 * result.success_rate
            + 0.25 * result.role_quality
            + 0.20 * result.format_rate
            + 0.10 * latency_score
            + 0.10 * consistency
        ),
        1,
    )
    if rate_limits >= 2:
        result.status, result.recommendation = "rate_limited", "Rate-limited in this test"
        result.potentially_usable = bool(successes)
        result.verdict_explanation = f"{len(successes)}/{count} probes returned usable output; {rate_limits} received HTTP 429. This measures route availability at the test rate, not answer quality."
        result.next_step = "Retry later or lower requests per minute. A paid route for the same family may have more capacity."
    elif empty >= 2:
        result.status, result.recommendation = "empty_output", "Repeated empty outputs"
        result.potentially_usable = bool(successes)
        result.verdict_explanation = f"{empty}/{count} probes returned HTTP 200 without a usable visible answer."
        result.next_step = "Try a larger output limit or a non-reasoning route, then retest before unattended use."
    elif result.success_rate == 1 and result.format_rate == 1 and result.overall_score >= 80:
        result.status, result.recommendation = "recommended", "Healthy in this test"
        result.potentially_usable = True
        result.verdict_explanation = f"All {count} probes returned usable output in the requested format and the overall score met the strict 80-point gate."
        result.next_step = "This route is a strong candidate now; continue monitoring because availability can change."
    elif result.success_rate >= 0.67 and result.format_rate >= 0.67:
        result.status, result.recommendation = "fragile", "Mostly working — use with safeguards"
        result.potentially_usable = True
        result.verdict_explanation = f"{len(successes)}/{count} probes succeeded and {round(result.format_rate * count)}/{count} followed the requested format, but it missed the strict recommendation gate."
        result.next_step = "Usable for experiments with validation, retries, and output-format checks; run more probes before production use."
    elif result.success_rate > 0:
        result.status, result.recommendation = "incompatible", "Partially working — instruction check missed"
        result.potentially_usable = True
        result.verdict_explanation = f"{len(successes)}/{count} probes produced output, but only {round(result.format_rate * count)}/{count} met the instruction and format checks."
        result.next_step = "It may work with a simpler prompt, larger output allowance, or a different provider route; retest that configuration."
    else:
        result.status, result.recommendation = "unavailable", "Unavailable during this test"
        result.verdict_explanation = f"None of the {count} probes produced a usable response during this test."
        result.next_step = "Retry later or choose another route; there is not enough evidence to use it unattended."


def serialize_job(job: Job) -> dict[str, Any]:
    data = asdict(job)
    if job.status in {"completed", "failed", "stopped", "limited"}:
        data["progress"] = 100
    else:
        data["progress"] = round(100 * job.completed_models / len(job.model_ids)) if job.model_ids else 0
    return data


def save_job(job: Job) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RESULTS_DIR / f"{stamp}-{job.role}-{job.id[:8]}.json"
    job.report_file = path.name
    path.write_text(json.dumps(serialize_job(job), indent=2), encoding="utf-8")


def history_items() -> list[dict[str, Any]]:
    if not RESULTS_DIR.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(RESULTS_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("test_mode") not in TEST_TYPES:
            continue
        results = data.get("results") or []
        items.append(
            {
                "file": path.name,
                "id": data.get("id"),
                "role": data.get("role", "unknown"),
                "test_mode": data.get("test_mode", "legacy"),
                "status": data.get("status", "unknown"),
                "created_at": data.get("created_at"),
                "completed_at": data.get("completed_at"),
                "model_count": len(results),
                "recommended_count": sum(r.get("status") == "recommended" for r in results),
                "total_requests": data.get("total_requests", 0),
                "target_working": data.get("target_working", 0),
                "working_counts": data.get("working_counts", {}),
                "max_requests": data.get("max_requests"),
                "max_models": data.get("max_models"),
            }
        )
    return items


def model_history_entries() -> list[dict[str, Any]]:
    """Return one durable observation per model/role test across saved reports."""
    if not RESULTS_DIR.exists():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(RESULTS_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("test_mode") not in TEST_TYPES:
            continue
        tested_at = data.get("completed_at") or data.get("created_at")
        for result in data.get("results") or []:
            model_id = result.get("model_id")
            if not model_id:
                continue
            probes = result.get("probes") or []
            entries.append(
                {
                    "tested_at": tested_at,
                    "report_file": path.name,
                    "run_id": data.get("id"),
                    "test_mode": data.get("test_mode", "legacy"),
                    "contributor_id": data.get("contributor_id") or "legacy-pre-counter",
                    "model_id": model_id,
                    "model_name": result.get("model_name") or model_id,
                    "family": model_family(model_id, result.get("model_name") or model_id),
                    "status": result.get("status", "unknown"),
                    "working": result.get("status") == "recommended",
                    "overall_score": float(result.get("overall_score") or 0),
                    "success_rate": float(result.get("success_rate") or 0),
                    "format_rate": float(result.get("format_rate") or 0),
                    "role_quality": float(result.get("role_quality") or 0),
                    "median_latency_ms": result.get("median_latency_ms"),
                    "rate_limits": sum(probe.get("status") == 429 for probe in probes),
                    "probe_count": len(probes),
                }
            )
    return entries


def execute_job(job: Job, models: dict[str, dict[str, Any]], api_key: str) -> None:
    try:
        gate = RateGate(job.requests_per_minute)
        job.status = "running"
        job.message = "Starting OpenRouter route checks"
        tasks = [("smoke", model_id) for model_id in job.model_ids]
        halt_status: str | None = None
        halt_message: str | None = None
        while tasks:
            if job.stop_requested:
                halt_status, halt_message = "stopped", "Stopped by user after the current request"
                break
            if job.completed_models >= job.max_models:
                halt_status, halt_message = "limited", f"Stopped at the configured limit of {job.max_models} tested models"
                break
            if job.total_requests >= job.max_requests:
                halt_status, halt_message = "limited", f"Stopped at the configured limit of {job.max_requests} total requests"
                break
            role, model_id = tasks.pop(0)
            model = models.get(model_id, {"id": model_id, "name": model_id})
            result = ModelResult(model_id=model_id, model_name=model.get("name", model_id), role=role)
            result.status = "testing"
            job.results.append(result)
            job.current_model = model_id
            failures = 0
            for attempt in range(1, job.probes_per_model + 1):
                if job.stop_requested:
                    halt_status, halt_message = "stopped", "Stopped by user after the current request"
                    break
                if job.total_requests >= job.max_requests:
                    halt_status, halt_message = "limited", f"Stopped at the configured limit of {job.max_requests} total requests"
                    break
                job.message = f"Testing {role}: {model_id} · probe {attempt}/{job.probes_per_model}"
                probe = run_probe(gate, api_key, model, role, attempt)
                result.probes.append(probe)
                job.total_requests += 1
                if probe.status == 429 or (probe.status == 200 and not probe.ok) or not probe.valid_format:
                    failures += 1
                if failures >= 2 and job.test_mode != "reliability":
                    break
            finalize_result(result)
            complete_probe_set = len(result.probes) == job.probes_per_model or (
                failures >= 2 and job.test_mode != "reliability"
            )
            if not complete_probe_set:
                result.status = "incomplete"
                result.recommendation = "Stopped before the reliability check completed"
            job.completed_models += 1
            if halt_status:
                break
        job.results.sort(key=lambda item: (item.overall_score, item.reliability_score), reverse=True)
        if halt_status:
            job.status, job.message = halt_status, halt_message or "Discovery stopped"
        else:
            job.status = "completed"
            job.message = "CrowdBench test complete"
    except Exception as exc:  # keep a background failure visible in the UI/report
        job.status = "failed"
        job.message = f"CrowdBench stopped safely: {type(exc).__name__}: {exc}"
    finally:
        job.current_model = None
        job.completed_at = utc_now()
        save_job(job)


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenRouterCrowdBench/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload: Any, status: int = 200) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def contributor_key(self) -> str | None:
        """Read a contributor key for this request without persisting it."""
        value = self.headers.get("X-OpenRouter-Key", "").strip()
        return value or None

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path == "/index.html":
            self.send_file(ROOT / "index.html", "text/html; charset=utf-8")
            return
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.send_json({"status": "ok", "service": "openrouter-crowdbench"})
            return
        if parsed.path == "/api/key-status":
            api_key = self.contributor_key()
            if not api_key:
                self.send_json({"error": "Enter an OpenRouter API key to connect"}, 401)
                return
            status, payload, _ = api_request("/key", api_key)
            if status != 200:
                self.send_json({"error": "OpenRouter rejected this key"}, 401)
                return
            key_data = payload.get("data") or {}
            self.send_json({
                "connected": True,
                "label": key_data.get("label") or "OpenRouter contributor key",
                "is_free_tier": bool(key_data.get("is_free_tier", True)),
            })
            return
        if parsed.path == "/api/prices":
            try:
                fetched_at, models = get_current_prices()
                self.send_json({"fetched_at": fetched_at, "models": models})
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, 502)
            return
        if parsed.path == "/api/models":
            api_key = self.contributor_key()
            if not api_key:
                self.send_json({"error": "Connect an OpenRouter API key first"}, 401)
                return
            try:
                include_paid = parse_qs(parsed.query).get("scope", ["free"])[0] == "all"
                self.send_json({"models": get_catalog_models(api_key, include_paid=include_paid, force=True)})
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, 502)
            return
        if self.path == "/api/history":
            self.send_json({"history": history_items()})
            return
        if self.path == "/api/model-history":
            self.send_json({"entries": model_history_entries()})
            return
        if self.path == "/api/quota":
            api_key = self.contributor_key()
            if not api_key:
                self.send_json({"error": "Connect an OpenRouter API key first"}, 401)
            else:
                self.send_json(free_quota_status(api_key))
            return
        history = re.fullmatch(r"/api/history/([A-Za-z0-9_.-]+\.json)", self.path)
        if history:
            resolved = (RESULTS_DIR / history.group(1)).resolve()
            if resolved.parent != RESULTS_DIR.resolve() or not resolved.exists():
                self.send_json({"error": "Historical report not found"}, 404)
            else:
                try:
                    data = json.loads(resolved.read_text(encoding="utf-8"))
                    data["report_file"] = history.group(1)
                    self.send_json(data)
                except (OSError, json.JSONDecodeError):
                    self.send_json({"error": "Historical report is unreadable"}, 500)
            return
        match = re.fullmatch(r"/api/runs/([a-f0-9-]+)", self.path)
        if match:
            with JOBS_LOCK:
                job = JOBS.get(match.group(1))
            if not job:
                self.send_json({"error": "Run not found"}, 404)
            else:
                self.send_json(serialize_job(job))
            return
        report = re.fullmatch(r"/reports/([A-Za-z0-9_.-]+\.json)", self.path)
        if report:
            resolved = (RESULTS_DIR / report.group(1)).resolve()
            if resolved.parent != RESULTS_DIR.resolve():
                self.send_error(HTTPStatus.BAD_REQUEST)
            else:
                self.send_file(resolved, "application/json; charset=utf-8")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        stop_match = re.fullmatch(r"/api/runs/([a-f0-9-]+)/stop", self.path)
        if stop_match:
            with JOBS_LOCK:
                job = JOBS.get(stop_match.group(1))
                if job and job.status in {"queued", "running", "stopping"}:
                    job.stop_requested = True
                    job.status = "stopping"
                    job.message = "Stop requested; waiting for the current provider request to return"
            if not job:
                self.send_json({"error": "Run not found"}, 404)
            elif job.status not in {"queued", "running", "stopping"}:
                self.send_json({"error": "Run is already finished"}, 409)
            else:
                self.send_json(serialize_job(job), 202)
            return
        if self.path != "/api/runs":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        api_key = self.contributor_key()
        if not api_key:
            self.send_json({"error": "Connect an OpenRouter API key first"}, 401)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            probes = int(payload.get("probes_per_model", 3))
            rpm = int(payload.get("requests_per_minute", DEFAULT_RPM))
            test_mode = str(payload.get("test_mode", "smoke"))
            max_requests = int(payload.get("max_requests", 50))
            max_models = int(payload.get("max_models", MAX_SELECTED_MODELS))
            catalog_scope = str(payload.get("catalog_scope", "free"))
            allow_paid = payload.get("allow_paid", False) is True
            tester_id = str(payload.get("tester_id") or "").strip().lower()
            if test_mode == "smoke":
                probes = 1
            elif not 1 <= probes <= 50:
                raise ValueError("Reliability-test count must be between 1 and 50")
            if not 1 <= rpm <= 12:
                raise ValueError("Free-tier safety limit must be between 1 and 12 RPM")
            if test_mode not in TEST_TYPES:
                raise ValueError("Test mode must be smoke or reliability")
            if not 1 <= max_requests <= 1000:
                raise ValueError("Total-request limit must be between 1 and 1,000")
            if not 1 <= max_models <= MAX_SELECTED_MODELS:
                raise ValueError(f"Model-test limit must be between 1 and {MAX_SELECTED_MODELS}")
            selected = list(dict.fromkeys(payload.get("model_ids") or []))
            if not selected or len(selected) > 5000:
                raise ValueError("Select between 1 and 5,000 model candidates")
            if catalog_scope not in {"free", "all"}:
                raise ValueError("Catalog scope must be free or all")
            if not re.fullmatch(r"[a-f0-9-]{20,64}", tester_id):
                raise ValueError("Anonymous tester identifier is missing or invalid")
            contributor_id = hashlib.sha256(f"crowdbench:{tester_id}".encode()).hexdigest()[:20]
            catalog = get_catalog_models(api_key, include_paid=catalog_scope == "all", force=True)
            models = {item["id"]: item for item in catalog}
            invalid = [item for item in selected if item not in models]
            if invalid:
                raise ValueError("One or more selected models are not in the current catalog scope")
            paid = [model_id for model_id in selected if not models[model_id].get("is_free")]
            if paid and not allow_paid:
                raise ValueError("Paid models require explicit paid-use confirmation")
            with JOBS_LOCK:
                if any(job.status in {"queued", "running", "stopping"} for job in JOBS.values()):
                    raise ValueError("Another CrowdBench test is already running; wait for it to finish")
        except (ValueError, TypeError, json.JSONDecodeError, RuntimeError) as exc:
            self.send_json({"error": str(exc)}, 400)
            return
        job = Job(
            id=str(uuid.uuid4()),
            role="smoke",
            probes_per_model=probes,
            requests_per_minute=rpm,
            model_ids=selected,
            contributor_id=contributor_id,
            test_mode=test_mode,
            max_requests=max_requests,
            max_models=max_models,
        )
        with JOBS_LOCK:
            JOBS[job.id] = job
        thread = threading.Thread(
            target=execute_job, args=(job, models, api_key), daemon=True, name=f"crowdbench-{job.id[:8]}"
        )
        thread.start()
        self.send_json({"id": job.id}, 202)


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenRouter CrowdBench UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8012)
    args = parser.parse_args()
    seeded = bootstrap_seed_results()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"OpenRouter CrowdBench: http://{args.host}:{args.port}")
    print(f"Inference safety ceiling: {DEFAULT_RPM} requests/minute")
    if seeded:
        print(f"Seeded {seeded} historical CrowdBench reports into {RESULTS_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
