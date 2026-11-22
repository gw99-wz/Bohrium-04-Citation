"""Verification (对得上 + 防编造), with mandatory offline degradation.

Three independent evidence layers, strongest first:

  1. closed world   - every cited work must resolve to a record in the supplied library;
                      an entry that exists only in the manuscript's own list is the
                      single strongest fabrication signal.
  2. field checks   - DOI syntax, year/volume/page plausibility, metadata completeness,
                      and conflicts surfaced by the merge layer.
  3. DOI resolution - optional network probe to doi.org. ANY failure (timeout, DNS,
                      TLS, offline) flips the whole run to `offline_degraded`, records
                      `network_unavailable: true`, and continues with layers 1-2.
                      Network problems never raise and never change a verdict into
                      "fabricated": `doi_unresolved` is kept distinct from `fabricated`.

Plus claim-level matching so a citation can be judged "does this sentence actually
match the work it cites", and an advisory list of number-bearing sentences with no
citation at all.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .common import containment, norm_key, sim, tokens

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>\]\),;]+")
CLAIM_CUE = re.compile(r"首次|首个|显著|表明|证明|优于|提高|降低|达到|突破|首次发现|"
                       r"significantly|demonstrate|prove|outperform|improve|first", re.I)
PCT_RE = re.compile(r"\d+(?:\.\d+)?\s*[%％]")

CURRENT_YEAR = datetime.now(timezone.utc).year


# ------------------------------------------------------------------ network

def check_doi(doi: str, timeout: float = 2.5) -> dict:
    """Probe doi.org. Returns a status; never raises."""
    url = f"https://doi.org/api/handles/{doi}"
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "citation-skill/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
        try:
            data = json.loads(body)
        except Exception:
            return {"doi": doi, "status": "resolved", "detail": "non-json 200"}
        code = data.get("responseCode")
        if code == 1:
            return {"doi": doi, "status": "resolved"}
        return {"doi": doi, "status": "not_found", "detail": str(code)}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"doi": doi, "status": "not_found"}
        return {"doi": doi, "status": "http_error", "detail": str(exc.code)}
    except Exception as exc:  # timeout, DNS, TLS, sandbox block, ...
        return {"doi": doi, "status": "network_unavailable", "detail": type(exc).__name__}


def run_doi_checks(entities: list[dict], mode: str = "auto", limit: int = 10,
                   timeout: float = 2.5) -> dict:
    """mode: auto | online | offline. Auto flips to offline on the first network failure."""
    result = {
        "mode": "offline_degraded" if mode == "offline" else "online",
        "attempted": 0,
        "network_unavailable": False,
        "checks": [],
        "note": "",
    }
    if mode == "offline":
        result["note"] = "network checks disabled by request (--no-network)"
        return result

    dois = []
    for e in entities:
        d = e.get("doi") or ""
        if d and d not in dois:
            dois.append(d)

    for doi in dois[:limit]:
        if mode == "offline":
            break
        chk = check_doi(doi, timeout=timeout)
        result["attempted"] += 1
        result["checks"].append(chk)
        if chk["status"] == "network_unavailable":
            result["network_unavailable"] = True
            result["mode"] = "offline_degraded"
            result["note"] = (
                f"DOI 解析不可用（{chk.get('detail','error')}），已降级为本地正则+元数据交叉比对；"
                "离线判定不等于条目编造。"
            )
            break

    if not result["network_unavailable"] and result["mode"] == "online":
        if len(dois) > limit:
            result["note"] = f"只需核验前 {limit} 个 DOI（共 {len(dois)} 个），其余按本地规则判定。"
        else:
            result["note"] = "DOI 解析可用。"
    return result


# ------------------------------------------------------------------ field checks

def field_checks(entity: dict, library_dois: set[str]) -> list[dict]:
    issues: list[dict] = []
    rec_ids = entity.get("record_ids") or []

    if not rec_ids:
        issues.append({
            "code": "REF_NOT_IN_LIBRARY", "severity": "high",
            "detail": "该条目无法在提供的文献库中找到对应记录",
            "fix": "核对条目真实存在性；若确为稿件新增文献，需标注来源并人工确认",
        })

    for f, label in (("title", "题名"), ("authors", "作者"), ("year", "年份"), ("venue", "来源")):
        if not entity.get(f):
            sev = "medium" if f in ("year", "venue") else "high"
            issues.append({"code": f"MISSING_{f.upper()}", "severity": sev,
                           "detail": f"{label}缺失", "fix": f"从文献库或原文补齐{label}"})

    doi = entity.get("doi") or ""
    if doi and not re.fullmatch(r"10\.\d{4,9}/\S{2,}", doi):
        issues.append({"code": "DOI_MALFORMED", "severity": "high",
                       "detail": f"DOI 格式不合法: {doi}"})
    if doi and library_dois and doi not in library_dois and rec_ids:
        issues.append({"code": "DOI_NOT_IN_LIBRARY", "severity": "medium",
                       "detail": "DOI 与文献库中任何记录都不一致"})

    year = entity.get("year")
    if isinstance(year, int):
        if year > CURRENT_YEAR + 1:
            issues.append({"code": "YEAR_IN_FUTURE", "severity": "medium",
                           "detail": f"年份 {year} 晚于当前年份"})
        if year < 1800:
            issues.append({"code": "YEAR_IMPLAUSIBLE", "severity": "medium",
                           "detail": f"年份 {year} 不合常理"})

    pages = str(entity.get("pages") or "")
    m = re.match(r"^(\d+)\s*[-–—~]\s*(\d+)$", pages.strip())
    if m and int(m.group(1)) > int(m.group(2)):
        issues.append({"code": "PAGES_REVERSED", "severity": "medium",
                       "detail": f"页码起止颠倒: {pages}"})

    for c in entity.get("field_conflicts") or []:
        issues.append({"code": "FIELD_CONFLICT", "severity": "low",
                       "detail": f"{c['field']} 在各来源间不一致: {c['values']}"})
    return issues


# ------------------------------------------------------------------ claim matching

def match_claim(sentence: str, entity: dict) -> dict:
    """Judge whether the citing sentence is supported by the cited work.

    Only uses evidence actually supplied (abstract / fulltext). With no evidence text
    the honest verdict is `evidence_insufficient`, never a confident "supported".
    """
    evidence = " ".join(
        x for x in (entity.get("abstract") or "", entity.get("fulltext") or "") if x
    ).strip()
    if not evidence:
        return {
            "verdict": "evidence_insufficient",
            "score": None,
            "numbers_ok": None,
            "unmatched_numbers": [],
            "reason": "材料未提供该文献的摘要或全文，无法判定论断—文献匹配",
        }

    score = containment(sentence, evidence)
    s_nums = NUMBER_RE.findall(sentence)
    e_nums = set(NUMBER_RE.findall(evidence))
    unmatched = [n for n in s_nums if n not in e_nums]

    if score >= 0.34 and not unmatched:
        verdict = "supported"
    elif score >= 0.18:
        verdict = "weak"
    else:
        verdict = "mismatch"
    if unmatched and verdict == "supported":
        verdict = "weak"

    return {
        "verdict": verdict,
        "score": round(score, 3),
        "numbers_ok": not unmatched,
        "unmatched_numbers": unmatched[:10],
        "reason": "论断关键词与文献证据重合" if verdict == "supported" else (
            "论断与文献证据重合度偏低" if verdict == "weak" else "论断与文献证据几乎不重合"
        ),
    }


# ------------------------------------------------------------------ main

def verify(entities: list[dict], occurrences: list[dict], library: list[dict],
           numbers: dict, body: str, mode: str = "auto") -> dict:
    library_dois = {r["doi"] for r in library if r.get("doi")}
    by_key = {e["entity_key"]: e for e in entities}

    doi_report = run_doi_checks(entities, mode=mode)
    doi_status = {}
    for chk in doi_report["checks"]:
        doi_status[chk["doi"]] = chk["status"]

    ledger: list[dict] = []
    issues_by_entity: dict[str, list[dict]] = {}
    checked: set[str] = set()

    for occ in occurrences:
        key = occ["entity_key"]
        ent = by_key.get(key) or {}
        issues = issues_by_entity.setdefault(key, [])
        if key not in checked:
            checked.add(key)
            issues.extend(field_checks(ent, library_dois))
            doi = ent.get("doi") or ""
            status = doi_status.get(doi)
            if status == "not_found":
                issues.append({
                    "code": "DOI_NOT_REGISTERED", "severity": "medium",
                    "detail": f"doi.org 未注册该 DOI: {doi}（可能是元数据错误，需人工确认；"
                              "不代表条目一定编造）",
                })
            elif status == "resolved":
                issues.append({"code": "DOI_RESOLVED", "severity": "info",
                               "detail": f"DOI 已在 doi.org 解析: {doi}"})

        claim = match_claim(occ.get("sentence", ""), ent)
        doi = ent.get("doi") or ""
        net = doi_status.get(doi, "not_checked")

        bad = [i for i in issues if i["severity"] == "high"]
        if bad:
            verdict = "fail"
        elif claim["verdict"] == "mismatch":
            verdict = "fail"
        elif any(i["severity"] == "medium" for i in issues) or claim["verdict"] == "weak":
            verdict = "needs_review"
        else:
            verdict = "ok"

        ledger.append(
            {
                "citation_id": f"C{len(ledger) + 1:03d}",
                "sentence_id": occ.get("sentence_id"),
                "region": occ.get("region"),
                "entity_key": key,
                "ref_no": numbers.get(key),
                "marker_raw": occ.get("raw"),
                "cited_sentence": (occ.get("sentence") or "")[:300],
                "title": ent.get("title", ""),
                "year": ent.get("year"),
                "doi": doi,
                "in_library": bool(ent.get("record_ids")),
                "doi_network": net,
                "claim_match": claim,
                "issues": issues,
                "verdict": verdict,
            }
        )

    # entries nobody cited still get field checks, so coverage is every reference
    for ent in entities:
        key = ent["entity_key"]
        if key in checked:
            continue
        checked.add(key)
        issues = issues_by_entity.setdefault(key, [])
        issues.extend(field_checks(ent, library_dois))
        status = doi_status.get(ent.get("doi") or "")
        if status == "not_found":
            issues.append({
                "code": "DOI_NOT_REGISTERED", "severity": "medium",
                "detail": f"doi.org 未注册该 DOI: {ent.get('doi')}（需人工确认）",
            })

    fabricated = []
    for key, issues in issues_by_entity.items():
        high = [i for i in issues if i["severity"] == "high"]
        if any(i["code"] in ("REF_NOT_IN_LIBRARY", "DOI_MALFORMED", "MISSING_TITLE") for i in high):
            ent = by_key.get(key) or {}
            fabricated.append(
                {
                    "entity_key": key,
                    "title": ent.get("title", ""),
                    "doi": ent.get("doi", ""),
                    "reasons": [i["detail"] for i in high],
                    "evidence": [i["code"] for i in high],
                }
            )

    gaps = find_uncited_claims(body, occurrences)

    verdicts = {}
    for row in ledger:
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1

    return {
        "network": doi_report,
        "ledger": ledger,
        "fabrication_suspected": fabricated,
        "possible_missing_citations": gaps,
        "issues_by_entity": {k: v for k, v in issues_by_entity.items() if v},
        "summary": {
            "citations": len(ledger),
            "verdicts": verdicts,
            "entities": len(entities),
            "entities_not_in_library": sum(1 for e in entities if not e.get("record_ids")),
            "fabrication_suspected": len(fabricated),
            "possible_missing_citations": len(gaps),
            "doi_checked": doi_report["attempted"],
            "network_mode": doi_report["mode"],
        },
    }


def find_uncited_claims(body: str, occurrences: list[dict]) -> list[dict]:
    """Advisory only: number-bearing sentences with no citation. Never auto-inserted."""
    from .common import split_sentences

    covered = set()
    for occ in occurrences:
        covered.add(occ["start"])
    out = []
    for start, end, text in split_sentences(body):
        if len(text.strip()) < 24:
            continue
        has_number = bool(PCT_RE.search(text) or NUMBER_RE.search(text))
        has_cue = bool(CLAIM_CUE.search(text))
        if not (has_number or has_cue):
            continue
        if any(start <= occ_start < end for occ_start in covered):
            continue
        if re.match(r"^\s*(参考文献|References|\[\d+\])", text):
            continue
        out.append(
            {
                "sentence": text.strip()[:300],
                "cue": "数量" if has_number else "论断词",
                "advice": "该句含数量或强论断但未挂引用；建议人工确认是否需要来源，Skill 不自动插入以免引入材料外事实",
            }
        )
        if len(out) >= 20:
            break
    return out
