"""Reference rendering: GB/T 7714-2015 (顺序编码制) and APA 7th (著者—出版年制).

Rendering is data-driven from entity fields only, so the same entity always produces
the same string - that is what makes the whole renumbering pass idempotent.
"""

from __future__ import annotations

import re

CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
SUP_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
SUP_MINUS = "⁻"

TYPE_TAG = {
    "J": "J", "C": "C", "M": "M", "D": "D", "R": "R", "A": "A", "N": "N",
    "S": "S", "P": "P", "EB": "EB/OL", "EB/OL": "EB/OL", "Z": "Z",
}


def _is_cjk(text: str) -> bool:
    return bool(re.search(f"[{CJK}]", text or ""))


def _initials(given: str) -> str:
    parts = re.split(r"[\s\-.]+", given.strip())
    out = []
    for p in parts:
        if not p or not p[0].isalpha():
            continue
        # "JA" is two initials, not one
        if p.isupper() and len(p) > 1:
            out.extend(ch for ch in p if ch.isalpha())
        else:
            out.append(p[0].upper())
    return " ".join(out)


_INITIAL_TOKEN = re.compile(r"^[A-Z][A-Z.\-]{0,2}$")


def _split_name(name: str) -> tuple[str, str]:
    """Return (family, given).

    Handles 'John Smith', 'Smith, John', 'Smith J' (GB/T 7714 style, surname first)
    and Chinese names.
    """
    name = (name or "").strip()
    if not name:
        return "", ""
    if _is_cjk(name):
        return name, ""
    if "," in name:
        fam, _, giv = name.partition(",")
        return fam.strip(), giv.strip()
    toks = name.split()
    if len(toks) == 1:
        return toks[0], ""
    # 'Wang L' / 'Perea-López N' -> surname first, trailing initials
    if _INITIAL_TOKEN.match(toks[-1]):
        return " ".join(toks[:-1]), toks[-1]
    return toks[-1], " ".join(toks[:-1])


def authors_gb(authors: list[str], lang: str = "en") -> str:
    if not authors:
        return ""
    names = []
    for a in authors[:3]:
        fam, giv = _split_name(a)
        if _is_cjk(fam):
            names.append(fam)
        else:
            ini = _initials(giv)
            names.append(f"{fam.upper()} {ini}".strip() if ini else fam.upper())
    sep = ", "
    out = sep.join(names)
    if len(authors) > 3:
        out += ", 等" if lang == "zh" else ", et al."
    return out


def authors_apa(authors: list[str]) -> str:
    if not authors:
        return ""
    parts = []
    for a in authors[:8]:
        fam, giv = _split_name(a)
        if _is_cjk(fam):
            parts.append(fam)
        elif giv:
            parts.append(f"{fam}, {_initials_apa(giv)}")
        else:
            parts.append(fam)
    if len(authors) > 8:
        return ", ".join(parts) + ", et al."
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]}, & {parts[1]}"
    return ", ".join(parts[:-1]) + f", & {parts[-1]}"


def _initials_apa(given: str) -> str:
    parts = re.split(r"[\s\-.]+", given.strip())
    return " ".join(f"{p[0].upper()}." for p in parts if p and p[0].isalpha())


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).strip(" .;,")


def _type_tag(entity: dict) -> str:
    t = (entity.get("type") or "J").strip().upper()
    if t in ("A", "PREPRINT") or entity.get("arxiv") and not entity.get("venue"):
        return "A"
    return TYPE_TAG.get(t, "J")


def _doi_suffix(entity: dict) -> str:
    doi = entity.get("doi") or ""
    return f" DOI: {doi}." if doi else ""


def format_entry_gb(entity: dict, number: int | None = None) -> str:
    prefix = f"[{number}] " if number is not None else ""
    authors = authors_gb(entity.get("authors") or [], entity.get("lang", "en"))
    title = _clean(entity.get("title"))
    alt = _clean(entity.get("title_alt"))
    if entity.get("lang") == "en" and alt and _is_cjk(alt):
        title = f"{title}. {alt}"
    venue = _clean(entity.get("venue"))
    year = entity.get("year") or ""
    volume = _clean(str(entity.get("volume") or ""))
    issue = _clean(str(entity.get("issue") or ""))
    pages = _clean(str(entity.get("pages") or ""))
    tag = _type_tag(entity)

    seg = []
    if authors:
        seg.append(authors + ".")
    if title:
        seg.append(f"{title}[{tag}].")

    if tag == "A":
        arxiv = entity.get("arxiv") or ""
        tail = f"arXiv:{arxiv}" if arxiv else venue
        parts = [p for p in (tail, str(year)) if p]
        seg.append(", ".join(parts) + "." + _doi_suffix(entity))
        return prefix + " ".join(seg)

    vol_issue = volume
    if issue:
        vol_issue = f"{volume}({issue})" if volume else f"({issue})"
    tail_bits = [p for p in (venue, str(year), vol_issue) if p]
    tail = ", ".join(tail_bits)
    if pages:
        tail = f"{tail}: {pages}" if tail else pages
    if tail:
        seg.append(tail + ".")
    if entity.get("doi"):
        seg.append(f"DOI: {entity['doi']}.")
    return prefix + " ".join(seg)


