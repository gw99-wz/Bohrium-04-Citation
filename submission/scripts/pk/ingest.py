"""Ingestion: case.json -> manuscript text + reference library records.

The real case payload is not published, so every read here is defensive:
the same logical input may be inline text, a sibling file, a .json/.bib/.ris/.csv,
a .docx or a .pdf. Nothing raises on a malformed shape; each stage degrades and
records what it could not read.
"""

from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from .common import (
    cjk_ratio,
    load_json,
    looks_like_inline_text,
    norm_key,
    read_text,
    resolve_input_path,
    strip_doi_prefix,
)

# ------------------------------------------------------------------ field maps

FIELD_MAP: dict[str, tuple[str, ...]] = {
    "title": ("title", "Title", "TI", "T1", "article-title", "题名", "标题", "篇名", "题目"),
    "title_alt": (
        "title_zh", "title_cn", "title_translated", "translated_title", "original_title",
        "中文题名", "中文标题", "译名", "英文题名", "title_en",
    ),
    "authors": ("author", "authors", "Author", "AU", "A1", "作者", "著者"),
    "year": ("year", "Year", "date", "PY", "Y1", "年份", "出版年", "年"),
    "venue": (
        "journal", "journal_name", "venue", "JO", "JF", "T2", "booktitle", "publisher",
        "container-title", "刊名", "期刊", "期刊名称", "来源", "出版物", "会议名称",
    ),
    "volume": ("volume", "Volume", "VL", "卷", "卷号"),
    "issue": ("issue", "number", "IS", "期", "期号"),
    "pages": ("pages", "page", "SP", "EP", "页码", "起止页", "页"),
    "doi": ("doi", "DOI", "Doi"),
    "arxiv": ("arxiv", "arxiv_id", "eprint", "arXiv"),
    "type": ("type", "entry_type", "document_type", "itemType", "文献类型", "类型"),
    "abstract": ("abstract", "Abstract", "AB", "summary", "摘要", "内容提要"),
    "url": ("url", "URL", "link", "链接"),
    "lang": ("language", "lang", "语言"),
    "db": ("source", "database", "db", "来源数据库", "数据库"),
}

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>\]\),;]+")
ARXIV_RE = re.compile(r"(?:arxiv[:\s/]*)?(\d{4}\.\d{4,5})(?:v\d+)?", re.I)

CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"

# ------------------------------------------------------------------ generic

def _pick(mapping: dict, keys: tuple[str, ...]):
    if not isinstance(mapping, dict):
        return None
    for want in keys:
        if want in mapping and mapping[want] not in (None, "", [], {}):
            return mapping[want]
    low = {str(k).lower(): k for k in mapping}
    for want in keys:
        k = low.get(want.lower())
        if k is not None and mapping[k] not in (None, "", [], {}):
            return mapping[k]
    return None


def _as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return ", ".join(_as_text(x) for x in v if _as_text(x))
    if isinstance(v, dict):
        for k in ("name", "text", "value", "title", "family", "literal"):
            if k in v:
                return _as_text(v[k])
        return ""
    return str(v)


