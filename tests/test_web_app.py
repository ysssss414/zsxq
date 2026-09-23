from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import web_app


class WebAppTests(unittest.TestCase):
    def test_validate_rejects_empty_company(self) -> None:
        with self.assertRaises(web_app.ValidationError):
            web_app.validate_job_payload(
                {
                    "company": "",
                    "group_id": "123",
                    "days": 30,
                    "max_keywords": 40,
                    "max_topics": 0,
                    "recent_pages": 5,
                    "recent_limit": 30,
                    "analysis_source": "detail_search",
                }
            )

    def test_validate_rejects_invalid_recent_limit(self) -> None:
        with self.assertRaises(web_app.ValidationError):
            web_app.validate_job_payload(
                {
                    "company": "普冉股份",
                    "group_id": "123",
                    "days": 30,
                    "max_keywords": 40,
                    "max_topics": 0,
                    "recent_pages": 5,
                    "recent_limit": 31,
                    "analysis_source": "detail_search",
                }
            )

    def test_build_cli_command_prefers_group_id(self) -> None:
        params = web_app.validate_job_payload(
            {
                "company": "普冉股份",
                "group_id": "123",
                "group_name": "每日调研",
                "days": 30,
                "max_keywords": 40,
                "max_topics": 0,
                "recent_pages": 5,
                "recent_limit": 30,
                "analysis_source": "detail_search",
                "recent_include_all": False,
            }
        )
        command = web_app.build_cli_command(params)
        self.assertEqual(command[:3], [sys.executable, "-u", "main.py"])
        self.assertIn("--group-id", command)
        self.assertNotIn("--group-name", command)
        self.assertIn("123", command)

    def test_build_cli_command_uses_group_name_when_no_id(self) -> None:
        params = web_app.validate_job_payload(
            {
                "company": "普冉股份",
                "group_name": "每日调研",
                "days": 30,
                "max_keywords": 40,
                "max_topics": 0,
                "recent_pages": 5,
                "recent_limit": 30,
                "analysis_source": "search",
                "recent_include_all": True,
            }
        )
        command = web_app.build_cli_command(params)
        self.assertIn("--group-name", command)
        self.assertIn("每日调研", command)
        self.assertIn("--recent-include-all", command)

    def test_markdown_to_html_escapes_markup_and_renders_basic_blocks(self) -> None:
        rendered = web_app.markdown_to_html("# 标题\n\n- `代码`\n- <script>alert(1)</script>")
        self.assertIn("<h1>标题</h1>", rendered)
        self.assertIn("<code>代码</code>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertNotIn("<script>", rendered)

    def test_child_env_forces_utf8_output(self) -> None:
        env = web_app.build_child_env()
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUTF8"], "1")

    def test_scan_reports_handles_missing_and_present_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports_dir = Path(tmp)
            run_dir = reports_dir / "测试公司_20260601_120000"
            run_dir.mkdir()
            missing = web_app.scan_reports(reports_dir)
            self.assertEqual(len(missing), 1)
            self.assertFalse(missing[0]["has_report"])

            (run_dir / "run_metadata.json").write_text(
                json.dumps({"company": "测试公司", "group_label": "每日调研", "days": 30}),
                encoding="utf-8",
            )
            (run_dir / "report.md").write_text("# 核心结论\n\n- 有报告。", encoding="utf-8")
            present = web_app.scan_reports(reports_dir)
            self.assertTrue(present[0]["has_report"])
            self.assertEqual(present[0]["company"], "测试公司")

            report = web_app.read_report(run_dir.name, reports_dir)
            self.assertIn("# 核心结论", report["markdown"])
            self.assertIn("<h1>核心结论</h1>", report["html"])
            self.assertEqual(report["bundle_download"], "")
            (run_dir / "pro_a_export.zip").write_bytes(b"synthetic-bundle")
            self.assertEqual(web_app.read_bundle(run_dir.name, reports_dir), b"synthetic-bundle")
            self.assertEqual(web_app.read_report(run_dir.name, reports_dir)["bundle_download"],
                             f"/api/reports/{run_dir.name}/pro-a-export")
            with self.assertRaises(web_app.ValidationError):
                web_app.read_bundle("../unsafe", reports_dir)


if __name__ == "__main__":
    unittest.main()
