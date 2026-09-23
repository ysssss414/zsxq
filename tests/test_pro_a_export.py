from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from pro_a_export import ExportError, build_bundle, export_run


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class ExportTests(unittest.TestCase):
    def fixture(self, root: Path) -> None:
        (root / "run_metadata.json").write_text(json.dumps({
            "company": "测试公司", "group_id": "GROUP_1", "group_label": "研究星球",
            "days": 30, "analysis_source": "detail_search", "generated_at": "2026-09-23T00:00:00+00:00",
        }, ensure_ascii=False), encoding="utf-8")
        write_jsonl(root / "detail_raw.jsonl", [
            {"topic_id": "B", "in_range": True, "published_at": "2026-09-22T00:00:00+00:00",
             "author": "作者甲", "keyword_sources": [{"keyword": "封装", "reason": "AI routing"}],
             "detail": {"content": "原始中文证据 B", "token": "SECRET_TOKEN_123"},
             "search_hits": [{"content": "检索上下文 B https://example.com/a?token=PRIVATE123 Bearer SHORT_SECRET"}]},
            {"topic_id": "A", "in_range": True, "published_at": "2026-09-22T00:00:00+00:00",
             "author": "作者乙", "keyword_sources": [],
             "detail": {"content": "原始中文证据 A", "cookie": "SECRET_COOKIE_123"},
             "search_hits": [{"content": "检索上下文 A"}]},
            {"topic_id": "C", "in_range": True, "published_at": "", "author": "作者丙",
             "keyword_sources": [], "detail": {"content": "无关内容"}, "search_hits": []},
        ])
        write_jsonl(root / "topic_analysis.jsonl", [
            {"topic_id": "A", "reportable": True, "relevance_level": "强相关", "category": "产业链",
             "credibility": "A", "summary": "AI_SUMMARY_SENTINEL", "impact": "AI_IMPACT_SENTINEL",
             "verification_items": ["AI_VERIFY_SENTINEL"]},
            {"topic_id": "B", "reportable": True, "relevance_level": "弱相关", "category": "行业",
             "credibility": "D", "summary": "AI_SUMMARY_SENTINEL"},
            {"topic_id": "C", "reportable": False, "relevance_level": "无实质关联"},
        ])

    def test_exact_selection_evidence_boundary_and_byte_determinism(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            first, manifest = build_bundle(root)
            second, again = build_bundle(root)
            self.assertEqual(first, second)
            self.assertEqual(manifest, again)
            self.assertEqual(manifest["input_topic_count"], 3)
            self.assertEqual(manifest["topic_count"], 2)
            self.assertEqual(manifest["omitted_unrelated"], 1)
            self.assertEqual(manifest["topic_ids"], ["A", "B"])
            with zipfile.ZipFile(io.BytesIO(first)) as archive:
                self.assertEqual(archive.namelist(), ["manifest.json", "topics.jsonl"])
                rows = [json.loads(line) for line in archive.read("topics.jsonl").splitlines()]
            self.assertIn("原始中文证据 A", rows[0]["evidence_text"])
            self.assertIn("检索上下文 B", rows[1]["evidence_text"])
            for forbidden in (b"AI_SUMMARY_SENTINEL", b"AI_IMPACT_SENTINEL", b"AI_VERIFY_SENTINEL",
                              b"SECRET_TOKEN_123", b"SECRET_COOKIE_123", b"?token=", b"SHORT_SECRET"):
                self.assertNotIn(forbidden, first)
            info = export_run(root)
            self.assertEqual(info["bundle_sha256"], manifest["bundle_sha256"])
            self.assertEqual((root / "pro_a_export.zip").read_bytes(), first)
            self.assertEqual(hashlib.sha256(first).digest(), hashlib.sha256(second).digest())

    def test_missing_analysis_and_duplicate_normalized_id_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            analysis = [json.loads(line) for line in (root / "topic_analysis.jsonl").read_text(encoding="utf-8").splitlines()]
            write_jsonl(root / "topic_analysis.jsonl", analysis[:1])
            _, manifest = build_bundle(root)
            self.assertEqual(manifest["omitted_missing_analysis"], 2)
            details = [json.loads(line) for line in (root / "detail_raw.jsonl").read_text(encoding="utf-8").splitlines()]
            write_jsonl(root / "detail_raw.jsonl", details + [{**details[0], "topic_id": " B "}])
            with self.assertRaisesRegex(ExportError, "DUPLICATE"):
                build_bundle(root)

    def test_analysis_source_modes_change_only_source_derived_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            for source, expected, excluded in (("detail", "原始中文证据 B", "检索上下文 B"),
                                               ("search", "检索上下文 B", "原始中文证据 B")):
                metadata = json.loads((root / "run_metadata.json").read_text(encoding="utf-8"))
                metadata["analysis_source"] = source
                (root / "run_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
                bundle, _ = build_bundle(root)
                with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
                    rows = [json.loads(line) for line in archive.read("topics.jsonl").splitlines()]
                self.assertIn(expected, rows[1]["evidence_text"])
                self.assertNotIn(excluded, rows[1]["evidence_text"])

    def test_publication_order_uses_utc_instant_then_id_with_null_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            details = [json.loads(line) for line in (root / "detail_raw.jsonl").read_text(encoding="utf-8").splitlines()]
            analyses = [json.loads(line) for line in (root / "topic_analysis.jsonl").read_text(encoding="utf-8").splitlines()]
            for topic_id, published in (("D", ""), ("E", "2026-09-23T00:00:00+00:00")):
                details.append({"topic_id": topic_id, "in_range": True, "published_at": published,
                                "author": "合成作者", "keyword_sources": [],
                                "detail": {"content": f"原始中文证据 {topic_id}"}, "search_hits": []})
                analyses.append({"topic_id": topic_id, "reportable": True,
                                 "relevance_level": "强相关", "category": "合成", "credibility": "B"})
            write_jsonl(root / "detail_raw.jsonl", details)
            write_jsonl(root / "topic_analysis.jsonl", analyses)
            _, manifest = build_bundle(root)
            self.assertEqual(manifest["topic_ids"], ["E", "A", "B", "D"])


if __name__ == "__main__":
    unittest.main()