def _as_authors(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        out = []
        for item in v:
            if isinstance(item, dict):
                fam = item.get("family") or item.get("last") or ""
                giv = item.get("given") or item.get("first") or ""
                literal = item.get("literal") or item.get("name") or ""
                name = " ".join(x for x in (fam, giv) if x) or literal
                if name:
                    out.append(name.strip())
            else:
                s = _as_text(item)
                if s:
                    out.append(s)
        return out
    s = _as_text(v)
    if not s:
        return []
    parts = re.split(r"\s*(?:;|；|\band\b|&)\s*", s)
    return [p.strip() for p in parts if p and p.strip()]


def first_author_key(authors: list[str]) -> str:
    if not authors:
        return ""
    a = authors[0] or ""
    if re.search(f"[{CJK}]", a):
        # Chinese name: surname is the first character (occasionally two)
        return re.sub(r"\s", "", a)[:2]
    a = re.sub(r"[^A-Za-z\s\-\u00c0-\u024f]", "", a).strip()
    if not a:
        return ""
    if "," in a:  # "Smith, John"
        return norm_key(a.split(",")[0])
    toks = [t for t in a.split() if t]
    return norm_key(toks[-1]) if toks else ""


def normalize_record(raw: dict, rid: str, source: str = "") -> dict:
    rec: dict = {"id": rid, "provenance": {"source": source}, "raw": raw}

    title = _as_text(_pick(raw, FIELD_MAP["title"]))
    title_alt = _as_text(_pick(raw, FIELD_MAP["title_alt"]))
    authors = _as_authors(_pick(raw, FIELD_MAP["authors"]))
    year = _pick(raw, FIELD_MAP["year"])
    venue = _as_text(_pick(raw, FIELD_MAP["venue"]))
    volume = _as_text(_pick(raw, FIELD_MAP["volume"]))
    issue = _as_text(_pick(raw, FIELD_MAP["issue"]))
    pages = _as_text(_pick(raw, FIELD_MAP["pages"]))
    doi = _as_text(_pick(raw, FIELD_MAP["doi"]))
    arxiv = _as_text(_pick(raw, FIELD_MAP["arxiv"]))
    rtype = _as_text(_pick(raw, FIELD_MAP["type"]))
    abstract = _as_text(_pick(raw, FIELD_MAP["abstract"]))
    url = _as_text(_pick(raw, FIELD_MAP["url"]))
    lang = _as_text(_pick(raw, FIELD_MAP["lang"]))
    db = _as_text(_pick(raw, FIELD_MAP["db"])) or source

    # year may arrive as "2020-05-01" or "2020."
    y = None
    if year is not None:
        m = re.search(r"(19|20)\d{2}", str(year))
        if m:
            y = int(m.group(0))

    # DOI / arXiv are often buried in the URL or the raw blob
    if not doi:
        for blob in (url, title, venue, json.dumps(raw, ensure_ascii=False)):
            m = DOI_RE.search(blob or "")
            if m:
                doi = m.group(0)
                break
    doi = strip_doi_prefix(doi)
    if not arxiv:
        m = ARXIV_RE.search(url or "") or ARXIV_RE.search(json.dumps(raw, ensure_ascii=False))
        if m:
            arxiv = m.group(1)

    if rtype:
        rtype = rtype.strip().strip("[]").upper()
        rtype = {"JOURNAL-ARTICLE": "J", "ARTICLE": "J", "JOUR": "J", "CONFERENCE": "C",
                 "CONFERENCE-PAPER": "C", "CPAPER": "C", "BOOK": "M", "THESIS": "D",
                 "DISSERTATION": "D", "PREPRINT": "A", "REPORT": "R"}.get(rtype, rtype[:1])

    if not lang:
        lang = "zh" if cjk_ratio(title + title_alt) > 0.3 else "en"

    rec.update(
        {
            "title": title,
            "title_alt": title_alt,
            "authors": authors,
            "year": y,
            "venue": venue,
            "volume": volume,
            "issue": issue,
            "pages": pages,
            "doi": doi,
            "arxiv": arxiv,
            "type": rtype or "J",
            "abstract": abstract,
            "url": url,
            "lang": lang,
            "db": db,
            "first_author": first_author_key(authors),
        }
    )
    rec["missing"] = [
        f for f in ("title", "authors", "year", "venue", "doi")
        if not rec.get(f)
    ]
    return rec


# ------------------------------------------------------------------ library readers

def parse_bib(text: str) -> list[dict]:
    out: list[dict] = []
    for m in re.finditer(r"@(\w+)\s*\{\s*([^,]+),", text):
        body_start = m.end()
        depth, i = 1, body_start
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        body = text[body_start : i - 1]
        fields: dict = {"_entry": m.group(1).lower(), "_key": m.group(2).strip()}
        for fm in re.finditer(r"(\w+)\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\}|\"[^\"]*\"|[^,\n]+)", body):
            key = fm.group(1).lower()
            val = fm.group(2).strip().rstrip(",").strip()
            if val.startswith("{") and val.endswith("}"):
                val = val[1:-1]
            val = val.strip('"').strip()
            fields[key] = re.sub(r"\s+", " ", val).strip()
        out.append(fields)
    return out


def parse_ris(text: str) -> list[dict]:
    records, cur = [], {}
    for line in text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])\s{2}-\s*(.*)$", line)
        if not m:
            continue
        tag, val = m.group(1), m.group(2).strip()
        if tag == "ER":
            if cur:
                records.append(cur)
            cur = {}
            continue
        if tag in ("AU", "A1"):
            cur.setdefault("AU", []).append(val)
        elif tag == "TI" or tag == "T1":
            cur.setdefault("TI", val)
        elif tag == "JO" or tag == "T2" or tag == "JF":
            cur.setdefault("JO", val)
        elif tag == "PY" or tag == "Y1":
            cur.setdefault("PY", val)
        else:
            cur.setdefault(tag, val)
    if cur:
        records.append(cur)
    mapped = []
    for r in records:
        d = {"TI": r.get("TI"), "AU": r.get("AU"), "JO": r.get("JO"), "PY": r.get("PY")}
        for k, v in r.items():
            if k not in d:
                d[k] = v
        mapped.append(d)
    return mapped


