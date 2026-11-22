#!/usr/bin/env python3
"""citation-toolkit entry point.

    python scripts/run.py --input /input/case.json --out /app/output --work /app/work

Reads a manuscript + reference library, de-duplicates the library, renumbers every
citation by first appearance, rewrites in-text markers + the reference list + captions
+ appendix in one pass, verifies each citation against the library and (optionally)
doi.org, and writes result.json with all declared output_contract fields.

Idempotency is proven the same way a reviewer would check it: the whole pipeline is
run a second time on its own output, and the two manuscripts are compared byte for byte.

Exit codes: 0 success, 1 partial (with --strict), 2 unable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pk import identity, ingest, numbering as numbering_mod, render, verify as verify_mod
from pk.common import now_iso
from pk.contract import detect_contract
from pk.render import build_manuscript
from pk.styles import render_reference_list

SKILL_INFO = {
    "name": "citation-toolkit",
    "version": "1.0.0",
    "entrypoint": "scripts/run.py",
}


def default_ctx() -> dict:
    return {
        "case_id": None,
        "status": "unable",
        "status_reason": "",
        "task": "",
        "style": "gb7714",
        "style_reason": "default",
        "entries": [],
        "entities": [],
        "decisions": [],
        "dedup_stats": {
            "items": 0, "entities": 0, "merged_pairs": 0, "duplicates_collapsed": 0,
            "distinct_pairs": 0, "needs_review_pairs": 0, "pairs_compared": 0,
        },
        "numbering": {
            "body": "", "numbers": {}, "order": [], "uncited": [], "occurrences": [],
            "dangling": [], "regions": {},
            "continuity": {"ok": True, "issues": [], "cited": 0, "entities": 0},
            "idempotency": {"idempotent": True, "drift": []},
        },
        "verify": {
            "network": {"mode": "offline_degraded", "attempted": 0,
                        "network_unavailable": False, "checks": [], "note": "未执行"},
            "ledger": [], "fabrication_suspected": [], "possible_missing_citations": [],
            "issues_by_entity": {},
            "summary": {"citations": 0, "verdicts": {}, "entities": 0,
                        "entities_not_in_library": 0, "fabrication_suspected": 0,
                        "possible_missing_citations": 0, "doi_checked": 0,
                        "network_mode": "offline_degraded"},
        },
        "sources": [],
        "summary_text": "",
        "inputs": {},
        "errors": [],
        "skill": SKILL_INFO,
        "started_at": None,
        "started_at_iso": now_iso(),
    }


def pipeline(text: str, records: list[dict], style: str) -> dict:
    """One full pass: parse -> link -> dedup -> renumber -> render."""
    struct = ingest.parse_manuscript_structure(text)
    items, links = identity.link_entries(struct["entries"], records)
    entities, decisions, stats = identity.dedup(items)
    index_to_entity = identity.map_index_to_entity(entities)
    valid = {e["index"] for e in struct["entries"]}
    markers = ingest.find_markers(struct["body"], valid)
    num = numbering_mod.renumber(struct["body"], markers, entities, index_to_entity, style)
    refs = render_reference_list(entities, style, num["numbers"], num["uncited"],
                                 num.get("suffixes"))
    manuscript = build_manuscript(num["body"], refs, style)
    return {
        "struct": struct,
        "items": items,
        "links": links,
        "entities": entities,
        "decisions": decisions,
        "stats": stats,
        "numbering": num,
        "refs_text": refs,
        "manuscript": manuscript,
        "markers": len(markers),
    }


def first_diff(a: str, b: str) -> list[dict]:
    if a == b:
        return []
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return [{"at": i, "before": a[max(0, i - 60): i + 60],
                     "after": b[max(0, i - 60): i + 60]}]
    return [{"at": n, "note": "one output is a prefix of the other", "len": [len(a), len(b)]}]


def resolve_case(path_arg: str) -> Path | None:
    p = Path(path_arg)
    if p.is_file():
        return p
    if p.is_dir():
        for name in ("case.json", "input.json"):
            cand = p / name
            if cand.is_file():
                return cand
        jsons = sorted(x for x in p.glob("*.json") if x.is_file())
        if len(jsons) == 1:
            return jsons[0]
    return None


def run(case_path: Path, out_dir: Path, work_dir: Path, network: str,
        style_override: str | None, case_id_override: str | None) -> dict:
    ctx = default_ctx()
    ctx["started_at"] = time.time()
    ctx["case_path"] = str(case_path)
    work_dir.mkdir(parents=True, exist_ok=True)

    case = ingest.load_case(case_path)
    case_dir = case_path.parent
    contract = detect_contract(case)

    ctx["case_id"] = case_id_override or case.get("case_id") or case.get("id") or case_path.stem
    ctx["task"] = str(case.get("task") or case.get("task_text") or "")[:4000]

    # ---- manuscript -------------------------------------------------------
    text = ""
    try:
        text, meta = ingest.load_manuscript(case, case_dir)
        ctx["inputs"]["manuscript"] = meta
    except Exception as exc:
        ctx["errors"].append({"stage": "manuscript", "error": f"{type(exc).__name__}: {exc}"})
        ctx["inputs"]["manuscript"] = {"reader": "error"}

    if not text.strip():
        ctx["status"] = "unable"
        ctx["status_reason"] = "无法读取稿件正文（case.json 未指向可读文件，且目录中未找到稿件）"
        ctx["summary_text"] = ctx["status_reason"]
        render.write_outputs(ctx, out_dir, contract)
        return ctx

    # ---- library ----------------------------------------------------------
    records: list[dict] = []
    try:
        records, lib_meta = ingest.load_library(case, case_dir)
        ctx["inputs"]["library"] = lib_meta
    except Exception as exc:
        ctx["errors"].append({"stage": "library", "error": f"{type(exc).__name__}: {exc}"})
        ctx["inputs"]["library"] = {"reader": "error"}

    style = style_override
    style_reason = "override:--style"
    if not style:
        probe = ingest.parse_manuscript_structure(text)
        style, style_reason = ingest.detect_style(case, probe["body"])
    ctx["style"], ctx["style_reason"] = style, style_reason

    # ---- pass 1 -----------------------------------------------------------
    first: dict = {}
    try:
        first = pipeline(text, records, style)
        ctx["entries"] = first["struct"]["entries"]
        ctx["entities"] = first["entities"]
        ctx["decisions"] = first["decisions"]
        ctx["dedup_stats"] = first["stats"]
        ctx["numbering"] = first["numbering"]
        ctx["links"] = first["links"]
        ctx["inputs"]["reference_section"] = first["struct"]["has_reference_section"]
        ctx["inputs"]["entries_parsed"] = len(first["struct"]["entries"])
        ctx["inputs"]["markers"] = first["markers"]
    except Exception as exc:
        ctx["errors"].append({"stage": "pipeline", "error": f"{type(exc).__name__}: {exc}",
                              "trace": traceback.format_exc()[-1200:]})

    # ---- pass 2: idempotency, proven on the real output -------------------
    if first:
        try:
            second = pipeline(first["manuscript"], records, style)
            same = second["manuscript"] == first["manuscript"]
            ctx["numbering"]["idempotency"] = {
                "idempotent": same,
                "drift": first_diff(first["manuscript"], second["manuscript"]),
                "second_pass": {
                    "citations": second["markers"],
                    "entities": len(second["entities"]),
                    "duplicates_collapsed": second["stats"]["duplicates_collapsed"],
                    "manuscript_sha256": __import__("hashlib").sha256(
                        second["manuscript"].encode("utf-8")).hexdigest(),
                },
                "method": "整体流程对自身输出再跑一遍，逐字节比较稿件",
            }
        except Exception as exc:
            ctx["numbering"]["idempotency"] = {
                "idempotent": False,
                "drift": [],
                "error": f"{type(exc).__name__}: {exc}",
            }

    # ---- verify -----------------------------------------------------------
    try:
        ctx["verify"] = verify_mod.verify(
            ctx["entities"], ctx["numbering"]["occurrences"], records,
            ctx["numbering"]["numbers"], ctx["numbering"]["body"], mode=network,
        )
    except Exception as exc:
        ctx["errors"].append({"stage": "verify", "error": f"{type(exc).__name__}: {exc}",
                              "trace": traceback.format_exc()[-800:]})

    # ---- sources ----------------------------------------------------------
    ctx["sources"] = [
        {"kind": "manuscript", **(ctx["inputs"].get("manuscript") or {})},
        {"kind": "library", **(ctx["inputs"].get("library") or {})},
        {"kind": "contract", "source": contract.source, "declared": contract.names},
    ]

    ctx["status"], ctx["status_reason"] = decide_status(ctx)

    summary = (
        f"共 {len(ctx['numbering']['occurrences'])} 处正文引用、{len(ctx['entities'])} 条文献；"
        f"折叠重复 {ctx['dedup_stats']['duplicates_collapsed']} 条；"
        f"核验判定 {ctx['verify']['summary']['verdicts']}；"
        f"编号连续={ctx['numbering']['continuity']['ok']}，"
        f"幂等={ctx['numbering']['idempotency']['idempotent']}；"
        f"网络={ctx['verify']['network']['mode']}。"
    )
    if ctx["errors"]:
        summary += f" 退化阶段：{[e['stage'] for e in ctx['errors']]}。"
    ctx["summary_text"] = summary

    render.write_outputs(ctx, out_dir, contract)
    return ctx


def decide_status(ctx: dict) -> tuple[str, str]:
    reasons = []
    v = ctx["verify"]["summary"]
    num = ctx["numbering"]

    if ctx["errors"]:
        reasons.append("部分阶段异常：" + ", ".join(e["stage"] for e in ctx["errors"]))
    if ctx["inputs"].get("entries_parsed", 0) == 0:
        reasons.append("未解析到参考文献条目（可能无文末列表）")
    if ctx["inputs"].get("markers", 0) == 0:
        reasons.append("未在正文中发现引用标记")
    if v.get("fabrication_suspected"):
        reasons.append(f"{v['fabrication_suspected']} 条文献不可核验/疑似编造")
    if v.get("entities_not_in_library"):
        reasons.append(f"{v['entities_not_in_library']} 条文献在文献库中无对应记录")
    fails = v.get("verdicts", {}).get("fail", 0)
    if fails:
        reasons.append(f"{fails} 处引用判定为 fail")
    if not num["continuity"]["ok"]:
        reasons.append("编号不连续")
    if not num.get("idempotency", {}).get("idempotent", True):
        reasons.append("重复执行结果不一致（非幂等）")

    if reasons:
        return "partial", "；".join(reasons)
    return "success", "全部引用可核验，编号连续且幂等"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="citation-toolkit: 引用编号/去重/核验")
    ap.add_argument("--input", required=True, help="case.json 或包含 case.json 的目录")
    ap.add_argument("--out", default="/app/output", help="产物目录")
    ap.add_argument("--work", default="/app/work", help="工作目录")
    ap.add_argument("--no-network", action="store_true", help="完全离线：不尝试 DOI 解析")
    ap.add_argument("--online", action="store_true", help="强制尝试 DOI 解析（默认 auto）")
    ap.add_argument("--style", choices=["gb7714", "apa"], default=None, help="强制引用样式")
    ap.add_argument("--case-id", default=None)
    ap.add_argument("--strict", action="store_true", help="非 success 时返回非零退出码")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    network = "offline" if args.no_network else "auto"
    case_path = resolve_case(args.input)
    if case_path is None:
        print(json.dumps({"status": "unable", "reason": f"找不到 case.json: {args.input}"},
                         ensure_ascii=False))
        return 2

    ctx = run(case_path, Path(args.out), Path(args.work), network, args.style, args.case_id)

    payload = {
        "status": ctx["status"],
        "status_reason": ctx["status_reason"],
        "case_id": ctx["case_id"],
        "style": ctx["style"],
        "out": str(args.out),
        "metrics": {
            "citations": len(ctx["numbering"]["occurrences"]),
            "references": len(ctx["entities"]),
            "entries": len(ctx["entries"]),
            "duplicates_collapsed": ctx["dedup_stats"]["duplicates_collapsed"],
            "distinct_pairs": ctx["dedup_stats"]["distinct_pairs"],
            "needs_review_pairs": ctx["dedup_stats"]["needs_review_pairs"],
            "idempotent": ctx["numbering"]["idempotency"]["idempotent"],
            "continuous": ctx["numbering"]["continuity"]["ok"],
            "fabrication_suspected": ctx["verify"]["summary"]["fabrication_suspected"],
            "network_mode": ctx["verify"]["network"]["mode"],
        },
        "errors": ctx["errors"],
    }
    if not args.quiet:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    if ctx["status"] == "unable":
        return 2
    if args.strict and ctx["status"] != "success":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
