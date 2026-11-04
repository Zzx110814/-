#!/usr/bin/env python3
"""Build a traceable, clause-level draft graph from PDFs in the repository.

This is deterministic text extraction, not legal interpretation or LLM summarization.
Scanned/image-only pages are reported as having no extractable text and require OCR.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "knowledge-base" / "graph.json"

# A lightweight, editable vocabulary. Matches are evidence-based mentions only;
# they do not assert that a document regulates or legally applies to a topic.
TOPICS = [
    "安全生产", "生产安全事故", "事故分类", "事故编码", "重大事故隐患",
    "危险与可操作性分析", "危险源", "风险评价", "风险评估", "风险管理",
    "建设项目", "安全设施", "三同时", "职业病", "职业病防护",
    "环境风险", "应急预案", "应急管理", "化学品", "危险化学品",
    "防爆", "爆炸性环境", "管道", "压力管道", "眼面部防护",
    "应急喷淋", "洗眼设备", "个体防护装备", "劳动防护用品",
]

# Recognizes common Chinese/GB standards identifiers in extracted text.
STANDARD_REF = re.compile(
    r"(?<![A-Z0-9])(?:GB\s*/\s*T|GB/T|GB|AQ\s*/\s*T|AQ/T|AQ|HG\s*/\s*T|HG/T|"
    r"JGJ|JJG|DL\s*/\s*T|DL/T)\s*\d+(?:\.\d+)*(?:[-— ]\d{4})?(?!\d)",
    re.IGNORECASE,
)
# Clause heading candidates, e.g. 4.1.2 Requirements; keep only heading text,
# never copy whole provision bodies into the generated graph.
CLAUSE_LINE = re.compile(r"^\s*((?:\d+\.)+\d+|\d+)\s*[、.． ]+\s*(\S.{1,100})\s*$")
ARTICLE_LINE = re.compile(r"^\s*(第[一二三四五六七八九十百千万零〇\d]+条)\s*(.{1,100})\s*$")


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_pdf(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], bool]:
    reader = PdfReader(str(path))
    clauses: list[dict[str, Any]] = []
    mentions: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    has_text = False
    seen_clause: set[tuple[int, str, str]] = set()
    seen_ref: set[tuple[int, str]] = set()
    seen_topic: set[tuple[int, str]] = set()

    for page_no, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            has_text = True
        lines = [norm(line) for line in text.splitlines() if norm(line)]
        for line in lines:
            match = CLAUSE_LINE.match(line)
            if match:
                number, heading = match.groups()
                heading = norm(heading).strip(" ：:；;")
                key = (page_no, number, heading)
                if heading and key not in seen_clause:
                    seen_clause.add(key)
                    clauses.append({"page": page_no, "clause": number, "heading": heading[:160]})
            article = ARTICLE_LINE.match(line)
            if article:
                number, heading = article.groups()
                heading = norm(heading).strip(" ：:；;")
                key = (page_no, number, heading)
                if heading and key not in seen_clause:
                    seen_clause.add(key)
                    clauses.append({"page": page_no, "clause": number, "heading": heading[:160]})

        for match in STANDARD_REF.finditer(text):
            identifier = norm(match.group(0)).replace(" ", "")
            key = (page_no, identifier.upper())
            if key not in seen_ref:
                seen_ref.add(key)
                refs.append({"page": page_no, "identifier": identifier})

        for topic in TOPICS:
            if topic in text:
                key = (page_no, topic)
                if key not in seen_topic:
                    seen_topic.add(key)
                    mentions.append({"page": page_no, "topic": topic})

    return clauses, mentions, refs, has_text


def main() -> None:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    documents: dict[str, dict[str, Any]] = {}
    topic_ids: set[str] = set()
    ref_ids: set[str] = set()

    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        rel = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        doc_id = "doc:" + rel
        doc: dict[str, Any] = {
            "id": doc_id,
            "type": "document",
            "title": path.stem,
            "path": rel,
            "format": "PDF",
            "sha": git_blob_sha(data),
            "size_bytes": len(data),
        }
        documents[doc_id] = doc
        nodes.append(doc)
        try:
            clauses, mentions, refs, has_text = extract_pdf(path)
            for item in clauses:
                item_id = f"item:{rel}#{item['clause']}@p{item['page']}"
                nodes.append({
                    "id": item_id,
                    "type": "provision_heading",
                    "document_id": doc_id,
                    "clause": item["clause"],
                    "heading": item["heading"],
                    "page": item["page"],
                    "extraction_method": "pypdf text + clause-heading pattern",
                    "review_status": "machine_extracted_unverified",
                })
                edges.append({"source": doc_id, "target": item_id, "type": "contains_heading", "page": item["page"]})
                for topic in TOPICS:
                    if topic in item["heading"]:
                        tid = "topic:" + topic
                        topic_ids.add(tid)
                        edges.append({"source": item_id, "target": tid, "type": "heading_mentions_topic", "page": item["page"]})
            for mention in mentions:
                tid = "topic:" + mention["topic"]
                topic_ids.add(tid)
                edges.append({
                    "source": doc_id, "target": tid, "type": "text_mentions_topic",
                    "page": mention["page"], "evidence_type": "literal term match",
                })
            for ref in refs:
                rid = "reference:" + ref["identifier"].upper()
                ref_ids.add(rid)
                edges.append({
                    "source": doc_id, "target": rid, "type": "mentions_standard_identifier",
                    "page": ref["page"], "evidence": ref["identifier"],
                })
            reports.append({
                "file": rel, "status": "text_extracted" if has_text else "no_extractable_text_ocr_needed",
                "pages": len(PdfReader(str(path)).pages),
                "provision_headings_found": len(clauses),
                "topic_term_mentions": len(mentions),
                "standard_identifier_mentions": len(refs),
            })
        except Exception as exc:  # Keep processing remaining documents.
            reports.append({"file": rel, "status": "error", "error": f"{type(exc).__name__}: {exc}"})

    for tid in sorted(topic_ids):
        nodes.append({"id": tid, "type": "topic", "name": tid.removeprefix("topic:")})
    for rid in sorted(ref_ids):
        nodes.append({
            "id": rid, "type": "standard_reference", "identifier": rid.removeprefix("reference:"),
            "note": "从正文匹配到的编号；未经核验，不表示规范性引用、现行有效或替代关系。",
        })

    result = {
        "schema_version": "2.0",
        "title": "法规标准库正文级知识图谱（机器抽取草稿）",
        "language": "zh-CN",
        "source_repository": "https://github.com/Zzx110814/-",
        "generation_method": "本地 PDF 文本抽取 + 可配置规则，不包含 AI 法律解释",
        "limitations": [
            "条款标题、主题词和标准编号由规则自动识别，均需人工复核；主题词命中不代表适用关系。",
            "仅保存条款号与标题，不复制条款正文。页面号是 PDF 页序，不保证等于印刷页码。",
            "扫描版或图片版 PDF 需要 OCR；抽取不到正文的文件会在 extraction_report.json 中标记。",
            "引用编号仅表示正文出现了该编号，不能据此判断规范性引用、有效状态或替代/废止关系。",
        ],
        "nodes": nodes,
        "edges": edges,
        "extraction_report": reports,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(nodes)} nodes, {len(edges)} edges")
    for report in reports:
        print(f"{report['status']}: {report['file']}")


if __name__ == "__main__":
    main()