def _records_from_json(payload) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("records", "references", "refs", "library", "items", "data", "entries"):
            v = payload.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                inner = _records_from_json(v)
                if inner:
                    return inner
        # a dict keyed by citation key
        vals = [v for v in payload.values() if isinstance(v, dict)]
        if vals and len(vals) == len(payload):
            return vals
    return []


def load_library_file(path: Path) -> tuple[list[dict], str]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = load_json(path, None)
        return _records_from_json(payload), "json"
    text, enc = read_text(path)
    if suffix in (".bib",) or text.lstrip().startswith("@"):
        return parse_bib(text), f"bibtex[{enc}]"
    if suffix in (".ris",) or re.search(r"^TY\s{2}-", text, re.M):
        return parse_ris(text), f"ris[{enc}]"
    if suffix in (".csv", ".tsv"):
        delim = "\t" if suffix == ".tsv" else ","
        try:
            rows = list(csv.DictReader(io.StringIO(text), delimiter=delim))
            return [r for r in rows if any(v for v in r.values())], f"csv[{enc}]"
        except Exception:
            pass
    # loose text: one record per non-empty line
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if len(line) > 30:
            rows.append({"raw": line, "title": line})
    return rows, f"text[{enc}]"


# ------------------------------------------------------------------ manuscript readers

def docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    txt = re.sub(r"<[^>]+>", "", xml)
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'")):
        txt = txt.replace(ent, ch)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


