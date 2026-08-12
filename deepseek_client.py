from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from zsxq_client import redact_sensitive


CATEGORIES = {"订单", "业绩", "产业链", "技术", "政策", "市场情绪", "传闻", "其他"}
CREDIBILITY = {"A", "B", "C", "D"}


class DeepSeekError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str
    chat_url: str
    model: str = "deepseek-chat"
    timeout_seconds: int = 90
    batch_size: int = 10
    max_topic_chars: int = 6000
    temperature: float = 0.1

    @classmethod
    def from_env(cls) -> "DeepSeekConfig":
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise DeepSeekError("DEEPSEEK_API_KEY is missing. Put it in .env.")

        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        chat_url = os.getenv("DEEPSEEK_CHAT_COMPLETIONS_URL", f"{base_url}/chat/completions")
        return cls(
            api_key=api_key,
            chat_url=chat_url,
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            timeout_seconds=int(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "90")),
            batch_size=max(1, int(os.getenv("DEEPSEEK_BATCH_SIZE", "10"))),
            max_topic_chars=max(500, int(os.getenv("DEEPSEEK_MAX_TOPIC_CHARS", "6000"))),
            temperature=float(os.getenv("DEEPSEEK_TEMPERATURE", "0.1")),
        )


class DeepSeekClient:
    def __init__(self, config: DeepSeekConfig | None = None) -> None:
        self.config = config or DeepSeekConfig.from_env()

    def generate_keyword_pack(self, company: str) -> dict[str, Any]:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是严谨的 A 股产业研究员。根据公司名生成知识星球检索关键词包，"
                    "只返回 JSON，不要 Markdown。关键词要覆盖公司全称、简称、股票代码、"
                    "核心产品、行业词、竞品、客户、供应商、产业链上下游和常见别称。"
                    "不确定的信息可以少写，不要编造非常具体的合同或客户。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "公司名："
                    + company
                    + "\n返回格式："
                    + json.dumps(
                        {
                            "company": company,
                            "keywords": [
                                {
                                    "keyword": "关键词",
                                    "type": "official_name|alias|stock_code|product|industry|competitor|customer|supplier|other",
                                    "reason": "为什么搜索它",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    )
                ),
            },
        ]
        parsed = self._json_chat(messages)
        keywords = parsed.get("keywords") if isinstance(parsed, dict) else None
        if not isinstance(keywords, list):
            parsed = {"company": company, "keywords": []}
        parsed.setdefault("company", company)
        parsed["keywords"] = normalize_keywords(company, parsed.get("keywords", []))
        return parsed

    def analyze_topics(self, company: str, topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
        analyses: list[dict[str, Any]] = []
        for batch in chunked(topics, self.config.batch_size):
            payload = [
                {
                    "topic_id": item.get("topic_id", ""),
                    "published_at": item.get("published_at", ""),
                    "author": item.get("author", ""),
                    "keyword_sources": item.get("keyword_sources", []),
                    "content": str(item.get("content", ""))[: self.config.max_topic_chars],
                }
                for item in batch
            ]
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是 A 股事件研究分析师。任务是逐条判断知识星球内容是否值得进入报告，"
                        "并做结构化摘要。输入可能同时包含 topic detail 与 search hit context；"
                        "search hit context 是搜索命中页看到的上下文，优先级不低于 topic detail；"
                        "如果 detail 正文较短或未命中目标公司，但 search hit context 显示了目标公司相关信息，"
                        "必须基于 search hit context 摘要，不要被 detail 稀释。不要复述或输出原文全文。"
                        "报告准入标准：只要内容明确出现目标公司全称、简称、股票代码、英文名，或在机构推荐、标的池、"
                        "板块逻辑、产业链/竞品比较中对目标公司有可用于投研判断的提及，就设置 reportable=true。"
                        "完全没有目标公司或别名，且无法建立产业链/竞品/客户/政策关系的，设置 reportable=false，relevance_level=无实质关联。"
                        "信息强度：强相关=公司层面订单、业绩、政策、技术、客户、明确推荐等；"
                        "中等相关=板块逻辑中明确影响公司或把公司作为核心/受益标的；"
                        "弱相关=仅标的池、名单、情绪或间接映射；无实质关联=不展示。"
                        "可信度评级：A=有明确来源或可核验数据；B=逻辑较强但需要二次验证；"
                        "C=弱信号或间接相关；D=传闻、情绪或无法核验。只返回 JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "目标公司："
                        + company
                        + "\n分类只能使用：订单、业绩、产业链、技术、政策、市场情绪、传闻、其他。"
                        + "\n返回格式："
                        + json.dumps(
                            {
                                "items": [
                                    {
                                        "topic_id": "原 topic_id",
                                        "reportable": True,
                                        "is_strongly_related": True,
                                        "relevance_level": "强相关|中等相关|弱相关|无实质关联",
                                        "category": "订单",
                                        "credibility": "A",
                                        "title": "招商电新：明确推荐阳光电源",
                                        "summary": "一句到三句摘要",
                                        "impact": "对公司或市场情绪的具体影响",
                                        "evidence_points": ["可核验要点"],
                                        "risk_notes": ["不确定性或噪声"],
                                        "verification_items": ["后续验证动作"],
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                        + "\n待分析内容："
                        + json.dumps(payload, ensure_ascii=False)
                    ),
                },
            ]
            parsed = self._json_chat(messages)
            items = parsed.get("items", []) if isinstance(parsed, dict) else []
            analyses.extend(normalize_analysis_items(items, batch))
        return analyses

    def summarize_markdown(
        self,
        company: str,
        group_label: str,
        days: int,
        keyword_pack: dict[str, Any],
        analyses: list[dict[str, Any]],
    ) -> str:
        reportable_analyses = filter_reportable_analyses(company, analyses)
        if not reportable_analyses:
            return build_empty_report(company, group_label, days, keyword_pack)

        compact_keywords = [
            {
                "keyword": item.get("keyword", ""),
                "type": item.get("type", ""),
                "reason": item.get("reason", ""),
            }
            for item in keyword_pack.get("selected_keywords", keyword_pack.get("keywords", []))
        ]
        payload = {
            "company": company,
            "group": group_label,
            "days": days,
            "keywords": compact_keywords,
            "analyses": reportable_analyses,
            "omitted_unrelated_count": len(analyses) - len(reportable_analyses),
        }
        messages = [
            {
                "role": "system",
                    "content": (
                        "你是严谨的 A 股投研助理。根据结构化材料生成 Markdown 报告。"
                        "只使用给定材料，不要编造。不要输出原文全文。"
                        "保留 topic_id、发布时间、作者、关键词来源用于附录。"
                        "输入 analyses 已经过滤掉无实质关联条目；不要再展示无关内容，也不要写“与公司无关”的条目。"
                        "弱相关、间接相关、标的池提及可展示，但需明确标注信息强度和可信度。"
                        "分条信息汇总必须按信息内容聚合，同类政策、订单、业绩、机构推荐、板块逻辑等放在一起。"
                        "报告风格要简洁，接近投研纪要：核心结论用一段话；高频主题用短 bullet；"
                        "分条信息汇总用二级标题聚合同类内容，三级标题写信息标题；不要使用表格。"
                    ),
            },
            {
                "role": "user",
                "content": (
                    "请输出 Markdown 报告，必须包含以下一级标题且顺序一致：\n"
                    "# 核心结论\n"
                    "# 高频主题\n"
                    "# 分条信息汇总\n"
                    "# 待验证清单\n"
                    "# 附录：topic_id、发布时间、作者、关键词来源\n\n"
                    "分类：订单、业绩、产业链、技术、政策、市场情绪、传闻、其他。可信度评级：A/B/C/D。"
                    "固定排版样式如下，必须模仿：\n"
                    "# 核心结论\n"
                    "一段话概括最近 N 天最重要结论。\n\n"
                    "# 高频主题\n"
                    "- 主题1\n"
                    "- 主题2\n\n"
                    "# 分条信息汇总\n"
                    "## 内容主题，例如：机构推荐与标的池\n"
                    "### 信息标题\n"
                    "- 发布时间：YYYY-MM-DD\n"
                    "- 概述：一到三句话，说明事实和逻辑。\n"
                    "- 信息强度与可信度：强相关/中等相关/弱相关 / 可信度A/B/C/D\n"
                    "- 具体影响：对目标公司订单、业绩、产业链、政策、市场情绪或验证方向的影响。\n\n"
                    "# 待验证清单\n"
                    "- 验证事项\n\n"
                    "# 附录：topic_id、发布时间、作者、关键词来源\n"
                    "- topic_id: xxx, 发布时间: YYYY-MM-DD, 作者: xxx, 关键词来源: xxx\n\n"
                    "分条信息汇总按内容主题合并，主题下每条必须使用以下格式：\n"
                    "## 主题名称，例如：机构推荐与标的池\n"
                    "### 信息标题\n"
                    "- 发布时间：YYYY-MM-DD\n"
                    "- 概述：一到三句话，说明事实和逻辑。\n"
                    "- 信息强度与可信度：强相关/中等相关/弱相关 / 可信度A/B/C/D\n"
                    "- 具体影响：对目标公司订单、业绩、产业链、政策、市场情绪或验证方向的影响。\n"
                    "不要展示 reportable=false 或 relevance_level=无实质关联 的条目。"
                    "不要写“该条与公司无关”作为分条内容。"
                    "\n结构化材料："
                    + json.dumps(payload, ensure_ascii=False)
                ),
            },
        ]
        content = self._chat(messages, json_mode=False)
        return normalize_report_sections(strip_markdown_fence(content).strip()) + "\n"

    def _json_chat(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        content = self._chat(messages, json_mode=True)
        parsed = extract_json(content)
        if isinstance(parsed, list):
            return {"items": parsed}
        if not isinstance(parsed, dict):
            raise DeepSeekError("DeepSeek did not return a JSON object")
        return parsed

    def _chat(self, messages: list[dict[str, str]], json_mode: bool) -> str:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.config.chat_url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.timeout_seconds
                ) as response:
                    response_data = json.loads(response.read().decode("utf-8"))
                return response_data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if json_mode and exc.code in (400, 422):
                    return self._chat(messages, json_mode=False)
                last_error = DeepSeekError(
                    f"DeepSeek HTTP {exc.code}: {redact_sensitive(body)[:1000]}"
                )
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))

        raise DeepSeekError(f"DeepSeek request failed: {redact_sensitive(str(last_error))}")


