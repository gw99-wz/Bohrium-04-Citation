"""Numbering engine (编得顺).

Citations are keyed by *entity*, never by number. Numbers are a derived ordering
(first appearance), so any insertion, deletion, merge or reordering is a single
recompute followed by one rewrite pass over every citation surface:

    in-text markers  ->  reference-list order  ->  figure/table captions  ->  appendix

The engine is idempotent by construction: the rewritten text re-parses to the same
entity sequence, which recomputes the same numbers, which rewrites to the same bytes.
"""

from __future__ import annotations

import re

from .ingest import find_markers
from .styles import collapse_numbers, format_marker, format_marker_author_year

CAPTION_RE = re.compile(r"^\s*(?:图|表|Figure|Fig\.?|Table)\s*[0-9一二三四五六七八九十]")
APPENDIX_RE = re.compile(r"(附\s*录|appendix|supplementary)", re.I)
HEADING_RE = re.compile(r"^\s{0,3}(?:#{1,6}\s+\S|\d+(?:\.\d+)*\s+\S|[一二三四五六七八九十]+[、.]\s*\S)")


def _line_start(text: str, pos: int) -> int:
    return text.rfind("\n", 0, pos) + 1


def _heading_positions(text: str) -> list[tuple[int, str]]:
    out = []
    for m in re.finditer(r"^.*$", text, re.M):
        line = m.group(0)
        if HEADING_RE.match(line):
            out.append((m.start(), line.strip()))
    return out


def classify_region(text: str, pos: int, headings: list[tuple[int, str]]) -> str:
    line_start = _line_start(text, pos)
    line_end = text.find("\n", pos)
    line = text[line_start: line_end if line_end != -1 else len(text)]
    if CAPTION_RE.match(line):
        return "caption"
    prior = [(p, h) for p, h in headings if p <= pos]
    if prior and APPENDIX_RE.search(prior[-1][1]):
        return "appendix"
    return "body"


def _family(entity: dict) -> str:
    fam = (entity.get("authors") or [""])[0]
    if not fam:
        return ""
    if "," in fam:
        return fam.split(",")[0].strip()
    toks = fam.split()
    if len(toks) >= 2 and re.fullmatch(r"[A-Z][A-Z.\-]{0,2}", toks[-1]):
        return " ".join(toks[:-1])
    return toks[-1] if toks else ""


def year_suffixes(entities: list[dict]) -> dict:
    """APA-style a/b disambiguation when first author AND year collide.

    Without this, `(Wang, 2020)` could point at two different works and an
    author-year manuscript could never be re-verified.
    """
    groups: dict[tuple, list[dict]] = {}
    for e in entities:
        key = (_family(e).lower(), e.get("year"))
        groups.setdefault(key, []).append(e)
    out: dict[str, str] = {}
    for key, items in groups.items():
        if len(items) > 1 and key[0]:
            for i, e in enumerate(sorted(items, key=lambda x: (x.get("title") or ""))):
                out[e["entity_key"]] = chr(97 + i) if i < 26 else ""
    return out


def resolve_marker(marker: dict, index_to_entity: dict, entities_by_key: dict,
                   suffixes: dict | None = None) -> list[str]:
    suffixes = suffixes or {}
    if marker["kind"] == "author-year":
        author = (marker.get("author") or "").strip()
        year_raw = str(marker.get("year") or "").strip()
        ym = re.match(r"(\d{4})([a-z]?)", year_raw)
        year, suf = (ym.group(1), ym.group(2)) if ym else (year_raw, "")
        key = re.split(r"[,，&]|等|et al", author)[0].strip()
        hits = []
        for e in entities_by_key.values():
            fam = _family(e)
            if not fam:
                continue
            if not (key and (key[:2].lower() in fam.lower() or fam.lower() in key.lower())):
                continue
            if str(e.get("year") or "") != year:
                continue
            if suf and suffixes.get(e["entity_key"], "") != suf:
                continue
            hits.append(e["entity_key"])
        return hits

    keys = []
    for idx in marker["indices"]:
        key = index_to_entity.get(idx)
        if key and key not in keys:
            keys.append(key)
    return keys