def pdf_text(path: Path) -> tuple[str, str]:
    """Try pdftotext (file in / file out, no pipes), then PyMuPDF."""
    try:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out.txt"
            proc = subprocess.run(
                ["pdftotext", "-layout", "-enc", "UTF-8", str(path), str(out)],
                timeout=60, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if proc.returncode == 0 and out.is_file():
                text, _ = read_text(out)
                if text.strip():
                    return text, "pdftotext"
    except Exception:
        pass
    try:
        import fitz  # type: ignore

        doc = fitz.open(str(path))
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        if text.strip():
            return text, "pymupdf"
    except Exception:
        pass
    return "", "unavailable"


def load_document(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        try:
            return docx_text(path), "docx"
        except Exception as exc:
            return "", f"docx-error:{type(exc).__name__}"
    if suffix == ".pdf":
        return pdf_text(path)
    if suffix == ".json":
        payload = load_json(path, None)
        if isinstance(payload, dict):
            for key in ("text", "manuscript", "content", "body", "draft"):
                if isinstance(payload.get(key), str):
                    return payload[key], "json-inline"
        text, enc = read_text(path)
        return text, f"json-raw[{enc}]"
    text, enc = read_text(path)
    return text, f"text[{enc}]"


# ------------------------------------------------------------------ case loading

MANUSCRIPT_KEYS = (
    "manuscript", "manuscript_path", "manuscriptPath", "draft", "draft_path", "text",
    "article", "article_text", "paper", "paper_path", "content", "body", "source_text",
    "input", "document", "稿件", "正文", "论文",
)
LIBRARY_KEYS = (
    "library", "library_path", "references", "refs", "reference_list", "bibliography",
    "bib", "bib_path", "records", "sources", "文献库", "参考文献", "文献",
)

SCAN_DOC_EXT = (".md", ".txt", ".docx", ".pdf", ".markdown", ".rst")
SCAN_LIB_EXT = (".bib", ".ris", ".json", ".csv", ".tsv", ".txt")


def _deep_pick(case: dict, keys: tuple[str, ...]):
    """Check the case object, then one level of nesting (e.g. materials/files/input)."""
    for key in keys:
        if key in case and case[key] not in (None, "", [], {}):
            return case[key], key
    low = {str(k).lower(): k for k in case}
    for key in keys:
        k = low.get(key.lower())
        if k is not None and case[k] not in (None, "", [], {}):
            return case[k], k
    for nest_key in ("materials", "files", "input", "inputs", "assets", "data", "resources"):
        nest = case.get(nest_key)
        if isinstance(nest, dict):
            for key in keys:
                if key in nest and nest[key] not in (None, "", [], {}):
                    return nest[key], f"{nest_key}.{key}"
    return None, None


def load_case(path: Path) -> dict:
    payload = load_json(path, None)
    if isinstance(payload, list):
        payload = {"cases": payload}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("_case_path", str(path))
    return payload


def load_manuscript(case: dict, case_dir: Path):
    """Return (text, meta). meta explains where it came from and how it was read."""
    value, key = _deep_pick(case, MANUSCRIPT_KEYS)
    bases = [case_dir, Path("/input"), Path.cwd()]

    if isinstance(value, dict):
        inner = value.get("text") or value.get("content")
        if isinstance(inner, str) and looks_like_inline_text(inner):
            return inner, {"source": f"case:{key}.text", "reader": "inline", "path": None}
        value = value.get("path") or value.get("file") or value.get("href")

    if isinstance(value, str) and value.strip():
        if looks_like_inline_text(value):
            return value, {"source": f"case:{key}", "reader": "inline", "path": None}
        path = resolve_input_path(value, bases)
        if path:
            text, reader = load_document(path)
            return text, {"source": f"case:{key}", "reader": reader, "path": str(path)}

    # no usable pointer: scan the case directory
    candidates: list[Path] = []
    try:
        for p in sorted(case_dir.iterdir()):
            if p.name == "case.json" or not p.is_file():
                continue
            if p.suffix.lower() in SCAN_DOC_EXT:
                candidates.append(p)
    except Exception:
        pass
    # prefer names that look like a manuscript
    def rank(p: Path) -> tuple:
        n = p.name.lower()
        return (0 if re.search(r"manuscript|draft|paper|article|main|论文|稿", n) else 1,
                SCAN_DOC_EXT.index(p.suffix.lower()) if p.suffix.lower() in SCAN_DOC_EXT else 9,
                n)

    for p in sorted(candidates, key=rank):
        text, reader = load_document(p)
        if text.strip():
            return text, {"source": "scan:case-dir", "reader": reader, "path": str(p)}

    return "", {"source": "missing", "reader": "none", "path": None}


def load_library(case: dict, case_dir: Path):
    """Return (records, meta). Records are already normalised."""
    value, key = _deep_pick(case, LIBRARY_KEYS)
    bases = [case_dir, Path("/input"), Path.cwd()]
    raw_records: list[dict] = []
    reader = "none"
    source = "missing"

    if isinstance(value, list):
        raw_records, reader, source = [x for x in value if isinstance(x, dict)], "inline", f"case:{key}"
    elif isinstance(value, dict):
        raw_records = _records_from_json(value)
        reader, source = "inline", f"case:{key}"
        if not raw_records:
            path = value.get("path") or value.get("file")
            if isinstance(path, str):
                p = resolve_input_path(path, bases)
                if p:
                    raw_records, reader = load_library_file(p)
                    source = f"case:{key}.path"
    elif isinstance(value, str) and value.strip():
        p = resolve_input_path(value, bases)
        if p:
            raw_records, reader = load_library_file(p)
            source = f"case:{key}"
        elif looks_like_inline_text(value):
            text = value
            if text.lstrip().startswith("@"):
                raw_records, reader = parse_bib(text), "inline-bibtex"
            elif re.search(r"^TY\s{2}-", text, re.M):
                raw_records, reader = parse_ris(text), "inline-ris"
            else:
                raw_records, reader = _records_from_json(load_json_str(text)), "inline-json"
            source = f"case:{key}"

    if not raw_records:
        candidates: list[Path] = []
        try:
            for p in sorted(case_dir.iterdir()):
                if p.name == "case.json" or not p.is_file():
                    continue
                if p.suffix.lower() in SCAN_LIB_EXT:
                    candidates.append(p)
        except Exception:
            pass

        def rank(p: Path) -> tuple:
            n = p.name.lower()
            hit = re.search(r"librar|ref|bib|citation|文献", n)
            return (0 if hit else 1, SCAN_LIB_EXT.index(p.suffix.lower()), n)

        for p in sorted(candidates, key=rank):
            recs, rd = load_library_file(p)
            if recs:
                raw_records, reader, source = recs, rd, f"scan:{p.name}"
                break

    records = [normalize_record(r, f"R{i+1}", source) for i, r in enumerate(raw_records)]
    meta = {
        "records": len(records),
        "reader": reader,
        "source": source,
        "complete_metadata": sum(1 for r in records if len(r["missing"]) <= 1),
        "with_doi": sum(1 for r in records if r["doi"]),
    }
    return records, meta


def load_json_str(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


# ------------------------------------------------------------------ manuscript structure

REF_HEADINGS = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?"
    r"(?:参\s*考\s*文\s*献|引\s*用\s*文\s*献|参\s*考\s*资\s*料|文\s*献\s*表|"
    r"references?|bibliography|works\s+cited|reference\s+list|literature\s+cited)"
    r"\s*[:：]?\s*$",
    re.I,
)

ENTRY_NUM_BRACKET = re.compile(r"^\s*\[(\d{1,3})\]\s*(.*)$")
ENTRY_NUM_DOT = re.compile(r"^\s*(\d{1,3})\s*[.、)）]\s*(.*)$")

BRACKET_MARK = re.compile(r"\[\s*(\d{1,3}(?:\s*[-–—,，]\s*\d{1,3})*)\s*\]")
SUP_MARK = re.compile(r"[\u2070\u00b9\u00b2\u00b3\u2074-\u2079]{1,4}")
AUTHOR_YEAR_MARK = re.compile(
    r"[（(]\s*([^（()）]{2,60}?)\s*[,，]\s*((?:19|20)\d{2}[a-z]?)\s*[）)]"
)
SUP_MAP = str.maketrans(
    {"\u2070": "0", "\u00b9": "1", "\u00b2": "2", "\u00b3": "3", "\u2074": "4",
     "\u2075": "5", "\u2076": "6", "\u2077": "7", "\u2078": "8", "\u2079": "9"}
)


def expand_indices(inner: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,，]", inner):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,3})\s*[-–—]\s*(\d{1,3})", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < a <= b and b - a <= 200:
                out.extend(range(a, b + 1))
            continue
        if part.isdigit():
            out.append(int(part))
    seen, dedup = set(), []
    for i in out:
        if i not in seen:
            seen.add(i)
            dedup.append(i)
    return dedup


def parse_manuscript_structure(text: str) -> dict:
    """Split body from the reference section and parse numbered entries."""
    lines = text.split("\n")
    ref_line = None
    for i, line in enumerate(lines):
        if REF_HEADINGS.match(line):
            ref_line = i
    body = text
    ref_text = ""
    heading = ""
    if ref_line is not None:
        heading = lines[ref_line].strip()
        body = "\n".join(lines[:ref_line])
        ref_text = "\n".join(lines[ref_line + 1 :])

    entries: list[dict] = []
    cur = None
    offset = 0
    for line in ref_text.split("\n"):
        stripped = line.strip()
        m = ENTRY_NUM_BRACKET.match(line) or ENTRY_NUM_DOT.match(line)
        if m:
            if cur:
                cur["raw"] = " ".join(cur["raw"].split())
                entries.append(cur)
            cur = {
                "index": int(m.group(1)),
                "raw": m.group(2),
                "start": offset,
            }
        elif cur is not None and stripped:
            cur["raw"] += " " + stripped
        offset += len(line) + 1
    if cur:
        cur["raw"] = " ".join(cur["raw"].split())
        entries.append(cur)

    # de-duplicate repeated index numbers, keep first occurrence order
    by_index: dict[int, dict] = {}
    for e in entries:
        by_index.setdefault(e["index"], e)
    entries = [by_index[k] for k in sorted(by_index)]

    # author-year lists carry no [n] prefix; fall back to one entry per paragraph
    if not entries and ref_text.strip():
        entries = _parse_unnumbered(ref_text)

    return {
        "body": body,
        "references_section": ref_text,
        "heading": heading,
        "has_reference_section": ref_line is not None,
        "entries": entries,
    }


YEAR_IN_TEXT = re.compile(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)")


def _parse_unnumbered(ref_text: str) -> list[dict]:
    """Split an unnumbered (APA/Harvard) reference list into entries."""
    entries: list[dict] = []
    cur: dict | None = None
    offset = 0
    for line in ref_text.split("\n"):
        stripped = line.strip()
        if not stripped:
            offset += len(line) + 1
            continue
        starts_new = bool(YEAR_IN_TEXT.search(stripped)) and (
            cur is None or len(stripped) > 25 or stripped[:1].isupper()
        )
        is_continuation = (
            cur is not None and not starts_new
            and (stripped[:1].islower() or len(stripped) < 25)
        )
        if is_continuation:
            cur["raw"] += " " + stripped
        elif cur is None or starts_new:
            if cur:
                cur["raw"] = " ".join(cur["raw"].split())
                entries.append(cur)
            cur = {"index": len(entries) + 1, "raw": stripped, "start": offset}
        else:
            cur["raw"] += " " + stripped
        offset += len(line) + 1
    if cur:
        cur["raw"] = " ".join(cur["raw"].split())
        entries.append(cur)
    return entries


def find_markers(body: str, valid_indices: set[int]) -> list[dict]:
    """Locate in-text citation markers in the body. Numeric first, then author-year.

    Markers whose numbers have no matching reference entry are returned with
    `resolved: False` instead of being dropped: silently deleting a citation is worse
    than reporting an unresolvable one.
    """
    markers: list[dict] = []
    for m in BRACKET_MARK.finditer(body):
        idx = expand_indices(m.group(1))
        if not idx:
            continue
        resolved = (not valid_indices) or bool(set(idx) & valid_indices)
        markers.append(
            {"start": m.start(), "end": m.end(), "raw": m.group(0), "indices": idx,
             "kind": "numeric", "resolved": resolved}
        )
    if not markers:
        for m in SUP_MARK.finditer(body):
            digits = m.group(0).translate(SUP_MAP)
            idx = expand_indices(digits)
            if not idx:
                continue
            resolved = (not valid_indices) or bool(set(idx) & valid_indices)
            markers.append(
                {"start": m.start(), "end": m.end(), "raw": m.group(0), "indices": idx,
                 "kind": "superscript", "resolved": resolved}
            )
    for m in AUTHOR_YEAR_MARK.finditer(body):
        if any(mk["start"] <= m.start() < mk["end"] for mk in markers):
            continue
        markers.append(
            {
                "start": m.start(),
                "end": m.end(),
                "raw": m.group(0),
                "indices": [],
                "kind": "author-year",
                "author": m.group(1).strip(),
                "year": m.group(2).strip(),
            }
        )
    markers.sort(key=lambda x: x["start"])
    return markers


def detect_style(case: dict, body: str) -> tuple[str, str]:
    """Return (style_id, why). Explicit case hint wins over inference."""
    hinted = ""
    for key in ("style", "citation_style", "target_style", "format", "引用格式", "规范"):
        v = case.get(key)
        if isinstance(v, str) and v.strip():
            hinted = v.strip()
            break
    blob = (hinted + " " + str(case.get("task", ""))[:4000]).lower()

    if re.search(r"gb\s*/?\s*t\s*7714|gb7714|7714", blob):
        return "gb7714", f"hint:{hinted or 'task'}"
    if re.search(r"顺序编码|numerical|numeric|vancouver|数字编号", blob):
        return "gb7714", f"hint:{hinted or 'task'}"
    if re.search(r"著者[—\-－]?出版年|author[- ]year|apa|harvard", blob):
        return "apa", f"hint:{hinted or 'task'}"
    ratio = cjk_ratio(body)
    if ratio > 0.25:
        return "gb7714", f"inferred:cjk_ratio={ratio:.2f}"
    return "apa", f"inferred:cjk_ratio={ratio:.2f}"
