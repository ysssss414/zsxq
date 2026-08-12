from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from deepseek_client import DeepSeekClient
from report_writer import ReportWriter
from zsxq_client import (
    ZsxqClient,
    ZsxqError,
    coerce_datetime,
    extract_author,
    extract_content_text,
    extract_group_id,
    extract_group_name,
    extract_published_at,
    extract_topic_id,
    find_first_by_keys,
    is_within_days,
    sanitize_for_storage,
)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(".env"))
    args = parse_args(argv)

    if args.days <= 0:
        raise SystemExit("--days must be a positive integer")

    writer = ReportWriter(args.output_dir, args.company)
    writer.write_jsonl("search_raw.jsonl", [])
    writer.write_jsonl("detail_raw.jsonl", [])
    writer.write_jsonl("topic_analysis.jsonl", [])
    writer.write_jsonl("errors.jsonl", [])

    print(f"Output directory: {writer.run_dir}")

    zsxq = ZsxqClient()
    deepseek = DeepSeekClient()

    zsxq.auth_status()
    print("zsxq-cli auth status: OK")

    group_id, group_label = resolve_group(zsxq, args.group_id, args.group_name)
    print(f"Target group: {group_label} ({group_id})")

    keyword_pack = deepseek.generate_keyword_pack(args.company)
    keyword_items = select_keywords(keyword_pack, args.max_keywords)
    keyword_pack["selected_keywords"] = keyword_items
    writer.write_json("keyword_pack.json", keyword_pack)
    print(f"Generated keywords: {len(keyword_items)}")

    searched_at = dt.datetime.now(tz=dt.UTC).isoformat()
    topic_index: dict[str, dict[str, Any]] = {}
    search_count = 0
    search_errors = 0
    recent_scan_stats = {
        "enabled": args.recent_pages > 0,
        "pages_requested": args.recent_pages,
        "pages_fetched": 0,
        "items_seen": 0,
        "items_in_range": 0,
        "items_matched": 0,
        "stopped_out_of_range": False,
        "stopped_no_cursor": False,
    }

    for item in keyword_items:
        keyword = item["keyword"]
        try:
            hits = zsxq.search_topics(group_id=group_id, keyword=keyword, days=args.days)
        except ZsxqError as exc:
            search_errors += 1
            writer.append_jsonl(
                "errors.jsonl",
                {"stage": "search", "keyword": keyword, "error": str(exc)},
            )
            print(f"Search failed for keyword: {keyword}")
            continue

        print(f"Search keyword: {keyword} -> {len(hits)} hits")
        for hit in hits:
            topic_id = extract_topic_id(hit)
            sanitized_hit = sanitize_for_storage(hit)
            raw_record = {
                "stage": "search",
                "keyword": keyword,
                "keyword_type": item.get("type", ""),
                "keyword_reason": item.get("reason", ""),
                "topic_id": topic_id,
                "searched_at": searched_at,
                "raw": sanitized_hit,
            }
            writer.append_jsonl("search_raw.jsonl", raw_record)
            search_count += 1
            if not topic_id:
                continue

            indexed = topic_index.setdefault(
                topic_id,
                {"keyword_sources": [], "search_hits": []},
            )
            if item not in indexed["keyword_sources"]:
                indexed["keyword_sources"].append(item)
            indexed["search_hits"].append(sanitized_hit)

    if args.recent_pages > 0:
        recent_keyword_items = select_recent_scan_keywords(keyword_items)
        scan_recent_topics(
            zsxq=zsxq,
            writer=writer,
            topic_index=topic_index,
            group_id=group_id,
            keyword_items=recent_keyword_items,
            days=args.days,
            pages=args.recent_pages,
            limit=args.recent_limit,
            include_all=args.recent_include_all,
            searched_at=searched_at,
            stats=recent_scan_stats,
        )

    if not topic_index and search_errors:
        raise SystemExit(
            "No topic_id was collected and at least one search failed. See errors.jsonl."
        )

    detail_records: list[dict[str, Any]] = []
    topic_ids, search_window_stats = plan_topic_fetches(topic_index, args.days, args.max_topics)

    print(
        "Unique topics: "
        f"{len(topic_index)}; search-window known in-range: "
        f"{search_window_stats['known_in_range']}; unknown-date: "
        f"{search_window_stats['unknown_date']}; known out-of-range: "
        f"{search_window_stats['known_out_of_range']}; fetching details: {len(topic_ids)}"
    )
    for index, topic_id in enumerate(topic_ids, start=1):
        indexed = topic_index[topic_id]
        try:
            detail = zsxq.topic_detail(topic_id)
        except ZsxqError as exc:
            writer.append_jsonl(
                "errors.jsonl",
                {"stage": "detail", "topic_id": topic_id, "error": str(exc)},
            )
            print(f"Detail failed: {topic_id}")
            continue

        sanitized_detail = sanitize_for_storage(detail)
        published_at = extract_published_at(sanitized_detail) or first_published_at(
            indexed["search_hits"]
        )
        in_range = is_within_days(published_at, args.days)

        record = {
            "stage": "detail",
            "topic_id": topic_id,
            "published_at": published_at,
            "in_range": in_range,
            "author": extract_author(sanitized_detail) or first_author(indexed["search_hits"]),
            "keyword_sources": indexed["keyword_sources"],
            "search_hits": indexed["search_hits"],
            "detail": sanitized_detail,
        }
        writer.append_jsonl("detail_raw.jsonl", record)
        detail_records.append(record)
        if index % 10 == 0 or index == len(topic_ids):
            print(f"Fetched details: {index}/{len(topic_ids)}")

    analysis_records = [record for record in detail_records if record.get("in_range", True)]
    if detail_records and not analysis_records:
        print(f"No details within the last {args.days} days; report will note empty sample.")
    ai_topics = [
        {
            "topic_id": record["topic_id"],
            "published_at": record.get("published_at", ""),
            "author": record.get("author", ""),
            "keyword_sources": record.get("keyword_sources", []),
            "content": build_analysis_content(
                record, deepseek.config.max_topic_chars, args.analysis_source
            ),
        }
        for record in analysis_records
    ]

    analyses = deepseek.analyze_topics(args.company, ai_topics) if ai_topics else []
    writer.write_jsonl("topic_analysis.jsonl", analyses)
    print(f"Analyzed topics: {len(analyses)}")

    markdown = deepseek.summarize_markdown(
        company=args.company,
        group_label=group_label,
        days=args.days,
        keyword_pack=keyword_pack,
        analyses=analyses,
    )
    report_path = writer.write_markdown("report.md", markdown)

    writer.write_json(
        "run_metadata.json",
        {
            "company": args.company,
            "group_id": group_id,
            "group_label": group_label,
            "days": args.days,
            "max_keywords": args.max_keywords,
            "max_topics": args.max_topics,
            "recent_pages": args.recent_pages,
            "recent_limit": args.recent_limit,
            "recent_include_all": args.recent_include_all,
            "analysis_source": args.analysis_source,
            "search_hits": search_count,
            "unique_topics": len(topic_index),
            "search_window_stats": search_window_stats,
            "recent_scan_stats": recent_scan_stats,
            "details_saved": len(detail_records),
            "details_in_range": len(analysis_records),
            "analyses_saved": len(analyses),
            "search_errors": search_errors,
            "generated_at": dt.datetime.now(tz=dt.UTC).isoformat(),
            "files": {
                "keyword_pack": "keyword_pack.json",
                "search_raw": "search_raw.jsonl",
                "detail_raw": "detail_raw.jsonl",
                "topic_analysis": "topic_analysis.jsonl",
                "report": "report.md",
                "errors": "errors.jsonl",
            },
        },
    )

    print(f"Markdown report: {report_path}")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search read-only zsxq topics for an A-share company and summarize with DeepSeek."
    )
    parser.add_argument("--company", required=True, help="A 股公司名")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--group-id", help="目标知识星球 group_id")
    group.add_argument("--group-name", help="目标知识星球名称，工具会用 group +list 解析")
    parser.add_argument("--days", type=int, default=30, help="时间范围，默认 30 天")
    parser.add_argument("--output-dir", required=True, help="输出目录")
    parser.add_argument(
        "--max-keywords",
        type=int,
        default=int(os.getenv("MAX_KEYWORDS", "40")),
        help="最多搜索多少个 DeepSeek 关键词，默认 40",
    )
    parser.add_argument(
        "--max-topics",
        type=int,
        default=int(os.getenv("MAX_TOPICS", "0")),
        help="最多拉取多少条 topic detail，0 表示不限",
    )
    parser.add_argument(
        "--recent-pages",
        type=int,
        default=int(os.getenv("RECENT_PAGES", "0")),
        help="额外扫描最近主题流页数，0 表示关闭；每页最多 30 条",
    )
    parser.add_argument(
        "--recent-limit",
        type=int,
        default=int(os.getenv("RECENT_LIMIT", "30")),
        help="最近主题流每页拉取数量，1-30，默认 30",
    )
    parser.add_argument(
        "--recent-include-all",
        action="store_true",
        help="最近主题流中 days 范围内的 topic 全部加入候选，不只加入命中关键词的 topic",
    )
    parser.add_argument(
        "--analysis-source",
        choices=("detail_search", "detail", "search"),
        default=os.getenv("ANALYSIS_SOURCE", "detail_search"),
        help="DeepSeek 分析输入来源：detail_search=detail+搜索上下文；detail=仅 detail；search=仅搜索上下文",
    )
    return parser.parse_args(argv)