def format_entry_apa(entity: dict, suffix: str = "") -> str:
    authors = authors_apa(entity.get("authors") or [])
    year = entity.get("year") or "n.d."
    year = f"{year}{suffix}" if suffix else str(year)
    title = _clean(entity.get("title"))
    alt = _clean(entity.get("title_alt"))
    if entity.get("lang") == "en" and alt and _is_cjk(alt):
        title = f"{title} [{alt}]"
    venue = _clean(entity.get("venue"))
    volume = _clean(str(entity.get("volume") or ""))
    issue = _clean(str(entity.get("issue") or ""))
    pages = _clean(str(entity.get("pages") or ""))

    head = f"{authors} " if authors else ""
    out = f"{head}({year}). {title}."
    if _type_tag(entity) == "A":
        arxiv = entity.get("arxiv") or ""
        if arxiv:
            out += f" arXiv. https://arxiv.org/abs/{arxiv}"
        elif entity.get("doi"):
            out += f" https://doi.org/{entity['doi']}"
        return out
    if venue:
        out += f" {venue}"
        if volume:
            out += f", {volume}"
            if issue:
                out += f"({issue})"
        if pages:
            out += f", {pages}"
        out += "."
    if entity.get("doi"):
        out += f" https://doi.org/{entity['doi']}"
    return out


def num_to_sup(n: int) -> str:
    return "".join(SUP_DIGITS[int(d)] for d in str(n))


def collapse_numbers(nums: list[int]) -> str:
    """1,2,3,7 -> '1-3,7' (runs of >=3 collapse; 2-long runs stay explicit)."""
    nums = sorted(set(int(n) for n in nums))
    if not nums:
        return ""
    out = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append((start, prev))
        start = prev = n
    out.append((start, prev))
    parts = []
    for a, b in out:
        if b - a >= 2:
            parts.append(f"{a}-{b}")
        elif b - a == 1:
            parts.extend([str(a), str(b)])
        else:
            parts.append(str(a))
    return ",".join(parts)


def format_marker(numbers: list[int], kind: str, style: str) -> str:
    if kind == "author-year" or style == "apa":
        return ""  # rendered elsewhere
    joined = collapse_numbers(numbers)
    if not joined:
        return ""
    if kind == "superscript":
        if "-" in joined:
            head, _, tail = joined.partition("-")
            return num_to_sup(int(head)) + SUP_MINUS + num_to_sup(int(tail))
        return ",".join(num_to_sup(int(x)) for x in joined.split(",") if x)
    return f"[{joined}]"


def format_marker_author_year(entities: list[dict], style: str,
                              suffixes: dict | None = None) -> str:
    suffixes = suffixes or {}
    bits = []
    for e in entities:
        fam = (e.get("authors") or [""])[0]
        fam, _ = _split_name(fam)
        fam = fam or e.get("title", "")[:12]
        sfx = suffixes.get(e.get("entity_key"), "")
        year = f"{e.get('year') or 'n.d.'}{sfx}"
        bits.append(f"{fam}, {year}")
    if not bits:
        return ""
    return "(" + "; ".join(bits) + ")"


def raw_entry_text(entity: dict) -> str:
    raw = (entity.get("raw_entry") or "").strip()
    return re.sub(r"^\s*\[\d{1,3}\]\s*", "", raw)


def is_renderable(entity: dict) -> bool:
    """Only rebuild an entry from metadata when the metadata can actually carry it.

    An entry that the library cannot back (unknown work, or record fields too thin)
    keeps its original wording verbatim: dropping fields would silently damage the
    author's text, which is worse than leaving it as-is.
    """
    core = all(entity.get(f) for f in ("title", "authors", "year"))
    return bool(entity.get("record_ids")) and core and bool(
        entity.get("venue") or entity.get("arxiv")
    )


def render_reference_list(entities: list[dict], style: str, numbers: dict[str, int],
                          uncited: list[str] | None = None,
                          suffixes: dict | None = None) -> str:
    """Render the whole reference section body (no heading)."""
    suffixes = suffixes or {}
    if style == "apa":
        ordered = sorted(entities, key=lambda e: (
            ((e.get("authors") or [""])[0] or "").lower(), e.get("year") or 0, e.get("title", "")
        ))
        out = []
        for e in ordered:
            if is_renderable(e):
                out.append(format_entry_apa(e, suffixes.get(e["entity_key"], "")))
            else:
                out.append(raw_entry_text(e))
        return "\n".join(out)

    out = []
    for e in sorted(entities, key=lambda x: numbers.get(x["entity_key"], 10**6)):
        n = numbers.get(e["entity_key"])
        if n is None:
            continue
        out.append(format_entry_gb(e, n) if is_renderable(e) else f"[{n}] {raw_entry_text(e)}")
    for key in uncited or []:
        e = next((x for x in entities if x["entity_key"] == key), None)
        if e:
            n = numbers.get(key)
            out.append(format_entry_gb(e, n) if is_renderable(e) else f"[{n}] {raw_entry_text(e)}")
    return "\n".join(out)
