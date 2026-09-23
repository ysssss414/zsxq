from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from main import load_dotenv
from zsxq_client import ZsxqClient, extract_group_id, extract_group_name, redact_sensitive


PROJECT_ROOT = Path(__file__).resolve().parent
REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
TERMINAL_STATES = {"success", "failed"}

load_dotenv(PROJECT_ROOT / ".env")


def utc_now() -> str:
    return dt.datetime.now(tz=dt.UTC).isoformat()


@dataclass
class Job:
    id: str
    params: dict[str, Any]
    status: str = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    logs: list[str] = field(default_factory=list)
    output_dir: str = ""
    report_path: str = ""
    error: str = ""
    returncode: int | None = None


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._active_job_id: str | None = None

    def create_job(self, payload: dict[str, Any]) -> Job:
        params = validate_job_payload(payload)
        with self._lock:
            if self._active_job_id:
                active = self._jobs.get(self._active_job_id)
                if active and active.status not in TERMINAL_STATES:
                    raise JobConflict("已有报告任务正在运行，请等待完成后再提交。")

            job = Job(id=uuid.uuid4().hex[:12], params=params)
            self._jobs[job.id] = job
            self._active_job_id = job.id

        thread = threading.Thread(target=self._run_job, args=(job.id,), daemon=True)
        thread.start()
        return job

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def job_payload(self, job_id: str) -> dict[str, Any] | None:
        job = self.get_job(job_id)
        if not job:
            return None
        return job_to_dict(job, include_report=True)

    def _append_log(self, job: Job, message: str) -> None:
        clean = redact_sensitive(message.rstrip())
        with self._lock:
            job.logs.append(clean)
            if len(job.logs) > 2000:
                job.logs = job.logs[-2000:]

    def _run_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if not job:
            return

        command = build_cli_command(job.params)
        with self._lock:
            job.status = "running"
            job.started_at = utc_now()
        self._append_log(job, "启动报告任务。")

        try:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=build_child_env(),
            )

            assert process.stdout is not None
            for line in process.stdout:
                clean_line = line.rstrip()
                self._capture_paths(job, clean_line)
                self._append_log(job, clean_line)

            returncode = process.wait()
            with self._lock:
                job.returncode = returncode
                job.finished_at = utc_now()
                if returncode == 0:
                    job.status = "success"
                    self._fill_missing_report_path(job)
                else:
                    job.status = "failed"
                    job.error = f"报告任务退出码：{returncode}"
        except Exception as exc:  # pragma: no cover - defensive for runtime only
            with self._lock:
                job.status = "failed"
                job.error = redact_sensitive(str(exc))
                job.finished_at = utc_now()
            self._append_log(job, f"任务异常：{exc}")
        finally:
            with self._lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None

    def _capture_paths(self, job: Job, line: str) -> None:
        if line.startswith("Output directory:"):
            with self._lock:
                job.output_dir = line.split(":", 1)[1].strip()
        elif line.startswith("Markdown report:"):
            with self._lock:
                job.report_path = line.split(":", 1)[1].strip()

    def _fill_missing_report_path(self, job: Job) -> None:
        if job.report_path and Path(job.report_path).exists():
            return
        if job.output_dir:
            candidate = Path(job.output_dir) / "report.md"
            if candidate.exists():
                job.report_path = str(candidate)


class ValidationError(ValueError):
    pass


class JobConflict(RuntimeError):
    pass


JOB_MANAGER = JobManager()


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def public_defaults() -> dict[str, Any]:
    return {
        "days": 30,
        "max_keywords": env_int("MAX_KEYWORDS", 40),
        "max_topics": env_int("MAX_TOPICS", 0),
        "recent_pages": env_int("RECENT_PAGES", 0),
        "recent_limit": min(30, max(1, env_int("RECENT_LIMIT", 30))),
        "analysis_source": os.getenv("ANALYSIS_SOURCE", "detail_search"),
        "deepseek_model": os.getenv("DEEPSEEK_MODEL", ""),
        "reports_dir": str(REPORTS_DIR),
    }


