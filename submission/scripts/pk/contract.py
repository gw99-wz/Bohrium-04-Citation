"""Output-contract resolution.

The challenge never publishes the field names: `task.md` only says result.json must
carry "all original output_contract fields and case_id", and the contract arrives at
run time inside /input/case.json. So this module's whole job is to answer, at run time
and without hardcoding:

    which field names must result.json expose, and what value goes in each?

Strategy, strongest first:
  1. exact match on the normalised field name
  2. synonym table (EN + ZH), longest synonym wins
  3. depth-limited deep search inside the enriched envelope
  4. honest null + an `unsatisfied` record (never a silently invented value)
"""

from __future__ import annotations

import re

from .common import norm_key

# Canonical envelope keys -> synonyms, written as raw fragments (normalised at load).
SYNONYMS: dict[str, list[str]] = {
    "case_id": ["caseid", "case", "案例编号", "案例标识", "任务编号", "样例"],
    "status": ["status", "运行状态", "状态", "结果状态", "outcome"],
    "manuscript": [
        "manuscript", "revised_manuscript", "fixed_manuscript", "document", "draft",
        "article", "paper", "body", "正文", "稿件", "修订稿", "论文", "papertext",
        "manuscriptpath", "outputdocument", "finaltext",
    ],
    "references": [
        "references", "reference_list", "referencelist", "bibliography", "refs",
        "bibliograph", "works_cited", "文献表", "参考文献", "文献列表", "引用列表",
        "referencelistmarkdown", "referencetable",
    ],
    "merge_decisions": [
        "merge", "merged", "dedup", "dedupe", "duplicate", "去重", "合并", "归并",
        "merge_log", "deduplication", "merge_decisions", "identity",
    ],
    "citation_ledger": [
        "ledger", "citation", "citations", "verify", "verified", "verification",
        "核验", "引用核对", "证据", "evidence", "citation_ledger", "check",
    ],
    "changes": [
        "change", "changes", "diff", "changelog", "修改", "变更", "改动", "edit",
        "revision", "modifications",
    ],
    "uncertainty": [
        "uncertain", "uncertainty", "warning", "warnings", "issue", "issues",
        "不确定", "人工确认", "manual", "limitation", "limits", "待确认",
    ],
    "metrics": ["metric", "metrics", "统计", "指标", "counts", "score", "summary_stats"],
    "sources": ["source", "sources", "provenance", "来源", "溯源", "出处"],
    "summary": ["summary", "report", "报告", "摘要", "notes", "说明", "result_summary"],
    "task": ["task", "任务", "任务说明", "instruction"],
    "style": ["style", "citation_style", "格式", "规范", "引用格式", "target_style"],
}

_NORM_SYNONYMS = {
    key: [norm_key(s) for s in vals] + [norm_key(key)] for key, vals in SYNONYMS.items()
}


def _contract_value(case: dict):
    """Find the contract payload under any of its plausible names."""
    for key in (
        "output_contract", "outputContract", "required_outputs", "requiredOutputs",
        "expected_outputs", "expectedOutputs", "required_fields", "requiredFields",
        "outputs", "contract",
    ):
        if key in case:
            return case[key], key
    return None, None


def _as_fields(raw) -> list[dict]:
    """Normalise the many contract shapes into [{'name','spec','required'}]."""
    fields: list[dict] = []

    def add(name, spec=None, required=None):
        if not name or not isinstance(name, str):
            return
        fields.append(
            {
                "name": name.strip(),
                "spec": spec,
                "required": True if required is None else bool(required),
            }
        )

    if isinstance(raw, dict):
        # {"field": "type"} or {"field": {"type": ..., "description": ...}}
        for name, spec in raw.items():
            if isinstance(spec, dict):
                required = spec.get("required")
                add(name, spec, required)
            else:
                add(name, spec, None)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                add(item, None, None)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("field") or item.get("key") or item.get("id")
                if not name and len(item) == 1:
                    name = next(iter(item))
                add(name, item, item.get("required"))
    elif isinstance(raw, str):
        # "field_a, field_b" or paragraph text -> take declarative tokens
        for tok in re.split(r"[,;\s]+", raw):
            if tok and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{1,40}", tok):
                add(tok, None, None)
    return fields


class Contract:
    def __init__(self, case: dict):
        self.case = case if isinstance(case, dict) else {}
        raw, self.source = _contract_value(self.case)
        self.raw = raw
        self.fields = _as_fields(raw)
        self.names = [f["name"] for f in self.fields]
        self.declared = raw is not None

    # ------------------------------------------------------------------ matching
    def _match(self, name: str, env: dict):
        nk = norm_key(name)
        if not nk:
            return None, None, "empty-name"

        # 1. exact normalised key
        for key in env:
            if norm_key(key) == nk:
                return key, env[key], "exact"

        # 2. synonym table, longest matching synonym wins
        best = None
        for key, syns in _NORM_SYNONYMS.items():
            if key not in env:
                continue
            for syn in syns:
                if not syn:
                    continue
                if syn in nk or nk in syn:
                    cand = (len(syn), key)
                    if best is None or cand[0] > best[0]:
                        best = cand
        if best:
            return best[1], env[best[1]], "synonym"

        # 3. depth-limited deep search
        hit = _deep_find(env, nk, depth=2)
        if hit is not None:
            return hit[0], hit[1], "deep"

        return None, None, "unresolved"

    def resolve(self, envelope: dict):
        """Return (flat_fields, report). flat_fields is what result.json exposes."""
        flat: dict = {}
        report: list[dict] = []
        used: set[str] = set()

        for f in self.fields:
            name = f["name"]
            src_key, value, how = self._match(name, envelope)
            flat[name] = value
            if src_key:
                used.add(src_key)
            report.append(
                {
                    "field": name,
                    "resolved_via": how,
                    "from": src_key,
                    "ok": how in ("exact", "synonym", "deep"),
                    "spec": f["spec"] if isinstance(f["spec"], (str, int, float, bool)) else None,
                }
            )

        unresolved = [r["field"] for r in report if not r["ok"]]
        return flat, {
            "declared": self.declared,
            "source": self.source,
            "declared_fields": self.names,
            "resolved": [r for r in report if r["ok"]],
            "unresolved": unresolved,
            "fallback_used": bool(unresolved) or not self.declared,
            "envelope_keys": sorted(envelope.keys()),
        }


def _deep_find(env, nk: str, depth: int):
    """Look for a nested dict key whose normalised name equals nk."""
    if depth <= 0 or not isinstance(env, dict):
        return None
    for key, value in env.items():
        if norm_key(key) == nk and not isinstance(value, (dict,)):
            return key, value
    for key, value in env.items():
        if isinstance(value, dict):
            hit = _deep_find(value, nk, depth - 1)
            if hit:
                return hit
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            hit = _deep_find(value[0], nk, depth - 1)
            if hit:
                return hit
    return None


def detect_contract(case: dict) -> Contract:
    return Contract(case)
