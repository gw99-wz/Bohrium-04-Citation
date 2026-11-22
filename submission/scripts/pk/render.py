"""Output assembly: images of the run go to /app/output, all editable and honest.

Every artefact is written even on a partial run, so a reviewer can always see how far
the pipeline got and why it stopped.
"""

from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path

from .common import dump_json, dump_jsonl, now_iso, sha256_text, write_text
from .contract import Contract
from .styles import render_reference_list

REF_HEADING = "参考文献"


def _csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: _flat(r.get(k)) for k in columns})
    return buf.getvalue()


def _flat(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def build_manuscript(body: str, ref_list_text: str, style: str) -> str:
    body = body.rstrip("\n")
    head = REF_HEADING if style != "apa" else "References"
    return f"{body}\n\n## {head}\n\n{ref_list_text}\n"


def changes(ctx: dict) -> list[dict]:
    numbering = ctx["numbering"]
    dedup = ctx["dedup_stats"]
    out = []

    for occ in numbering["occurrences"]:
        out.append(
            {
                "type": "renumber",
                "citation_id": occ["seq"] + 1,
                "entity": occ["entity_key"],
                "old_marker": occ["raw"],
                "new_numbers": occ["numbers"],
                "region": occ["region"],
            }
        )
    for d in ctx["decisions"]:
        if d["decision"] == "merged":
            out.append(
                {
                    "type": "merge",
                    "pair": [d["a_index"], d["b_index"]],
                    "reason": d.get("reason"),
                    "confidence": d["confidence"],
                    "basis": d["basis"],
                }
            )
        elif d["decision"] == "needs_review":
            out.append(
                {
                    "type": "needs_review",
                    "pair": [d["a_index"], d["b_index"]],
                    "reason": d.get("reason"),
                    "basis": d["basis"],
                }
            )
    for e in ctx["entities"]:
        if not e.get("record_ids"):
            out.append(
                {
                    "type": "unresolved_reference",
                    "entity": e["entity_key"],
                    "title": e.get("title", ""),
                    "note": "文献库中无对应记录，已保留原条目并标注",
                }
            )
    return out


def uncertainty(ctx: dict) -> list[dict]:
    out = []
    for e in ctx["entities"]:
        if e.get("needs_review"):
            out.append(
                {
                    "entity": e["entity_key"],
                    "kind": "merge_needs_review",
                    "detail": f"{len(e['members'])} 条记录被归并但缺乏强标识（DOI/arXiv），请人工确认",
                    "title": e.get("title", ""),
                }
            )
        for c in e.get("field_conflicts") or []:
            out.append(
                {
                    "entity": e["entity_key"],
                    "kind": "field_conflict",
                    "detail": f"{c['field']} 取值不一致: {c['values']}",
                }
            )
    for f in ctx["verify"]["fabrication_suspected"]:
        out.append(
            {
                "entity": f["entity_key"],
                "kind": "fabrication_suspected",
                "detail": "；".join(f["reasons"]),
                "title": f["title"],
            }
        )
    if ctx["verify"]["network"]["mode"] == "offline_degraded":
        out.append(
            {
                "entity": None,
                "kind": "network_unavailable",
                "detail": ctx["verify"]["network"].get("note", "DOI 解析不可用，已降级为本地核验"),
            }
        )
    for d in ctx["numbering"]["dangling"]:
        out.append(
            {
                "entity": None,
                "kind": "dangling_marker",
                "detail": f"正文标记 {d['raw']} 无对应条目，已原样保留",
            }
        )
    return out


def write_outputs(ctx: dict, out_dir: Path, contract: Contract) -> dict:
    started = ctx.get("started_at", time.time())
    out_dir.mkdir(parents=True, exist_ok=True)

    entities = ctx["entities"]
    numbering = ctx["numbering"]
    verify = ctx["verify"]
    decisions = ctx["decisions"]
    numbers = numbering["numbers"]

    ref_list_text = render_reference_list(entities, ctx["style"], numbers,
                                          numbering["uncited"], numbering.get("suffixes"))
    manuscript = build_manuscript(numbering["body"], ref_list_text, ctx["style"])

    dump_json(entities, out_dir / "references.json")
    write_text(ref_list_text + "\n", out_dir / "references.md")
    dump_jsonl(verify["ledger"], out_dir / "citation_ledger.jsonl")
    write_text(
        _csv(
            verify["ledger"],
            ["citation_id", "sentence_id", "region", "entity_key", "ref_no", "title",
             "year", "doi", "in_library", "doi_network", "verdict", "cited_sentence"],
        ),
        out_dir / "citation_ledger.csv",
    )
    dump_json(
        {
            "stats": ctx["dedup_stats"],
            "decisions": decisions,
            "entities": [
                {
                    "entity_key": e["entity_key"],
                    "entry_indices": e["entry_indices"],
                    "record_ids": e["record_ids"],
                    "title": e.get("title", ""),
                    "title_alt": e.get("title_alt", ""),
                    "year": e.get("year"),
                    "doi": e.get("doi", ""),
                    "arxiv": e.get("arxiv", ""),
                    "needs_review": e.get("needs_review"),
                    "merge_basis": e.get("merge_basis", []),
                    "field_conflicts": e.get("field_conflicts", []),
                }
                for e in entities
            ],
        },
        out_dir / "merge_decisions.json",
    )
    write_text(manuscript, out_dir / "manuscript_fixed.md")
    dump_json(changes(ctx), out_dir / "changes.json")
    write_text(render_report(ctx, manuscript), out_dir / "verify_report.md")

    envelope = build_envelope(ctx, manuscript, ref_list_text, contract)

    flat, report = contract.resolve(envelope)
    envelope["contract"] = report

    result = dict(flat)
    result["case_id"] = ctx.get("case_id")
    result["status"] = ctx.get("status")
    result["status_reason"] = ctx.get("status_reason", "")
    result["envelope"] = envelope
    dump_json(result, out_dir / "result.json")

    manifest = {
        "skill": ctx.get("skill", {}),
        "run": {
            "started_at": ctx.get("started_at_iso"),
            "finished_at": now_iso(),
            "seconds": round(time.time() - started, 3),
            "style": ctx["style"],
            "style_reason": ctx["style_reason"],
            "case_path": ctx.get("case_path"),
        },
        "inputs": ctx.get("inputs", {}),
        "outputs": sorted(p.name for p in out_dir.iterdir() if p.is_file()),
        "manuscript_sha256": sha256_text(manuscript),
        "idempotent": numbering.get("idempotency", {}).get("idempotent"),
        "network_mode": verify["network"]["mode"],
    }
    dump_json(manifest, out_dir / "run_manifest.json")
    return {"result": result, "envelope": envelope, "contract": report}


def build_envelope(ctx: dict, manuscript: str, ref_list_text: str, contract: Contract) -> dict:
    verify = ctx["verify"]
    numbering = ctx["numbering"]
    stats = ctx["dedup_stats"]

    return {
        "case_id": ctx.get("case_id"),
        "status": ctx.get("status"),
        "status_reason": ctx.get("status_reason", ""),
        "task": ctx.get("task", ""),
        "style": ctx["style"],
        "manuscript": manuscript,
        "references": [
            {
                "no": numbering["numbers"].get(e["entity_key"]),
                "entity_key": e["entity_key"],
                "title": e.get("title", ""),
                "title_alt": e.get("title_alt", ""),
                "authors": e.get("authors") or [],
                "year": e.get("year"),
                "venue": e.get("venue", ""),
                "volume": e.get("volume", ""),
                "issue": e.get("issue", ""),
                "pages": e.get("pages", ""),
                "doi": e.get("doi", ""),
                "arxiv": e.get("arxiv", ""),
                "type": e.get("type", ""),
                "record_ids": e.get("record_ids", []),
                "entry_indices": e.get("entry_indices", []),
                "uncited": e["entity_key"] in numbering["uncited"],
            }
            for e in sorted(ctx["entities"], key=lambda x: numbering["numbers"].get(x["entity_key"], 9999))
        ],
        "references_markdown": ref_list_text,
        "merge_decisions": {
            "stats": stats,
            "merged_pairs": [d for d in ctx["decisions"] if d["decision"] == "merged"],
            "distinct_pairs": [d for d in ctx["decisions"] if d["decision"] == "distinct"],
            "needs_review_pairs": [d for d in ctx["decisions"] if d["decision"] == "needs_review"],
        },
        "citation_ledger": verify["ledger"],
        "changes": changes(ctx),
        "uncertainty": uncertainty(ctx),
        "metrics": {
            "citations": len(verify["ledger"]),
            "references": len(ctx["entities"]),
            "entries_in_input": len(ctx["entries"]),
            "duplicates_collapsed": stats["duplicates_collapsed"],
            "distinct_pairs_checked": stats["distinct_pairs"],
            "needs_review_pairs": stats["needs_review_pairs"],
            "numbering_idempotent": numbering.get("idempotency", {}).get("idempotent"),
            "numbering_continuous": numbering["continuity"]["ok"],
            "fabrication_suspected": len(verify["fabrication_suspected"]),
            "citations_not_in_library": sum(1 for r in verify["ledger"] if not r["in_library"]),
            "possible_missing_citations": len(verify["possible_missing_citations"]),
            "regions": numbering["regions"],
            "claim_verdicts": verify["summary"]["verdicts"],
            "network_mode": verify["network"]["mode"],
        },
        "sources": ctx.get("sources", []),
        "summary": ctx.get("summary_text", ""),
    }


def render_report(ctx: dict, manuscript: str) -> str:
    verify = ctx["verify"]
    numbering = ctx["numbering"]
    stats = ctx["dedup_stats"]
    m = verify["summary"]
    lines = [
        "# 引用整理核验报告",
        "",
        f"- 案例：`{ctx.get('case_id')}`　状态：**{ctx.get('status')}**",
        f"- 引用样式：`{ctx['style']}`（{ctx['style_reason']}）",
        f"- 输入条目：{len(ctx['entries'])}　归并后实体：{len(ctx['entities'])}　"
        f"折叠重复：{stats['duplicates_collapsed']}　保留为不同文献：{stats['distinct_pairs']} 对",
        f"- 正文引用点：{len(verify['ledger'])}　区域分布：{numbering['regions']}",
        f"- 编号连续：{'是' if numbering['continuity']['ok'] else '否'}　"
        f"幂等：{'是' if numbering.get('idempotency', {}).get('idempotent') else '否'}",
        "",
        "## 一、编号一致性（编得顺）",
        "",
        f"- 重排后编号 1..{len(numbering['order'])} 连续，文末列表按首现顺序排列。",
        f"- 结构重复执行结果一致：`{numbering.get('idempotency', {}).get('idempotent')}`。",
        f"- 未引用但保留的条目：{len(numbering['uncited'])} 个（已标注 `uncited`）。",
        f"- 无法解析的正文标记：{len(numbering['dangling'])} 处（原样保留，不静默丢弃）。",
        "",
        "## 二、去重决策（收得净）",
        "",
        f"- 比对 {stats['pairs_compared']} 对，合并 {stats['merged_pairs']} 对，"
        f"判为不同文献 {stats['distinct_pairs']} 对，存疑 {stats['needs_review_pairs']} 对。",
        "",
    ]

    merged = [d for d in ctx["decisions"] if d["decision"] == "merged"]
    if merged:
        lines += ["| 条目对 | 判据 | 置信度 |", "|-|-|-|"]
        for d in merged[:20]:
            basis = "、".join(
                f"{b['field']}={b.get('value', b.get('relation'))}" for b in d.get("basis", [])
            )
            lines.append(f"| [{d['a_index']}] ↔ [{d['b_index']}] | {d.get('reason','')}；{basis} | {d['confidence']} |")
        lines.append("")

    rejected = [d for d in ctx["decisions"] if d["decision"] == "distinct"]
    if rejected:
        lines += ["**明确判为不同文献（防误合并）**", "", "| 条目对 | 否决依据 |", "|-|-|"]
        for d in rejected[:20]:
            veto = "；".join(f"{v['field']}:{v['relation']}" for v in d.get("veto", []))
            lines.append(f"| [{d['a_index']}] ↔ [{d['b_index']}] | {veto} |")
        lines.append("")

    lines += [
        "## 三、真实性与匹配（对得上）",
        "",
        f"- 网络模式：`{verify['network']['mode']}`"
        f"（尝试 {verify['network']['attempted']} 个 DOI）"
        + ("　**已标注 network_unavailable**" if verify["network"]["network_unavailable"] else ""),
        f"- 引用判定：{m['verdicts']}",
        f"- 文献库中无对应记录：{m['entities_not_in_library']} 条",
        f"- 疑似编造/不可核验：{m['fabrication_suspected']} 条",
        "",
    ]
    if verify["fabrication_suspected"]:
        lines += ["| 条目 | 文献 | 原因 |", "|-|-|-|"]
        for f in verify["fabrication_suspected"][:20]:
            lines.append(f"| {f['entity_key']} | {f['title'][:50]} | {'；'.join(f['reasons'])} |")
        lines.append("")

    if verify["possible_missing_citations"]:
        lines += ["**可能缺失引用的句子（仅建议，未自动插入）**", ""]
        for g in verify["possible_missing_citations"][:10]:
            lines.append(f"- [{g['cue']}] {g['sentence'][:120]}")
        lines.append("")

    lines += ["## 四、产物", "", "```", manuscript[:400].replace("```", "ˋˋˋ"), "```", ""]
    lines += ["完整稿见 `manuscript_fixed.md`；逐条账本见 `citation_ledger.csv`；"
              "去重明细见 `merge_decisions.json`。"]
    return "\n".join(lines) + "\n"
