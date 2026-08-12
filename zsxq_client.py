from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ALLOWED_COMMANDS = {
    "auth_status": ("auth", "status"),
    "group_list": ("group", "+list"),
    "group_topics": ("group", "+topics"),
    "topic_search": ("topic", "+search"),
    "topic_detail": ("topic", "+detail"),
}


class ZsxqError(RuntimeError):
    pass


class ZsxqCommandError(ZsxqError):
    def __init__(self, command: list[str], returncode: int, stderr: str) -> None:
        rendered = " ".join(command[:4] + ["..."]) if len(command) > 4 else " ".join(command)
        message = redact_sensitive(stderr).strip()
        if len(message) > 1000:
            message = message[:1000] + "...[truncated]"
        super().__init__(f"zsxq-cli command failed ({returncode}): {rendered}\n{message}")


class ZsxqParseError(ZsxqError):
    pass


@dataclass(frozen=True)
class ZsxqCliConfig:
    cli_command: tuple[str, ...]
    group_list_args: str
    topic_search_args: str
    topic_detail_args: str
    runtime_home: str | None
    timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> "ZsxqCliConfig":
        cli_command = os.getenv("ZSXQ_CLI", "npx --no-install zsxq-cli")
        timeout = int(os.getenv("ZSXQ_TIMEOUT_SECONDS", "60"))
        return cls(
            cli_command=tuple(normalize_cli_command(split_args(cli_command))),
            group_list_args=os.getenv("ZSXQ_GROUP_LIST_ARGS", "--json"),
            topic_search_args=os.getenv(
                "ZSXQ_TOPIC_SEARCH_ARGS",
                "--group-id {group_id} --query {keyword} --json",
            ),
            topic_detail_args=os.getenv(
                "ZSXQ_TOPIC_DETAIL_ARGS",
                "--topic-id {topic_id} --json",
            ),
            runtime_home=os.getenv("ZSXQ_RUNTIME_HOME") or None,
            timeout_seconds=timeout,
        )


