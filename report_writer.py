from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any


class ReportWriter:
    def __init__(self, output_dir: str | Path, company: str) -> None:
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = Path(output_dir).expanduser().resolve() / f"{safe_name(company)}_{timestamp}"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def path(self, filename: str) -> Path:
        return self.run_dir / filename

    def write_json(self, filename: str, data: Any) -> Path:
        path = self.path(filename)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def write_jsonl(self, filename: str, rows: list[dict[str, Any]]) -> Path:
        path = self.path(filename)
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def append_jsonl(self, filename: str, row: dict[str, Any]) -> Path:
        path = self.path(filename)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def write_markdown(self, filename: str, markdown: str) -> Path:
        path = self.path(filename)
        path.write_text(markdown, encoding="utf-8")
        return path


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    return cleaned or "report"