def validate_job_payload(payload: dict[str, Any]) -> dict[str, Any]:
    company = str(payload.get("company", "")).strip()
    group_id = str(payload.get("group_id", "")).strip()
    group_name = str(payload.get("group_name", "")).strip()
    if not company:
        raise ValidationError("请输入公司名。")
    if not group_id and not group_name:
        raise ValidationError("请选择星球，或手动输入 group_id/group_name。")

    params = {
        "company": company,
        "group_id": group_id,
        "group_name": group_name,
        "days": bounded_int(payload.get("days"), "时间范围", 1, 3650),
        "max_keywords": bounded_int(payload.get("max_keywords"), "最大关键词数", 0, 500),
        "max_topics": bounded_int(payload.get("max_topics"), "最大 topic 数", 0, 10000),
        "recent_pages": bounded_int(payload.get("recent_pages"), "最近主题流页数", 0, 200),
        "recent_limit": bounded_int(payload.get("recent_limit"), "最近主题流每页数量", 1, 30),
        "analysis_source": str(payload.get("analysis_source", "detail_search")).strip(),
        "recent_include_all": bool(payload.get("recent_include_all", False)),
    }
    if params["analysis_source"] not in {"detail_search", "detail", "search"}:
        raise ValidationError("analysis_source 只能是 detail_search、detail 或 search。")
    return params


def bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label}必须是整数。") from exc
    if parsed < minimum or parsed > maximum:
        raise ValidationError(f"{label}必须在 {minimum} 到 {maximum} 之间。")
    return parsed


def build_cli_command(params: dict[str, Any]) -> list[str]:
    command = [
        sys.executable,
        "-u",
        "main.py",
        "--company",
        params["company"],
    ]
    if params.get("group_id"):
        command.extend(["--group-id", params["group_id"]])
    else:
        command.extend(["--group-name", params["group_name"]])
    command.extend(
        [
            "--days",
            str(params["days"]),
            "--output-dir",
            "./reports",
            "--max-keywords",
            str(params["max_keywords"]),
            "--max-topics",
            str(params["max_topics"]),
            "--recent-pages",
            str(params["recent_pages"]),
            "--recent-limit",
            str(params["recent_limit"]),
            "--analysis-source",
            params["analysis_source"],
        ]
    )
    if params.get("recent_include_all"):
        command.append("--recent-include-all")
    return command


def build_child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def list_groups() -> dict[str, Any]:
    try:
        groups = []
        for group in ZsxqClient().list_groups():
            group_id = extract_group_id(group)
            group_name = extract_group_name(group)
            if group_id and group_name:
                groups.append({"id": group_id, "name": group_name})
        return {"groups": groups, "error": ""}
    except Exception as exc:
        return {"groups": [], "error": redact_sensitive(str(exc))}


