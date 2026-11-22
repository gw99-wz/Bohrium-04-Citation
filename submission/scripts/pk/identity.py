"""Reference identity: link manuscript entries to library records, then de-duplicate.

Two decisions this module must get right, because the challenge scores both:

  * merge     - same work appearing as 中/英 title, preprint vs published, or
                different database exports of one article
  * must NOT merge - two genuinely different works by the same author

The rule engine therefore has an explicit *veto* list that is evaluated before any
positive rule. Every pair it looked at (merged, distinct, or needs_review) is written
to the decision log with the evidence that produced it, so a reviewer can re-derive
the call from fields alone.
"""

from __future__ import annotations

import re

from .common import (
    cjk_ratio,
    first_number,
    jaccard,
    norm_key,
    norm_text,
    sim,
    strip_doi_prefix,
    tokens,
)

CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>\]\),;]+")
ARXIV_RE = re.compile(r"arxiv[:\s/]*(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
VOLISSUE_RE = re.compile(r"(\d{1,4})\s*[（(]\s*(\d{1,3})\s*[)）]")
VOLPAGE_RE = re.compile(r"[（(]?\s*(\d{1,3})\s*[)）]?\s*[:：]\s*(\d{1,6})\s*[-–—~]\s*(\d{1,6})")
TYPE_TAG_RE = re.compile(r"\[([A-Z]{1,2}(?:/OL)?)\]")
PAGES_RE = re.compile(r"(\d{1,6})\s*[-–—~]\s*(\d{1,6})")

STOP_TITLE = {"study", "studies", "effect", "effects", "analysis", "review", "research"}


def parse_entry(raw: str) -> dict:
    """Best-effort structured parse of one reference-list entry."""
    text = " ".join((raw or "").split())
    out: dict = {"raw": text}

    m = DOI_RE.search(text)
    if m:
        out["doi"] = strip_doi_prefix(m.group(0))
    m = ARXIV_RE.search(text)
    if m:
        out["arxiv"] = m.group(1)

    type_tag = ""
    m = TYPE_TAG_RE.search(text)
    if m:
        type_tag = m.group(1).split("/")[0]
        out["type"] = type_tag
    if re.search(r"arxiv|preprint|预印本", text, re.I) and not type_tag:
        out["type"] = "A"

    # year: prefer one following a journal/comma, else the last plausible year
    years = [int(y) for y in YEAR_RE.findall(text)]
    if years:
        out["year"] = years[-1] if len(years) > 1 else years[0]
        out["years_all"] = years

    m = VOLPAGE_RE.search(text)
    if m:
        out["volume"] = m.group(1)
        out["pages"] = f"{m.group(2)}-{m.group(3)}"
    else:
        m = VOLISSUE_RE.search(text)
        if m:
            out["volume"] = m.group(1)
            out["issue"] = m.group(2)
        pm = PAGES_RE.search(text)
        if pm and "pages" not in out:
            out["pages"] = f"{pm.group(1)}-{pm.group(2)}"

    # title guess: text after the author block, before the [TAG] or the venue
    body = text
    body = re.sub(r"^\[[^\]]*\]\s*", "", body)
    # ---- APA / author-year entries: "Wang, L., & Chen, H. (2020a). Title. Venue, ..."
    apa = re.match(
        r"^\s*(?P<au>.{3,240}?)\s*[（(](?P<yr>(?:19|20)\d{2})(?P<sfx>[a-z]?)[）)]\s*[.．]?\s*",
        text,
    )
    if apa:
        out["authors_raw"] = apa.group("au").strip(" ,")
        out["year"] = int(apa.group("yr"))
        out["years_all"] = [int(apa.group("yr"))]
        rest = text[apa.end():]
        m2 = re.match(r"(.+?)(?:\.\s|\.$)", rest)
        if m2:
            out["title"] = m2.group(1).strip()
            out["venue"] = rest[m2.end():].strip(" .")
        else:
            out["title"] = rest.strip()
        out["format"] = "apa"

    if "format" not in out and type_tag:
        # find the tag *in its bracket form*; a bare letter match would hit author
        # initials such as "Smith J" and swallow the title.
        tag_m = TYPE_TAG_RE.search(body)
        if tag_m:
            pre = body[: tag_m.start()].rstrip(" .")
            post = body[tag_m.end() :].lstrip("]. ").strip(" .")
            parts = [p.strip() for p in re.split(r"(?<=[.!?。])\s+", pre) if p.strip()]
            if len(parts) >= 2:
                out["authors_raw"] = " ".join(parts[:-1])
                out["title"] = parts[-1]
                out["venue"] = post
            elif parts:
                out["title"] = parts[0]
                out["venue"] = post
    if "title" not in out:
        parts = [p.strip() for p in re.split(r"[.]\s+", body) if p.strip()]
        if len(parts) >= 3:
            out["authors_raw"], out["title"] = parts[0], parts[1]
            out["venue"] = parts[2]
        elif len(parts) == 2:
            out["title"], out["venue"] = parts[0], parts[1]
        else:
            out["title"] = body

    out["title"] = (out.get("title") or "").strip(" .,;")
    out["title"] = re.sub(r"\s*\[[A-Z]{1,2}(?:/OL)?\]\s*$", "", out["title"]).strip(" .,;")
    out["lang"] = "zh" if cjk_ratio(out["title"]) > 0.3 else "en"
    return out


def make_item(index: int, entry: dict, record: dict | None) -> dict:
    """One cited reference, as it appears in the manuscript, optionally backed by a record."""
    parsed = parse_entry(entry.get("raw", ""))
    item = {
        "index": index,
        "entry_seq": index,
        "raw": entry.get("raw", ""),
        "record_id": record["id"] if record else None,
        "link": "record" if record else "none",
        "title": "",
        "title_alt": "",
        "authors": [],
        "year": None,
        "venue": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "doi": "",
        "arxiv": "",
        "type": "",
        "abstract": "",
        "lang": parsed.get("lang", "en"),
        "provenance": {"entry_index": index},
    }

    # record facts win (they are the library of record), entry text fills the gaps
    if record:
        for f in ("title", "title_alt", "authors", "year", "venue", "volume", "issue",
                  "pages", "doi", "arxiv", "type", "abstract"):
            item[f] = record.get(f) or item[f]
        item["provenance"]["record_id"] = record["id"]
    for f in ("title", "year", "volume", "issue", "pages", "doi", "arxiv"):
        if not item.get(f) and parsed.get(f):
            item[f] = parsed[f]
            item["provenance"][f] = "entry_text"

    if not item["title"]:
        item["title"] = parsed.get("title", "")
    item["first_author"] = _first_author(record, parsed)
    item["lang"] = "zh" if cjk_ratio(item["title"] + item["title_alt"]) > 0.3 else "en"
    return item


def _first_author(record, parsed) -> str:
    if record and record.get("first_author"):
        return record["first_author"]
    raw = (parsed.get("authors_raw") or "").strip()
    if not raw:
        return ""
    raw = re.split(r"[,，]|\bet al\.?|\band\b|等", raw)[0].strip()
    if re.search(f"[{CJK}]", raw):
        return norm_key(raw)[:2]
    toks = [t for t in re.split(r"\s+", raw) if t]
    if not toks:
        return ""
    # 'Wang L' -> 'wang' (surname first), not 'l'
    if len(toks) >= 2 and re.fullmatch(r"[A-Z][A-Z.\-]{0,2}", toks[-1]):
        return norm_key(toks[0])
    return norm_key(toks[-1])


# ------------------------------------------------------------------ linking

def _record_quality(rec: dict) -> tuple:
    """Tie-break for equal link scores: published beats preprint, DOI beats none."""
    return (
        1 if rec.get("venue") else 0,
        1 if rec.get("doi") else 0,
        sum(1 for f in ("title", "authors", "year", "venue", "volume", "pages") if rec.get(f)),
        0 if rec.get("type") == "A" else 1,
    )


def link_entries(entries: list[dict], records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Attach each reference-list entry to at most one library record."""
    items: list[dict] = []
    links: list[dict] = []
    used: dict[str, int] = {}

    for entry in entries:
        parsed = parse_entry(entry.get("raw", ""))
        best, best_score, best_how = None, 0.0, ""
        best_quality: tuple = ()

        for rec in records:
            score, how = _link_score(parsed, rec)
            quality = _record_quality(rec)
            # strict-greater on the pair: an equal title match must still prefer the
            # published record over its preprint, never the first one seen.
            if (round(score, 4), quality) > (round(best_score, 4), best_quality):
                best, best_score, best_how, best_quality = rec, score, how, quality

        if best is not None and best_score < 0.55:
            best, best_how = None, ""

        items.append(make_item(entry["index"], entry, best))
        links.append(
            {
                "entry_index": entry["index"],
                "record_id": best["id"] if best else None,
                "score": round(best_score, 3),
                "how": best_how,
                "title": parsed.get("title", "")[:120],
            }
        )
        if best:
            used[best["id"]] = used.get(best["id"], 0) + 1

    return items, links


def _link_score(parsed: dict, rec: dict) -> tuple[float, str]:
    if parsed.get("doi") and rec.get("doi") and parsed["doi"] == rec["doi"]:
        return 0.99, "doi"
    if parsed.get("arxiv") and rec.get("arxiv") and parsed["arxiv"] == rec["arxiv"]:
        return 0.97, "arxiv"

    entry_title = parsed.get("title", "")
    if not entry_title:
        return 0.0, ""
    candidates = [rec.get("title", ""), rec.get("title_alt", "")]
    tsim = max((sim(entry_title, c) for c in candidates if c), default=0.0)

    year_ok = bool(parsed.get("year") and rec.get("year") and parsed["year"] == rec["year"])
    vol_ok = bool(parsed.get("volume") and rec.get("volume") and parsed["volume"] == rec["volume"])
    page_ok = bool(parsed.get("pages") and rec.get("pages") and parsed["pages"] == rec["pages"])

    if tsim >= 0.90:
        return min(0.95, 0.86 + 0.09 * tsim + (0.03 if year_ok else 0)), "title"
    if tsim >= 0.70 and (year_ok or vol_ok):
        return 0.70 + 0.1 * tsim, "title+year/vol"
    if year_ok and vol_ok and page_ok:
        return 0.72, "year+vol+pages"
    if vol_ok and page_ok and tsim >= 0.55:
        return 0.66, "vol+pages+title"
    return max(0.0, tsim * 0.6), "weak"


# ------------------------------------------------------------------ pair decision

def _authors_overlap(a: dict, b: dict) -> float:
    sa = {norm_key(x) for x in (a.get("authors") or []) if x}
    sb = {norm_key(x) for x in (b.get("authors") or []) if x}
    if sa and sb:
        return jaccard(sa, sb)
    fa, fb = a.get("first_author") or "", b.get("first_author") or ""
    if fa and fb:
        return 1.0 if fa == fb else 0.0
    return 0.0


def _title_sim(a: dict, b: dict) -> float:
    ta = [t for t in (a.get("title"), a.get("title_alt")) if t]
    tb = [t for t in (b.get("title"), b.get("title_alt")) if t]
    best = 0.0
    for x in ta:
        for y in tb:
            best = max(best, sim(x, y))
    return best


def compare(a: dict, b: dict) -> dict:
    """Decide whether two cited references are the same work. Deterministic."""
    basis: list[dict] = []
    veto: list[dict] = []
    conf = 0.0

    ad, bd = a.get("doi") or "", b.get("doi") or ""
    aa, ba = a.get("arxiv") or "", b.get("arxiv") or ""

    if ad and bd:
        if ad == bd:
            basis.append({"field": "doi", "relation": "equal", "value": ad})
            conf = max(conf, 0.99)
        else:
            veto.append({"field": "doi", "relation": "differ", "a": ad, "b": bd})

    if aa and ba:
        if aa == ba:
            basis.append({"field": "arxiv", "relation": "equal", "value": aa})
            conf = max(conf, 0.97)
        elif not (ad and bd and ad == bd):
            veto.append({"field": "arxiv", "relation": "differ", "a": aa, "b": ba})

    if a.get("record_id") and a.get("record_id") == b.get("record_id"):
        basis.append({"field": "record_id", "relation": "equal", "value": a["record_id"]})
        conf = max(conf, 0.95)

    tsim = _title_sim(a, b)
    aov = _authors_overlap(a, b)
    ay, by = a.get("year"), b.get("year")
    dy = abs(ay - by) if (ay and by) else None
    av, bv = a.get("volume") or "", b.get("volume") or ""
    ap, bp = a.get("pages") or "", b.get("pages") or ""

    if tsim:
        basis.append({"field": "title", "relation": "similarity", "value": round(tsim, 3)})
    if aov:
        basis.append({"field": "authors", "relation": "overlap", "value": round(aov, 3)})
    if dy is not None:
        basis.append({"field": "year", "relation": "equal" if dy == 0 else "delta", "value": dy})

    # ---- vetoes (different works) -------------------------------------------
    if av and bv and av != bv:
        veto.append({"field": "volume", "relation": "differ", "a": av, "b": bv})
    if dy is not None and dy >= 2 and tsim < 0.97 and not (ad and bd and ad == bd):
        veto.append({"field": "year", "relation": "delta>=2", "value": dy})
    if aov == 0.0 and a.get("first_author") and b.get("first_author") and tsim < 0.80:
        veto.append({"field": "authors", "relation": "disjoint"})
    if ap and bp and ap != bp and tsim < 0.90 and not (ad and bd and ad == bd):
        veto.append({"field": "pages", "relation": "differ", "a": ap, "b": bp})
    if tsim < 0.55 and not (ad and bd and ad == bd) and not (aa and ba and aa == ba) \
            and not a.get("record_id") == b.get("record_id"):
        veto.append({"field": "title", "relation": "below-floor", "value": round(tsim, 3)})

    if veto:
        return {
            "decision": "distinct",
            "confidence": 0.0,
            "basis": basis,
            "veto": veto,
        }

    # ---- positive rules ------------------------------------------------------
    if ad and bd and ad == bd:
        return {"decision": "merged", "confidence": 0.99, "basis": basis, "veto": [],
                "reason": "same DOI"}
    if aa and ba and aa == ba:
        return {"decision": "merged", "confidence": 0.97, "basis": basis, "veto": [],
                "reason": "same arXiv id"}
    if a.get("record_id") and a.get("record_id") == b.get("record_id"):
        return {"decision": "merged", "confidence": 0.95, "basis": basis, "veto": [],
                "reason": "same library record"}

    year_ok = dy is None or dy == 0
    near_year = dy is not None and dy <= 1
    preprint_a = bool(aa) and not av
    preprint_b = bool(ba) and not bv

    # preprint -> published: same title/author, one side has a journal venue
    if (preprint_a or preprint_b) and tsim >= 0.86 and aov >= 0.5 and (year_ok or near_year):
        return {"decision": "merged", "confidence": 0.88, "basis": basis, "veto": [],
                "reason": "preprint and published version of one work"}

    if tsim >= 0.92 and (year_ok or near_year) and (aov >= 0.5 or not (a.get("first_author") and b.get("first_author"))):
        return {"decision": "merged", "confidence": 0.90, "basis": basis, "veto": [],
                "reason": "title match with consistent author/year"}

    # cross-language (zh title vs en title) has no usable title similarity;
    # require two corroborating fields and refuse to guess on one.
    cross_lang = {a.get("lang"), b.get("lang")} == {"zh", "en"} or tsim < 0.2
    if cross_lang and aov >= 0.5 and year_ok:
        corroboration = [x for x, ok in (("volume", av and bv and av == bv),
                                         ("pages", ap and bp and ap == bp),
                                         ("venue", _venue_overlap(a, b))) if ok]
        if corroboration:
            return {"decision": "merged", "confidence": 0.82,
                    "basis": basis + [{"field": c, "relation": "equal"} for c in corroboration],
                    "veto": [], "reason": f"cross-language merge corroborated by {corroboration}"}
        return {"decision": "needs_review", "confidence": 0.45, "basis": basis, "veto": [],
                "reason": "cross-language candidate with only author+year; not enough to merge"}

    if tsim >= 0.75:
        return {"decision": "needs_review", "confidence": 0.5, "basis": basis, "veto": [],
                "reason": "moderate title similarity, no identifier to confirm"}

    return {"decision": "distinct", "confidence": 0.0, "basis": basis,
            "veto": [{"field": "title", "relation": "no-positive-rule", "value": round(tsim, 3)}]}


def _venue_overlap(a: dict, b: dict) -> bool:
    va, vb = a.get("venue") or "", b.get("venue") or ""
    if not va or not vb:
        return False
    va, vb = norm_text(va), norm_text(vb)
    return va in vb or vb in va or jaccard(tokens(va), tokens(vb)) >= 0.5


# ------------------------------------------------------------------ clustering

class _DSU:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def dedup(items: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Cluster cited references into canonical entities. Returns (entities, decisions, stats)."""
    n = len(items)
    dsu = _DSU(n)
    decisions: list[dict] = []

    for i in range(n):
        for j in range(i + 1, n):
            a, b = items[i], items[j]
            # cheap pre-filter: skip pairs that cannot possibly be the same work
            if not _worth_comparing(a, b):
                continue
            d = compare(a, b)
            d.update({"a_index": a["index"], "b_index": b["index"]})
            decisions.append(d)
            if d["decision"] == "merged":
                dsu.union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(dsu.find(i), []).append(i)

    entities: list[dict] = []
    for gi, member_pos in enumerate(sorted(groups.values(), key=lambda g: items[g[0]]["index"])):
        members = [items[p] for p in member_pos]
        primary = _choose_primary(members)
        ent = _build_entity(gi + 1, members, primary)
        entities.append(ent)

    for d in decisions:
        if d["decision"] == "merged":
            ea = _entity_of(entities, d["a_index"])
            eb = _entity_of(entities, d["b_index"])
            if ea and ea is eb:
                ea["merge_basis"].append(
                    {"pair": [d["a_index"], d["b_index"]], "reason": d.get("reason"),
                     "confidence": d["confidence"], "basis": d["basis"]}
                )
                d["entity"] = ea["entity_key"]

    rejected = [d for d in decisions if d["decision"] == "distinct"]
    review = [d for d in decisions if d["decision"] == "needs_review"]
    stats = {
        "items": n,
        "entities": len(entities),
        "merged_pairs": sum(1 for d in decisions if d["decision"] == "merged"),
        "duplicates_collapsed": n - len(entities),
        "distinct_pairs": len(rejected),
        "needs_review_pairs": len(review),
        "pairs_compared": len(decisions),
    }
    return entities, decisions, stats


def _worth_comparing(a: dict, b: dict) -> bool:
    """Pre-filter. Cheap and conservative: only obvious negatives are skipped."""
    if a.get("doi") and b.get("doi") and a["doi"] == b["doi"]:
        return True
    if a.get("arxiv") and b.get("arxiv") and a["arxiv"] == b["arxiv"]:
        return True
    if a.get("record_id") and a.get("record_id") == b.get("record_id"):
        return True
    if _title_sim(a, b) >= 0.30:
        return True
    if _authors_overlap(a, b) >= 0.5:
        return True
    # same first author + same year is exactly the 误合并 risk; always examine it so the
    # decision log carries an explicit negative finding instead of silently skipping.
    if (a.get("first_author") and a.get("first_author") == b.get("first_author")
            and a.get("year") and a.get("year") == b.get("year")):
        return True
    if (a.get("year") and a.get("year") == b.get("year")
            and (a.get("volume") and a.get("volume") == b.get("volume"))):
        return True
    return False


def _choose_primary(members: list[dict]) -> dict:
    def score(m: dict) -> tuple:
        published = 1 if (m.get("venue") and m.get("type") not in ("A",)) else 0
        has_doi = 1 if m.get("doi") else 0
        filled = sum(1 for f in ("title", "authors", "year", "venue", "volume", "pages", "doi")
                     if m.get(f))
        has_record = 1 if m.get("record_id") else 0
        return (published + has_doi, filled, has_record, -m["index"])

    return max(members, key=score)


def _build_entity(eid: int, members: list[dict], primary: dict) -> dict:
    fields = {}
    provenance = {}
    for f in ("title", "title_alt", "authors", "year", "venue", "volume", "issue",
              "pages", "doi", "arxiv", "type", "abstract"):
        val = primary.get(f)
        src = primary["index"]
        if not val:
            for m in members:
                if m.get(f):
                    val, src = m[f], m["index"]
                    break
        fields[f] = val
        if val:
            provenance[f] = src

    conflicts = []
    for f in ("year", "volume", "pages", "doi"):
        vals = {str(m.get(f)) for m in members if m.get(f)}
        if len(vals) > 1:
            conflicts.append({"field": f, "values": sorted(vals)})

    return {
        "entity_key": f"E{eid:03d}",
        "members": [m["index"] for m in members],
        "entry_indices": sorted(m["entry_seq"] for m in members),
        "primary_index": primary["index"],
        "raw_entry": primary.get("raw", ""),
        "record_ids": sorted({m["record_id"] for m in members if m.get("record_id")}),
        "fields": fields,
        "field_provenance": provenance,
        "field_conflicts": conflicts,
        "merge_basis": [],
        "needs_review": len(members) > 1 and not _has_strong_basis(members),
        "title": fields.get("title", ""),
        "title_alt": fields.get("title_alt", ""),
        "authors": fields.get("authors") or [],
        "year": fields.get("year"),
        "venue": fields.get("venue", ""),
        "volume": fields.get("volume", ""),
        "issue": fields.get("issue", ""),
        "pages": fields.get("pages", ""),
        "doi": fields.get("doi", ""),
        "arxiv": fields.get("arxiv", ""),
        "type": fields.get("type", ""),
        "lang": "zh" if cjk_ratio((fields.get("title") or "") + (fields.get("title_alt") or "")) > 0.3 else "en",
    }


def _has_strong_basis(members: list[dict]) -> bool:
    if len(members) < 2:
        return True
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            a, b = members[i], members[j]
            if a.get("doi") and a["doi"] == b.get("doi"):
                return True
            if a.get("arxiv") and a["arxiv"] == b.get("arxiv"):
                return True
            if a.get("record_id") and a["record_id"] == b.get("record_id"):
                return True
    return False


def _entity_of(entities: list[dict], index: int):
    for e in entities:
        if index in e["members"]:
            return e
    return None


def map_index_to_entity(entities: list[dict]) -> dict:
    out = {}
    for e in entities:
        for idx in e["members"]:
            out[idx] = e["entity_key"]
    return out
