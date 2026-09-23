"""Deterministic, evidence-only export seam for pro_a Community intake."""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile
from typing import Any

from main import build_analysis_content
from zsxq_client import SENSITIVE_KEYWORDS, coerce_datetime, redact_sensitive


CONTRACT_VERSION = "zsxq-pro-a-community-export-v1"
MAX_TOPICS = 100
MAX_EVIDENCE_CHARS = 12000
MAX_TOTAL_EVIDENCE_CHARS = 500000
MAX_BUNDLE_BYTES = 20 * 1024 * 1024
MEMBERS = ("manifest.json", "topics.jsonl")
_SENSITIVE_KEYS = SENSITIVE_KEYWORDS | {"api_key", "apikey", "client_secret"}


class ExportError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ExportError("EXPORT_ROW_INVALID")
            rows.append(row)
    return rows


def _by_topic_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed = {}
    for row in rows:
        topic_id = str(row.get("topic_id", "")).strip()
        if not topic_id or topic_id in indexed:
            raise ExportError("EXPORT_TOPIC_ID_DUPLICATE_OR_MISSING")
        indexed[topic_id] = row
    return indexed


def _source_only(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _source_only(child) for key, child in value.items()
                if key.lower().replace("-", "_") not in _SENSITIVE_KEYS}
    if isinstance(value, list):
        return [_source_only(child) for child in value]
    return value


def _safe_text(value: str) -> str:
    text = redact_sensitive(value)
    text = re.sub(r"(?i)\bbearer\s+\S+", "[REDACTED]", text)
    text = re.sub(r"(?i)https?://[^\s]*[?&](?:token|cookie|secret|password|api[_-]?key)=[^\s]*",
                  "[REDACTED_URL]", text)
    text = re.sub(r"(?i)\b(?:token|cookie|secret|authorization|password|api[_-]?key)\s*[:=]\s*\S+",
                  "[REDACTED]", text)
    text = re.sub(r"(?i)(?:[A-Za-z]:[\\/]|\\\\|/(?:home|Users)/)[^\s]+", "[REDACTED_PATH]", text)
    text = re.sub(r"(?i)\.runtime-home(?:[\\/][^\s]+)?", "[REDACTED_PATH]", text)
    return text


def _sort_key(row: dict[str, Any]) -> tuple[int, float, str]:
    parsed = coerce_datetime(row["published_at"])
    return (0, -parsed.astimezone(dt.UTC).timestamp(), row["topic_id"]) if parsed else (1, 0, row["topic_id"])


def build_bundle(run_dir: Path) -> tuple[bytes, dict[str, Any]]:
    metadata = json.loads((run_dir / "run_metadata.json").read_text(encoding="utf-8"))
    source = metadata.get("analysis_source")
    if source not in {"detail_search", "detail", "search"}:
        raise ExportError("EXPORT_ANALYSIS_SOURCE_INVALID")
    details = _by_topic_id(_read_jsonl(run_dir / "detail_raw.jsonl"))
    analyses = _by_topic_id(_read_jsonl(run_dir / "topic_analysis.jsonl"))
    rows = []
    omitted_missing = omitted_unrelated = 0
    in_range_count = 0
    for topic_id, detail in details.items():
        if detail.get("in_range") is not True:
            continue
        in_range_count += 1
        analysis = analyses.get(topic_id)
        if analysis is None:
            omitted_missing += 1
            continue
        if analysis.get("reportable") is not True or analysis.get("relevance_level") == "无实质关联":
            omitted_unrelated += 1
            continue
        source_record = _source_only(detail)
        evidence = _safe_text(build_analysis_content(source_record, 1_000_000, source))
        if not evidence or len(evidence) > MAX_EVIDENCE_CHARS:
            raise ExportError("EXPORT_EVIDENCE_SIZE_INVALID")
        keywords = [str(item.get("keyword", "")) for item in detail.get("keyword_sources", [])
                    if isinstance(item, dict) and item.get("keyword")]
        published = coerce_datetime(detail.get("published_at"))
        row = {
            "topic_id": topic_id,
            "published_at": published.astimezone(dt.UTC).isoformat() if published else None,
            "author": _safe_text(str(detail.get("author", ""))),
            "keyword_sources": sorted(set(_safe_text(keyword) for keyword in keywords)),
            "evidence_text": evidence,
            "evidence_text_sha256": sha256(evidence.encode("utf-8")),
            "routing": {
                "reportable": True,
                "relevance_level": _safe_text(str(analysis.get("relevance_level", ""))),
                "category": _safe_text(str(analysis.get("category", ""))),
                "credibility": _safe_text(str(analysis.get("credibility", ""))),
            },
        }
        rows.append(row)
    if len(rows) > MAX_TOPICS or sum(len(row["evidence_text"]) for row in rows) > MAX_TOTAL_EVIDENCE_CHARS:
        raise ExportError("EXPORT_TOPIC_LIMIT_EXCEEDED")
    rows.sort(key=_sort_key)
    topics_bytes = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    topics_sha = sha256(topics_bytes)
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "bundle_id": "COMMUNITY_BUNDLE_" + topics_sha[:24].upper(),
        "company_input": _safe_text(str(metadata.get("company", ""))),
        "group_id": _safe_text(str(metadata.get("group_id", ""))),
        "group_label": _safe_text(str(metadata.get("group_label", ""))),
        "days": metadata.get("days"),
        "analysis_source": source,
        "generated_at": metadata.get("generated_at"),
        "input_topic_count": len(details),
        "in_range_count": in_range_count,
        "topic_count": len(rows),
        "reportable_topic_count": len(rows),
        "omitted_unrelated": omitted_unrelated,
        "omitted_missing_analysis": omitted_missing,
        "topic_ids": [row["topic_id"] for row in rows],
        "topics_file_sha256": topics_sha,
        "selection_rule": "in_range=true AND reportable=true AND relevance_level!=无实质关联",
        "source_pipeline": "zsxq-cli read-only topic search/recent scan/detail; DeepSeek routing only",
    }
    manifest["bundle_sha256"] = sha256(canonical_bytes(manifest) + b"\n" + topics_bytes)
    manifest_bytes = canonical_bytes(manifest) + b"\n"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in (("manifest.json", manifest_bytes), ("topics.jsonl", topics_bytes)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    bundle = output.getvalue()
    if len(bundle) > MAX_BUNDLE_BYTES:
        raise ExportError("EXPORT_BUNDLE_TOO_LARGE")
    return bundle, manifest


def export_run(run_dir: Path) -> dict[str, Any]:
    bundle, manifest = build_bundle(run_dir)
    (run_dir / "pro_a_export.zip").write_bytes(bundle)
    return {"contract_version": CONTRACT_VERSION, "filename": "pro_a_export.zip",
            "bundle_sha256": manifest["bundle_sha256"], "topic_count": manifest["topic_count"]}