def resolve_group(
    zsxq: ZsxqClient, group_id: str | None, group_name: str | None
) -> tuple[str, str]:
    if group_id:
        return group_id, group_name or group_id
    assert group_name is not None

    groups = zsxq.list_groups()
    exact: list[tuple[str, str]] = []
    fuzzy: list[tuple[str, str]] = []
    wanted = group_name.casefold()

    for group in groups:
        candidate_id = extract_group_id(group)
        candidate_name = extract_group_name(group)
        if not candidate_id or not candidate_name:
            continue
        pair = (candidate_id, candidate_name)
        if candidate_name.casefold() == wanted:
            exact.append(pair)
        elif wanted in candidate_name.casefold():
            fuzzy.append(pair)

    matches = exact or fuzzy
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        choices = ", ".join(f"{name}({gid})" for gid, name in matches[:10])
        raise SystemExit(f"Multiple groups matched '{group_name}'. Use --group-id. Matches: {choices}")

    visible = ", ".join(
        f"{extract_group_name(group)}({extract_group_id(group)})"
        for group in groups[:10]
        if extract_group_name(group) and extract_group_id(group)
    )
    raise SystemExit(f"No group matched '{group_name}'. First groups: {visible}")


def select_keywords(keyword_pack: dict[str, Any], limit: int) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in keyword_pack.get("keywords", []):
        if isinstance(item, str):
            normalized = {"keyword": item, "type": "other", "reason": ""}
        elif isinstance(item, dict):
            normalized = {
                "keyword": str(item.get("keyword", "")).strip(),
                "type": str(item.get("type", "other")).strip(),
                "reason": str(item.get("reason", "")).strip(),
            }
        else:
            continue
        if not normalized["keyword"] or normalized["keyword"] in seen:
            continue
        seen.add(normalized["keyword"])
        selected.append(normalized)
        if limit > 0 and len(selected) >= limit:
            break
    return selected