def normalize_keywords(company: str, items: Any) -> list[dict[str, str]]:
    seen: set[str] = set()
    normalized: list[dict[str, str]] = []

    def add(keyword: str, kind: str = "other", reason: str = "") -> None:
        keyword = str(keyword).strip()
        if not keyword or keyword in seen:
            return
        seen.add(keyword)
        normalized.append({"keyword": keyword, "type": kind or "other", "reason": reason})

    add(company, "input_company", "用户输入的公司名")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                add(
                    str(item.get("keyword", "")).strip(),
                    str(item.get("type", "other")).strip(),
                    str(item.get("reason", "")).strip(),
                )
    return normalized


def normalize_analysis_items(items: Any, original_batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(item.get("topic_id", "")): item for item in original_batch}
    output: list[dict[str, Any]] = []

    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            topic_id = str(item.get("topic_id", ""))
            original = by_id.get(topic_id, {})
            category = str(item.get("category", "其他")).strip()
            credibility = str(item.get("credibility", "D")).strip().upper()[:1]
            output.append(
                {
                    "topic_id": topic_id,
                    "published_at": original.get("published_at", ""),
                    "author": original.get("author", ""),
                    "keyword_sources": original.get("keyword_sources", []),
                    "reportable": as_bool(item.get("reportable", item.get("is_strongly_related", False))),
                    "is_strongly_related": as_bool(item.get("is_strongly_related", False)),
                    "relevance_level": str(item.get("relevance_level", "")).strip(),
                    "category": category if category in CATEGORIES else "其他",
                    "credibility": credibility if credibility in CREDIBILITY else "D",
                    "title": str(item.get("title", "")).strip(),
                    "summary": str(item.get("summary", "")).strip(),
                    "impact": str(item.get("impact", "")).strip(),
                    "evidence_points": as_string_list(item.get("evidence_points")),
                    "risk_notes": as_string_list(item.get("risk_notes")),
                    "verification_items": as_string_list(item.get("verification_items")),
                }
            )

    returned_ids = {item["topic_id"] for item in output}
    for original in original_batch:
        topic_id = str(original.get("topic_id", ""))
        if topic_id and topic_id not in returned_ids:
            output.append(
                {
                    "topic_id": topic_id,
                    "published_at": original.get("published_at", ""),
                    "author": original.get("author", ""),
                    "keyword_sources": original.get("keyword_sources", []),
                    "reportable": False,
                    "is_strongly_related": False,
                    "relevance_level": "无实质关联",
                    "category": "其他",
                    "credibility": "D",
                    "title": "",
                    "summary": "DeepSeek 未返回该条内容的结构化判断。",
                    "impact": "",
                    "evidence_points": [],
                    "risk_notes": ["模型返回缺失，需要人工复核。"],
                    "verification_items": ["打开 detail jsonl 中对应 topic_id 复查。"],
                }
            )
    return output