def renumber(body: str, markers: list[dict], entities: list[dict], index_to_entity: dict,
             style: str) -> dict:
    by_key = {e["entity_key"]: e for e in entities}
    suffixes = year_suffixes(entities)
    headings = _heading_positions(body)

    occurrences = []
    order: list[str] = []
    dangling = []

    for seq, mk in enumerate(markers):
        keys = resolve_marker(mk, index_to_entity, by_key, suffixes)
        unresolved = [i for i in mk.get("indices", []) if i not in index_to_entity]
        if not keys:
            if mk["kind"] != "author-year":
                note = ("正文标记无对应条目，已原样保留"
                        if not mk.get("resolved", True)
                        else "标记无法解析到文献实体，已原样保留")
                dangling.append({"start": mk["start"], "raw": mk["raw"],
                                 "indices": mk.get("indices", []), "note": note})
            continue
        if unresolved:
            dangling.append(
                {"start": mk["start"], "raw": mk["raw"], "indices": unresolved,
                 "note": "marker kept as-is; some indices have no reference entry"}
            )
        for k in keys:
            if k not in order:
                order.append(k)
        occurrences.append(
            {
                "seq": seq,
                "start": mk["start"],
                "end": mk["end"],
                "kind": mk["kind"],
                "raw": mk["raw"],
                "entity_keys": keys,
                "region": classify_region(body, mk["start"], headings),
                "sentence": _sentence_of(body, mk["start"]),
            }
        )

    # numbers: cited entities get 1..N by first appearance
    numbers = {key: i + 1 for i, key in enumerate(order)}
    uncited = [e["entity_key"] for e in entities if e["entity_key"] not in numbers]
    next_no = len(order) + 1
    for key in uncited:
        numbers[key] = next_no
        next_no += 1

    # rewrite right-to-left so earlier offsets stay valid
    new_body = body
    for occ in sorted(occurrences, key=lambda o: o["start"], reverse=True):
        if occ["kind"] == "author-year" or style == "apa":
            token = format_marker_author_year(
                [by_key[k] for k in occ["entity_keys"]], style, suffixes
            )
        else:
            nums = [numbers[k] for k in occ["entity_keys"]]
            token = format_marker(nums, occ["kind"], style)
        if not token:
            continue
        new_body = new_body[: occ["start"]] + token + new_body[occ["end"]:]

    for occ in occurrences:
        occ["numbers"] = [numbers[k] for k in occ["entity_keys"]]
        key0 = occ["entity_keys"][0]
        occ["entity_key"] = key0
        occ["sentence_id"] = f"S{occ['seq'] + 1:03d}"

    regions = {}
    for occ in occurrences:
        regions[occ["region"]] = regions.get(occ["region"], 0) + 1

    return {
        "body": new_body,
        "numbers": numbers,
        "order": order,
        "uncited": uncited,
        "occurrences": occurrences,
        "dangling": dangling,
        "regions": regions,
        "suffixes": suffixes,
        "continuity": check_continuity(numbers, order, entities),
    }


def _sentence_of(text: str, pos: int) -> str:
    start = max(text.rfind(ch, 0, pos) for ch in "。！？\n") + 1
    end_candidates = [text.find(ch, pos) for ch in "。！？\n"]
    end_candidates = [e for e in end_candidates if e != -1]
    end = min(end_candidates) if end_candidates else len(text)
    return text[start:end].strip()[:400]


def check_continuity(numbers: dict, order: list[str], entities: list[dict]) -> dict:
    cited = [numbers[k] for k in order]
    issues = []
    expected = list(range(1, len(order) + 1))
    if sorted(cited) != expected:
        issues.append({"code": "NON_CONTIGUOUS", "detail": f"cited={sorted(cited)} expected={expected}"})
    if len(set(cited)) != len(cited):
        issues.append({"code": "DUPLICATE_NUMBER"})
    total = len(entities)
    if len(numbers) != total:
        issues.append({"code": "NUMBER_COUNT_MISMATCH", "numbers": len(numbers), "entities": total})
    return {"ok": not issues, "issues": issues, "cited": len(order), "entities": total}