def scan_reports(reports_dir: Path = REPORTS_DIR) -> list[dict[str, Any]]:
    if not reports_dir.exists():
        return []
    items: list[dict[str, Any]] = []
    for run_dir in sorted(
        [path for path in reports_dir.iterdir() if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ):
        metadata = read_json_file(run_dir / "run_metadata.json")
        report_path = run_dir / "report.md"
        items.append(
            {
                "run_name": run_dir.name,
                "company": metadata.get("company") or run_dir.name.split("_", 1)[0],
                "group_label": metadata.get("group_label", ""),
                "generated_at": metadata.get("generated_at", ""),
                "days": metadata.get("days", ""),
                "has_report": report_path.exists(),
                "report_path": str(report_path) if report_path.exists() else "",
                "run_dir": str(run_dir),
            }
        )
    return items


def read_report(run_name: str, reports_dir: Path = REPORTS_DIR) -> dict[str, Any]:
    safe_name = validate_run_name(run_name)
    run_dir = (reports_dir / safe_name).resolve()
    reports_root = reports_dir.resolve()
    if reports_root not in run_dir.parents and run_dir != reports_root:
        raise ValidationError("非法报告目录。")
    if not run_dir.is_dir():
        raise FileNotFoundError("报告不存在。")

    metadata = read_json_file(run_dir / "run_metadata.json")
    markdown = read_text_file(run_dir / "report.md")
    return {
        "run_name": safe_name,
        "run_dir": str(run_dir),
        "metadata": metadata,
        "markdown": markdown,
        "html": markdown_to_html(markdown),
        "report_path": str(run_dir / "report.md"),
        "bundle_download": (f"/api/reports/{safe_name}/pro-a-export"
                            if (run_dir / "pro_a_export.zip").is_file() else ""),
    }


def read_bundle(run_name: str, reports_dir: Path = REPORTS_DIR) -> bytes:
    safe_name = validate_run_name(run_name)
    run_dir = (reports_dir / safe_name).resolve()
    if reports_dir.resolve() not in run_dir.parents or not run_dir.is_dir():
        raise FileNotFoundError("Bundle 不存在。")
    bundle = run_dir / "pro_a_export.zip"
    if bundle.is_symlink() or not bundle.is_file():
        raise FileNotFoundError("Bundle 不存在。")
    return bundle.read_bytes()


def validate_run_name(run_name: str) -> str:
    decoded = unquote(run_name).strip()
    if not decoded or "/" in decoded or "\\" in decoded or ".." in decoded:
        raise ValidationError("非法报告名称。")
    return decoded


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_text_file(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def job_to_dict(job: Job, include_report: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": job.id,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "company": job.params.get("company", ""),
        "params": job.params,
        "logs": list(job.logs),
        "output_dir": job.output_dir,
        "report_path": job.report_path,
        "error": job.error,
        "returncode": job.returncode,
        "bundle_download": (f"/api/reports/{Path(job.output_dir).name}/pro-a-export"
                            if job.status == "success" and job.output_dir and
                            (Path(job.output_dir) / "pro_a_export.zip").is_file() else ""),
    }
    if include_report and job.report_path:
        markdown = read_text_file(Path(job.report_path))
        payload["markdown"] = markdown
        payload["html"] = markdown_to_html(markdown)
    return payload


def markdown_to_html(markdown: str) -> str:
    output: list[str] = []
    paragraph: list[str] = []
    in_list = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            output.append("<p>" + render_inline(" ".join(paragraph)) + "</p>")
            paragraph = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            output.append("</ul>")
            in_list = False

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            flush_paragraph()
            close_list()
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            output.append(f"<h{level}>{render_inline(heading.group(2).strip())}</h{level}>")
            continue
        if line.startswith("- "):
            flush_paragraph()
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{render_inline(line[2:].strip())}</li>")
            continue
        paragraph.append(line.strip())

    flush_paragraph()
    close_list()
    return "\n".join(output)


def render_inline(text: str) -> str:
    parts = re.split(r"(`[^`]*`)", text)
    rendered = []
    for part in parts:
        if len(part) >= 2 and part.startswith("`") and part.endswith("`"):
            rendered.append(f"<code>{html.escape(part[1:-1])}</code>")
        else:
            rendered.append(html.escape(part))
    return "".join(rendered)


class WebHandler(BaseHTTPRequestHandler):
    server_version = "ZsxqReportWeb/1.0"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                self.send_html(INDEX_HTML)
            elif path == "/api/defaults":
                self.send_json(public_defaults())
            elif path == "/api/groups":
                self.send_json(list_groups())
            elif path.startswith("/api/jobs/"):
                job_id = path.rsplit("/", 1)[-1]
                payload = JOB_MANAGER.job_payload(job_id)
                if payload is None:
                    self.send_json({"error": "任务不存在。"}, status=404)
                else:
                    self.send_json(payload)
            elif path == "/api/reports":
                self.send_json({"reports": scan_reports()})
            elif path.startswith("/api/reports/") and path.endswith("/pro-a-export"):
                run_name = path[len("/api/reports/"):-len("/pro-a-export")]
                self.send_bytes(read_bundle(run_name), "application/zip", "pro_a_export.zip")
            elif path.startswith("/api/reports/"):
                run_name = path.split("/api/reports/", 1)[1]
                self.send_json(read_report(run_name))
            else:
                self.send_json({"error": "Not found"}, status=404)
        except ValidationError as exc:
            self.send_json({"error": str(exc)}, status=400)
        except FileNotFoundError as exc:
            self.send_json({"error": str(exc)}, status=404)
        except Exception as exc:  # pragma: no cover - HTTP guardrail
            self.send_json({"error": redact_sensitive(str(exc))}, status=500)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/jobs":
            self.send_json({"error": "Not found"}, status=404)
            return

        try:
            payload = self.read_json_body()
            job = JOB_MANAGER.create_job(payload)
            self.send_json(job_to_dict(job), status=201)
        except ValidationError as exc:
            self.send_json({"error": str(exc)}, status=400)
        except JobConflict as exc:
            self.send_json({"error": str(exc)}, status=409)
        except Exception as exc:  # pragma: no cover - HTTP guardrail
            self.send_json({"error": redact_sensitive(str(exc))}, status=500)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON object。")
        return data

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, markup: str, status: int = 200) -> None:
        data = markup.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data: bytes, content_type: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), WebHandler)
    print(f"Web app running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web app.")
    finally:
        server.server_close()


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>知识星球报告生成</title>
  <style>
    :root {
      --bg: #f5f6f8;
      --panel: #ffffff;
      --ink: #20242a;
      --muted: #6a7280;
      --line: #d9dee7;
      --accent: #0f766e;
      --accent-dark: #115e59;
      --warn: #b45309;
      --danger: #b91c1c;
      --code: #101828;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      font-size: 14px;
      letter-spacing: 0;
    }
    header {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      padding: 16px 24px;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 700; }
    h2 { margin: 0 0 12px; font-size: 16px; }
    h3 { margin: 18px 0 8px; font-size: 15px; }
    .notice {
      margin-top: 10px;
      color: #7c2d12;
      background: #fff7ed;
      border: 1px solid #fed7aa;
      border-radius: 6px;
      padding: 10px 12px;
    }
    main {
      display: grid;
      grid-template-columns: minmax(320px, 420px) minmax(0, 1fr);
      gap: 16px;
      padding: 16px 24px 24px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-width: 0;
    }
    label { display: block; margin: 12px 0 6px; color: #344054; font-weight: 600; }
    input, select {
      width: 100%;
      height: 38px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      padding: 0 10px;
      color: var(--ink);
      background: #fff;
    }
    input[type="checkbox"] { width: 16px; height: 16px; vertical-align: middle; }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .check-row { margin-top: 12px; color: #344054; }
    .hint { color: var(--muted); font-size: 12px; margin-top: 4px; line-height: 1.5; }
    button {
      height: 38px;
      border: 1px solid transparent;
      border-radius: 6px;
      padding: 0 14px;
      background: var(--accent);
      color: white;
      cursor: pointer;
      font-weight: 700;
    }
    button:hover { background: var(--accent-dark); }
    button.secondary {
      background: #fff;
      color: var(--ink);
      border-color: #cbd5e1;
    }
    button.secondary:hover { background: #f8fafc; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .actions { margin-top: 16px; }
    .status-line {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 10px;
    }
    .badge {
      border-radius: 999px;
      padding: 3px 9px;
      background: #e0f2fe;
      color: #075985;
      font-weight: 700;
      font-size: 12px;
    }
    .badge.success { background: #dcfce7; color: #166534; }
    .badge.failed { background: #fee2e2; color: var(--danger); }
    .tabs { display: flex; gap: 8px; margin: 14px 0 10px; }
    .tab {
      background: #fff;
      color: var(--ink);
      border-color: #cbd5e1;
      height: 34px;
      font-weight: 600;
    }
    .tab.active { background: #e6fffb; border-color: #99f6e4; color: var(--accent-dark); }
    pre {
      background: var(--code);
      color: #d1e7ff;
      border-radius: 8px;
      padding: 12px;
      overflow: auto;
      white-space: pre-wrap;
      max-height: 280px;
      line-height: 1.5;
    }
    .report {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      background: #fff;
      max-height: 64vh;
      overflow: auto;
      line-height: 1.7;
    }
    .report h1 { margin: 0 0 14px; font-size: 24px; }
    .report h2 { margin-top: 22px; border-top: 1px solid var(--line); padding-top: 16px; font-size: 18px; }
    .report h3 { font-size: 16px; }
    .report code { background: #f1f5f9; padding: 2px 4px; border-radius: 4px; }
    .history-list { display: grid; gap: 8px; margin-top: 10px; }
    .history-item {
      width: 100%;
      text-align: left;
      height: auto;
      min-height: 42px;
      background: #fff;
      color: var(--ink);
      border-color: #cbd5e1;
      font-weight: 600;
      padding: 8px 10px;
    }
    .path { color: var(--muted); word-break: break-all; font-size: 12px; margin-top: 6px; }
    .error { color: var(--danger); margin-top: 8px; min-height: 18px; }
    @media (max-width: 960px) {
      main { grid-template-columns: 1fr; padding: 12px; }
      header { padding: 14px 12px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>知识星球报告生成</h1>
    <div class="notice">提示：报告生成会把检索到的知识星球内容发送到 DeepSeek API，用于结构化分析和生成 Markdown 报告。</div>
  </header>
  <main>
    <section>
      <h2>生成参数</h2>
      <form id="job-form">
        <label for="company">公司名</label>
        <input id="company" name="company" placeholder="例如：普冉股份" autocomplete="off" required>

        <label for="group-select">星球</label>
        <div class="row">
          <select id="group-select">
            <option value="">加载星球中...</option>
          </select>
          <button type="button" class="secondary" id="refresh-groups">刷新</button>
        </div>
        <div class="grid-2">
          <div>
            <label for="manual-group-id">手动 group_id</label>
            <input id="manual-group-id" placeholder="优先使用">
          </div>
          <div>
            <label for="manual-group-name">手动星球名</label>
            <input id="manual-group-name" placeholder="例如：每日调研">
          </div>
        </div>

        <div class="grid-2">
          <div>
            <label for="days">时间范围</label>
            <input id="days" type="number" min="1" max="3650">
          </div>
          <div>
            <label for="analysis-source">分析来源</label>
            <select id="analysis-source">
              <option value="detail_search">detail + search</option>
              <option value="detail">仅 detail</option>
              <option value="search">仅 search</option>
            </select>
          </div>
        </div>

        <div class="grid-2">
          <div>
            <label for="max-keywords">最大关键词数</label>
            <input id="max-keywords" type="number" min="0" max="500">
          </div>
          <div>
            <label for="max-topics">最大 topic 数</label>
            <input id="max-topics" type="number" min="0" max="10000">
          </div>
        </div>

        <div class="grid-2">
          <div>
            <label for="recent-pages">最近主题流页数</label>
            <input id="recent-pages" type="number" min="0" max="200">
          </div>
          <div>
            <label for="recent-limit">每页数量</label>
            <input id="recent-limit" type="number" min="1" max="30">
          </div>
        </div>
        <div class="hint">最近主题流只用公司名、全称、别名、股票代码等身份类关键词做本地匹配。</div>

        <div class="check-row">
          <label><input id="recent-include-all" type="checkbox"> 最近主题流命中时间范围内全部加入候选</label>
          <div class="hint">这会绕过身份类关键词过滤，可能显著增加 detail 拉取和 DeepSeek 分析成本。</div>
        </div>

        <div class="actions row">
          <button id="submit-button" type="submit">生成报告</button>
          <button id="refresh-history" class="secondary" type="button">刷新历史</button>
        </div>
        <div id="form-error" class="error"></div>
      </form>
    </section>

    <section>
      <div class="status-line">
        <h2>任务与报告</h2>
        <span id="status-badge" class="badge">未运行</span>
      </div>
      <div class="path" id="report-path"></div>
      <a id="bundle-download" style="display:none" download="pro_a_export.zip">Download pro_a bundle</a>
      <pre id="logs">等待提交任务。</pre>

      <div class="tabs">
        <button type="button" id="tab-rendered" class="tab active">渲染视图</button>
        <button type="button" id="tab-raw" class="tab">原始 Markdown</button>
      </div>
      <div id="report-rendered" class="report">报告完成后会显示在这里。</div>
      <pre id="report-raw" style="display:none;"></pre>

      <h3>历史报告</h3>
      <div id="history" class="history-list"></div>
    </section>
  </main>

  <script>
    const state = { jobId: null, pollTimer: null, reportHtml: "", reportMarkdown: "" };
    const $ = (id) => document.getElementById(id);

    function setError(message) { $("form-error").textContent = message || ""; }
    function setStatus(status) {
      const badge = $("status-badge");
      badge.textContent = status;
      badge.className = "badge";
      if (status === "success") badge.classList.add("success");
      if (status === "failed") badge.classList.add("failed");
    }
    function setReport(html, markdown, path, bundleUrl) {
      state.reportHtml = html || "";
      state.reportMarkdown = markdown || "";
      $("report-rendered").innerHTML = state.reportHtml || "暂无报告内容。";
      $("report-raw").textContent = state.reportMarkdown || "";
      $("report-path").textContent = path ? `报告路径：${path}` : "";
      $("bundle-download").href = bundleUrl || "";
      $("bundle-download").style.display = bundleUrl ? "" : "none";
    }
    function showRendered() {
      $("tab-rendered").classList.add("active");
      $("tab-raw").classList.remove("active");
      $("report-rendered").style.display = "";
      $("report-raw").style.display = "none";
    }
    function showRaw() {
      $("tab-rendered").classList.remove("active");
      $("tab-raw").classList.add("active");
      $("report-rendered").style.display = "none";
      $("report-raw").style.display = "";
    }
    async function fetchJson(url, options) {
      const response = await fetch(url, options);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    }
    async function loadDefaults() {
      const defaults = await fetchJson("/api/defaults");
      $("days").value = defaults.days;
      $("max-keywords").value = defaults.max_keywords;
      $("max-topics").value = defaults.max_topics;
      $("recent-pages").value = defaults.recent_pages;
      $("recent-limit").value = defaults.recent_limit;
      $("analysis-source").value = defaults.analysis_source || "detail_search";
    }
    async function loadGroups() {
      const select = $("group-select");
      select.innerHTML = '<option value="">加载中...</option>';
      const data = await fetchJson("/api/groups");
      select.innerHTML = '<option value="">手动输入或选择星球</option>';
      for (const group of data.groups || []) {
        const option = document.createElement("option");
        option.value = group.id;
        option.textContent = `${group.name} (${group.id})`;
        option.dataset.name = group.name;
        select.appendChild(option);
      }
      if (data.error) setError(`星球列表读取失败：${data.error}`);
    }
    async function loadHistory() {
      const data = await fetchJson("/api/reports");
      const root = $("history");
      root.innerHTML = "";
      for (const item of data.reports || []) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "history-item";
        button.textContent = `${item.company} · ${item.group_label || "未知星球"} · ${item.run_name}`;
        button.onclick = () => openReport(item.run_name);
        root.appendChild(button);
      }
      if (!root.children.length) root.textContent = "暂无历史报告。";
    }
    async function openReport(runName) {
      const report = await fetchJson(`/api/reports/${encodeURIComponent(runName)}`);
      setReport(report.html, report.markdown, report.report_path, report.bundle_download);
      setStatus("history");
      $("logs").textContent = `已打开历史报告：${runName}`;
      showRendered();
    }
    function readPayload() {
      const selected = $("group-select").selectedOptions[0];
      const manualGroupId = $("manual-group-id").value.trim();
      const manualGroupName = $("manual-group-name").value.trim();
      return {
        company: $("company").value.trim(),
        group_id: manualGroupId || $("group-select").value,
        group_name: manualGroupId ? "" : (manualGroupName || (selected ? selected.dataset.name || "" : "")),
        days: Number($("days").value),
        max_keywords: Number($("max-keywords").value),
        max_topics: Number($("max-topics").value),
        recent_pages: Number($("recent-pages").value),
        recent_limit: Number($("recent-limit").value),
        analysis_source: $("analysis-source").value,
        recent_include_all: $("recent-include-all").checked
      };
    }
    async function submitJob(event) {
      event.preventDefault();
      setError("");
      $("submit-button").disabled = true;
      try {
        const job = await fetchJson("/api/jobs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(readPayload())
        });
        state.jobId = job.id;
        setStatus(job.status);
        $("logs").textContent = "任务已提交。";
        setReport("", "", "", "");
        pollJob();
      } catch (error) {
        setError(error.message);
        $("submit-button").disabled = false;
      }
    }
    async function pollJob() {
      if (!state.jobId) return;
      try {
        const job = await fetchJson(`/api/jobs/${state.jobId}`);
        setStatus(job.status);
        $("logs").textContent = (job.logs || []).join("\n") || "等待日志。";
        $("logs").scrollTop = $("logs").scrollHeight;
        if (job.report_path) $("report-path").textContent = `报告路径：${job.report_path}`;
        if (job.status === "success") {
          setReport(job.html, job.markdown, job.report_path, job.bundle_download);
          $("submit-button").disabled = false;
          clearTimeout(state.pollTimer);
          loadHistory();
          return;
        }
        if (job.status === "failed") {
          setError(job.error || "任务失败。");
          $("submit-button").disabled = false;
          clearTimeout(state.pollTimer);
          return;
        }
        state.pollTimer = setTimeout(pollJob, 1500);
      } catch (error) {
        setError(error.message);
        $("submit-button").disabled = false;
      }
    }
    $("job-form").addEventListener("submit", submitJob);
    $("refresh-groups").addEventListener("click", () => loadGroups().catch(error => setError(error.message)));
    $("refresh-history").addEventListener("click", () => loadHistory().catch(error => setError(error.message)));
    $("tab-rendered").addEventListener("click", showRendered);
    $("tab-raw").addEventListener("click", showRaw);
    Promise.all([loadDefaults(), loadGroups(), loadHistory()]).catch(error => setError(error.message));
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    host = os.getenv("WEB_HOST", DEFAULT_HOST)
    port = env_int("WEB_PORT", DEFAULT_PORT)
    run_server(host, port)