class ZsxqClient:
    def __init__(self, config: ZsxqCliConfig | None = None) -> None:
        self.config = config or ZsxqCliConfig.from_env()
        if not self.config.cli_command:
            raise ZsxqError("ZSXQ_CLI is empty")

    def auth_status(self) -> Any:
        return parse_json_output(self._run_allowed("auth_status", []), allow_text=True)

    def list_groups(self) -> list[dict[str, Any]]:
        parsed = parse_json_output(
            self._run_allowed("group_list", render_args(self.config.group_list_args, {}))
        )
        return [item for item in extract_items(parsed) if isinstance(item, dict)]

    def search_topics(self, group_id: str, keyword: str, days: int) -> list[dict[str, Any]]:
        args = render_args(
            self.config.topic_search_args,
            {"group_id": group_id, "keyword": keyword, "days": str(days)},
        )
        parsed = parse_json_output(self._run_allowed("topic_search", args))
        return [item for item in extract_items(parsed) if isinstance(item, dict)]

    def group_topics(
        self, group_id: str, limit: int = 30, end_time: str | None = None
    ) -> dict[str, Any]:
        args = ["--group-id", group_id, "--limit", str(limit), "--json"]
        if end_time:
            args.extend(["--end-time", end_time])
        parsed = parse_json_output(self._run_allowed("group_topics", args))
        return {
            "raw": parsed,
            "items": [item for item in extract_items(parsed) if isinstance(item, dict)],
            "next_end_time": extract_next_end_time(parsed),
            "has_more": extract_bool(parsed, ("has_more", "hasMore")),
        }

    def topic_detail(self, topic_id: str) -> dict[str, Any]:
        args = render_args(self.config.topic_detail_args, {"topic_id": topic_id})
        parsed = parse_json_output(self._run_allowed("topic_detail", args), allow_text=True)
        items = extract_items(parsed)
        if len(items) == 1 and isinstance(items[0], dict):
            return items[0]
        if isinstance(parsed, dict):
            return parsed
        return {"topic_id": topic_id, "raw": parsed}

    def _run_allowed(self, action: str, args: list[str]) -> str:
        if action not in ALLOWED_COMMANDS:
            raise ZsxqError(f"Disallowed zsxq action: {action}")
        command = list(self.config.cli_command) + list(ALLOWED_COMMANDS[action]) + args
        env = self._build_env()
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.timeout_seconds,
                env=env,
            )
        except FileNotFoundError as exc:
            raise ZsxqCommandError(command, 127, str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise ZsxqCommandError(command, 124, f"timeout after {exc.timeout} seconds") from exc

        if result.returncode != 0:
            stderr = result.stderr or f"no stderr; stdout length {len(result.stdout)} omitted"
            raise ZsxqCommandError(command, result.returncode, stderr)
        return result.stdout

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if not self.config.runtime_home:
            return env

        runtime_home = Path(self.config.runtime_home).expanduser().resolve()
        runtime_home.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(runtime_home)
        env["USERPROFILE"] = str(runtime_home)
        env["XDG_CONFIG_HOME"] = str(runtime_home / ".config")
        return env


def split_args(value: str) -> list[str]:
    return shlex.split(value, posix=True)


def normalize_cli_command(parts: list[str]) -> list[str]:
    if not parts:
        return parts
    command = parts[0]
    if os.name == "nt" and ("/" in command or "\\" in command):
        path = Path(command)
        if not path.is_absolute():
            path = Path.cwd() / path
        parts[0] = str(path.resolve())
    return parts


def render_args(template: str, values: dict[str, str]) -> list[str]:
    if not template.strip():
        return []
    parts = split_args(template)
    return [part.format_map(DefaultFormatMap(values)) for part in parts]


class DefaultFormatMap(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def redact_sensitive(text: str) -> str:
    text = re.sub(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{16,}", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((token|cookie|authorization|secret)\s*[:=]\s*)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(deepseek[-_ ]?api[-_ ]?key\s*[:=]\s*)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)([?&]token=)[^&\s]+", r"\1[REDACTED]", text)
    return text


SENSITIVE_KEYWORDS = {
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "secret",
    "password",
}


def sanitize_for_storage(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            if key.lower() in SENSITIVE_KEYWORDS:
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = sanitize_for_storage(child)
        return sanitized
    if isinstance(value, list):
        return [sanitize_for_storage(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive(value)
    return value


def parse_json_output(text: str, allow_text: bool = False) -> Any:
    cleaned = strip_ansi(text).strip("\ufeff \r\n\t")
    if not cleaned:
        return {}

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    objects: list[Any] = []
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if objects:
        return objects
    if allow_text:
        return {"raw_text": cleaned}
    raise ZsxqParseError(
        "zsxq-cli output is not JSON. Adjust ZSXQ_*_ARGS in .env so the CLI returns JSON."
    )


def extract_items(parsed: Any) -> list[Any]:
    if isinstance(parsed, list):
        return flatten_items(parsed)
    if not isinstance(parsed, dict):
        return [parsed]

    found = find_candidate_list(parsed)
    if found is not None:
        return flatten_items(found)
    return [parsed]


def flatten_items(items: list[Any]) -> list[Any]:
    flattened: list[Any] = []
    for item in items:
        if isinstance(item, list):
            flattened.extend(item)
        else:
            flattened.append(item)
    return flattened


def find_candidate_list(obj: Any) -> list[Any] | None:
    if isinstance(obj, dict):
        for key in (
            "topics_brief",
            "topicsBrief",
            "topics",
            "items",
            "list",
            "results",
            "groups",
            "data",
            "records",
        ):
            value = obj.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = find_candidate_list(value)
                if nested is not None:
                    return nested
        for value in obj.values():
            nested = find_candidate_list(value)
            if nested is not None:
                return nested
    elif isinstance(obj, list):
        for value in obj:
            nested = find_candidate_list(value)
            if nested is not None:
                return nested
    return None


def find_first_by_keys(obj: Any, keys: Iterable[str]) -> Any | None:
    key_set = {key.lower() for key in keys}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in key_set and value not in (None, ""):
                return value
        for value in obj.values():
            found = find_first_by_keys(value, key_set)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_first_by_keys(value, key_set)
            if found not in (None, ""):
                return found
    return None


def extract_topic_id(obj: Any) -> str | None:
    for keys in (
        ("topic_id", "topicId", "topicID", "topicid"),
        ("topicNo", "topic_no"),
    ):
        value = find_first_by_keys(obj, keys)
        if value not in (None, ""):
            return str(value)

    if isinstance(obj, dict):
        topic = obj.get("topic")
        if isinstance(topic, dict):
            for key in ("id", "topic_id", "topicId"):
                if topic.get(key) not in (None, ""):
                    return str(topic[key])
        if obj.get("id") not in (None, ""):
            return str(obj["id"])
    rendered = json.dumps(obj, ensure_ascii=False)
    match = re.search(r"(?:topic_id|topicId|topics?/)(?:[=:\"/\s]+)(\d{6,})", rendered)
    if match:
        return match.group(1)
    return None


def extract_group_id(obj: Any) -> str | None:
    value = find_first_by_keys(obj, ("group_id", "groupId", "groupID"))
    if value not in (None, ""):
        return str(value)
    if isinstance(obj, dict) and obj.get("id") not in (None, ""):
        return str(obj["id"])
    return None


def extract_group_name(obj: Any) -> str | None:
    value = find_first_by_keys(obj, ("group_name", "groupName", "name", "title"))
    return None if value in (None, "") else str(value)


def extract_next_end_time(obj: Any) -> str | None:
    value = find_first_by_keys(
        obj,
        (
            "next_end_time",
            "nextEndTime",
            "end_time",
            "endTime",
            "next_cursor",
            "nextCursor",
            "cursor",
        ),
    )
    return None if value in (None, "") else str(value)


def extract_bool(obj: Any, keys: Iterable[str]) -> bool | None:
    value = find_first_by_keys(obj, keys)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return None


def extract_author(obj: Any) -> str:
    value = find_first_by_keys(obj, ("author_name", "authorName", "user_name", "name", "nickname"))
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def extract_published_at(obj: Any) -> str:
    value = find_first_by_keys(
        obj,
        (
            "published_at",
            "publishedAt",
            "create_time",
            "createTime",
            "created_at",
            "createdAt",
            "time",
            "timestamp",
        ),
    )
    parsed = coerce_datetime(value)
    if parsed:
        return parsed.isoformat()
    return "" if value in (None, "") else str(value)


def coerce_datetime(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
        return dt.datetime.fromtimestamp(seconds, tz=dt.UTC)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            return coerce_datetime(int(stripped))
        normalized = stripped.replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(normalized)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(stripped, fmt).replace(tzinfo=dt.UTC)
            except ValueError:
                continue
    return None


def is_within_days(published_at: str, days: int, now: dt.datetime | None = None) -> bool:
    if not published_at:
        return True
    parsed = coerce_datetime(published_at)
    if not parsed:
        return True
    reference = now or dt.datetime.now(tz=dt.UTC)
    return parsed >= reference - dt.timedelta(days=days)


CONTENT_KEYS = {
    "text",
    "content",
    "article",
    "article_content",
    "articlecontent",
    "description",
    "excerpt",
    "title",
    "question",
    "answer",
    "talk",
    "comment",
    "comments",
    "summary",
}


def extract_content_text(obj: Any, max_chars: int) -> str:
    chunks: list[str] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                walk(child_value, child_key)
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str) and key.lower() in CONTENT_KEYS:
            cleaned = normalize_text(value)
            if cleaned and cleaned not in chunks:
                chunks.append(cleaned)

    walk(obj)
    text = "\n\n".join(chunks)
    if not text:
        text = normalize_text(json.dumps(obj, ensure_ascii=False))
    return text[:max_chars]


def normalize_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()