RECENT_SCAN_KEYWORD_TYPES = {"input_company", "official_name", "alias", "stock_code"}


def select_recent_scan_keywords(keyword_items: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        item
        for item in keyword_items
        if item.get("type", "").strip().casefold() in RECENT_SCAN_KEYWORD_TYPES
    ]


def first_published_at(hits: list[dict[str, Any]]) -> str:
    for hit in hits:
        value = extract_published_at(hit)
        if value:
            return value
    return ""


def plan_topic_fetches(
    topic_index: dict[str, dict[str, Any]], days: int, max_topics: int
) -> tuple[list[str], dict[str, int]]:
    planned: list[tuple[int, float, int, str]] = []
    stats = {
        "known_in_range": 0,
        "unknown_date": 0,
        "known_out_of_range": 0,
        "total_before_limit": len(topic_index),
        "total_after_limit": 0,
    }

    for order, (topic_id, indexed) in enumerate(topic_index.items()):
        published_at = first_published_at(indexed.get("search_hits", []))
        parsed = coerce_datetime(published_at)
        if parsed:
            if is_within_days(published_at, days):
                priority = 0
                stats["known_in_range"] += 1
            else:
                priority = 2
                stats["known_out_of_range"] += 1
            sort_time = parsed.timestamp()
        else:
            priority = 1
            stats["unknown_date"] += 1
            sort_time = 0.0
        planned.append((priority, -sort_time, order, topic_id))

    planned.sort()
    topic_ids = [topic_id for _, _, _, topic_id in planned]
    if max_topics > 0:
        topic_ids = topic_ids[:max_topics]
    stats["total_after_limit"] = len(topic_ids)
    return topic_ids, stats


def build_analysis_content(record: dict[str, Any], max_chars: int, source: str) -> str:
    detail_text = extract_content_text(record.get("detail", {}), max_chars)
    search_texts: list[str] = []
    for hit in record.get("search_hits", []):
        text = extract_content_text(hit, 1200)
        if text and text not in search_texts:
            search_texts.append(text)

    parts = []
    if source in {"search", "detail_search"} and search_texts:
        parts.append("【search hit context】\n" + "\n\n".join(search_texts))
    if source in {"detail", "detail_search"} and detail_text:
        parts.append("【topic detail】\n" + detail_text)
    return "\n\n".join(parts)[:max_chars]