def as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value).strip()]


def filter_reportable_analyses(company: str, analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reportable: list[dict[str, Any]] = []

    for item in analyses:
        if item.get("relevance_level") == "无实质关联":
            continue
        explicit = item.get("reportable")
        if explicit is True:
            reportable.append(item)
            continue
        if explicit is False:
            text = " ".join(
                str(item.get(key, ""))
                for key in ("title", "summary", "impact", "category")
            )
            if not has_substantive_company_signal(company, text):
                continue
            if item.get("is_strongly_related"):
                reportable.append(item)
            continue

        text = " ".join(
            str(item.get(key, "")) for key in ("title", "summary", "impact", "category")
        )
        if not has_substantive_company_signal(company, text):
            continue
        reportable.append(item)
    return reportable


def has_substantive_company_signal(company: str, text: str) -> bool:
    negative_patterns = (
        "无关",
        "未涉及",
        "不涉及",
        "未提及",
        "未包含",
        "无实质关联",
        "完全没有",
        "无法建立",
    )
    if not any(pattern in text for pattern in negative_patterns):
        return True

    company_terms = {company, company.replace("股份有限公司", ""), "阳光电源", "阳光", "Sungrow"}
    company_terms = {term for term in company_terms if term}
    action_terms = ("推荐", "列为", "列举", "包含", "标的", "受益", "看好", "明确")
    for company_term in company_terms:
        if re.search(rf"(未提及|未涉及|不涉及|未包含).{{0,10}}{re.escape(company_term)}", text):
            continue
        for action_term in action_terms:
            if re.search(
                rf"({re.escape(action_term)}.{{0,20}}{re.escape(company_term)}|{re.escape(company_term)}.{{0,20}}{re.escape(action_term)})",
                text,
            ):
                return True
    return False


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1", "是", "强相关"}
    return bool(value)


def extract_json(text: str) -> Any:
    cleaned = strip_markdown_fence(text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(cleaned):
            if char not in "{[":
                continue
            try:
                value, _ = decoder.raw_decode(cleaned[index:])
                return value
            except json.JSONDecodeError:
                continue
    raise DeepSeekError("Cannot parse JSON from DeepSeek response")


def strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines)
    return text


REPORT_SECTION_ORDER = [
    "核心结论",
    "高频主题",
    "分条信息汇总",
    "待验证清单",
    "附录：topic_id、发布时间、作者、关键词来源",
]


def normalize_report_sections(markdown: str) -> str:
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer, current
        if current:
            sections[current] = "\n".join(buffer).strip()
        buffer = []

    for line in markdown.splitlines():
        if line.startswith("# "):
            flush()
            title = line[2:].strip()
            current = title
            continue
        if current:
            buffer.append(line)
    flush()

    if not sections:
        return markdown.strip()

    output: list[str] = []
    for title in REPORT_SECTION_ORDER:
        body = sections.pop(title, "").strip()
        output.append(f"# {title}")
        output.append(body if body else "- 无。")
        output.append("")

    for title, body in sections.items():
        output.append(f"# {title}")
        output.append(body.strip() or "- 无。")
        output.append("")

    return "\n".join(output).strip()


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def build_empty_report(
    company: str, group_label: str, days: int, keyword_pack: dict[str, Any]
) -> str:
    keywords = [
        str(item.get("keyword", "")).strip()
        for item in keyword_pack.get("selected_keywords", keyword_pack.get("keywords", []))
        if isinstance(item, dict) and str(item.get("keyword", "")).strip()
    ]
    keyword_text = "、".join(keywords) if keywords else "无"
    return (
        "# 核心结论\n\n"
        f"在目标星球 `{group_label}` 最近 {days} 天范围内，未获得可进入分析的 `{company}` 强相关 topic。"
        "当前样本不足，不能形成订单、业绩、产业链、技术、政策或情绪方面的结论。\n\n"
        "# 高频主题\n\n"
        "- 无可统计主题。\n\n"
        "# 分条信息汇总\n\n"
        "- 无可分析条目。\n\n"
        "# 待验证清单\n\n"
        f"- 放宽 `days` 时间范围后重跑，确认是否存在较早的 `{company}` 相关内容。\n"
        "- 增加 `--max-keywords` 或调整关键词包，覆盖更多别称、产品和产业链词。\n"
        "- 如搜索结果存在但均超出时间范围，可人工查看 `detail_raw.jsonl` 中 `in_range=false` 的条目。\n\n"
        "# 附录：topic_id、发布时间、作者、关键词来源\n\n"
        "- 无。"
        f"\n- 本次关键词：{keyword_text}\n"
    )