def scan_recent_topics(
    zsxq: ZsxqClient,
    writer: ReportWriter,
    topic_index: dict[str, dict[str, Any]],
    group_id: str,
    keyword_items: list[dict[str, str]],
    days: int,
    pages: int,
    limit: int,
    include_all: bool,
    searched_at: str,
    stats: dict[str, Any],
) -> None:
    limit = min(30, max(1, limit))
    end_time: str | None = None
    for page in range(1, pages + 1):
        try:
            page_data = zsxq.group_topics(group_id=group_id, limit=limit, end_time=end_time)
        except ZsxqError as exc:
            writer.append_jsonl(
                "errors.jsonl",
                {"stage": "recent_scan", "page": page, "error": str(exc)},
            )
            print(f"Recent scan failed on page {page}")
            return

        stats["pages_fetched"] += 1
        items = page_data["items"]
        if not items:
            stats["stopped_no_cursor"] = True
            return

        page_has_in_range = False
        for hit in items:
            sanitized_hit = sanitize_for_storage(hit)
            published_at = extract_published_at(sanitized_hit)
            in_range = is_within_days(published_at, days)
            if in_range:
                page_has_in_range = True
                stats["items_in_range"] += 1
            stats["items_seen"] += 1

            matched_sources = match_keyword_sources(sanitized_hit, keyword_items)
            should_keep = in_range and (include_all or bool(matched_sources))
            topic_id = extract_topic_id(sanitized_hit)
            writer.append_jsonl(
                "search_raw.jsonl",
                {
                    "stage": "recent_scan",
                    "page": page,
                    "matched": should_keep,
                    "matched_keywords": [item["keyword"] for item in matched_sources],
                    "topic_id": topic_id,
                    "published_at": published_at,
                    "searched_at": searched_at,
                    "raw": sanitized_hit,
                },
            )
            if not should_keep or not topic_id:
                continue

            stats["items_matched"] += 1
            indexed = topic_index.setdefault(
                topic_id,
                {"keyword_sources": [], "search_hits": []},
            )
            sources = matched_sources or [
                {
                    "keyword": "__recent_in_range__",
                    "type": "recent_scan",
                    "reason": "recent topic within requested days",
                }
            ]
            for source in sources:
                if source not in indexed["keyword_sources"]:
                    indexed["keyword_sources"].append(source)
            indexed["search_hits"].append(sanitized_hit)

        end_time = page_data.get("next_end_time") or newest_cursor_from_items(items)
        if not end_time:
            stats["stopped_no_cursor"] = True
            return
        if not page_has_in_range:
            stats["stopped_out_of_range"] = True
            return

    print(
        "Recent scan: "
        f"pages={stats['pages_fetched']}, seen={stats['items_seen']}, "
        f"in_range={stats['items_in_range']}, matched={stats['items_matched']}"
    )


def match_keyword_sources(
    hit: dict[str, Any], keyword_items: list[dict[str, str]]
) -> list[dict[str, str]]:
    haystack = build_match_text(hit)
    matched: list[dict[str, str]] = []
    for item in keyword_items:
        keyword = item.get("keyword", "").strip()
        if not keyword:
            continue
        if keyword_matches(haystack, keyword):
            matched.append(item)
    return matched


def build_match_text(hit: dict[str, Any]) -> str:
    text = extract_content_text(hit, 3000)
    text += "\n" + json.dumps(hit, ensure_ascii=False)
    return normalize_for_match(text)


def normalize_for_match(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def keyword_matches(haystack: str, keyword: str) -> bool:
    normalized = normalize_for_match(keyword)
    if not normalized:
        return False
    return normalized in haystack


def newest_cursor_from_items(items: list[dict[str, Any]]) -> str | None:
    for hit in reversed(items):
        raw_value = find_first_by_keys(
            hit,
            (
                "create_time",
                "createTime",
                "published_at",
                "publishedAt",
                "created_at",
                "createdAt",
                "time",
            ),
        )
        if raw_value not in (None, ""):
            return str(raw_value)
    return None


def first_author(hits: list[dict[str, Any]]) -> str:
    for hit in hits:
        value = extract_author(hit)
        if value:
            return value
    return ""


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = strip_matching_quotes(value.strip())
        if key and key not in os.environ:
            os.environ[key] = value


def strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
