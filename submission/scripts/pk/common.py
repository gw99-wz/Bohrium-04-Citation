"""Shared primitives: IO, text normalisation, similarity, sentence/offset utilities.

Stdlib only. Every function is deterministic: no randomness, no locale dependence,
no wall-clock in any value that feeds the output (timestamps live in a separate
meta block so re-runs stay byte-comparable).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- text constants

CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"

STOP_EN = {
    "the", "and", "for", "with", "from", "that", "this", "these", "those", "are",
    "was", "were", "has", "have", "had", "not", "but", "its", "their", "our",
    "into", "than", "then", "there", "which", "who", "whom", "how", "why",
    "using", "used", "use", "based", "between", "among", "over", "under",
    "can", "may", "also", "such", "via", "per", "both", "each", "more", "most",
}

STOP_ZH = {
    "研究", "方法", "结果", "本文", "我们", "以及", "通过", "进行", "可以",
    "显示", "表明", "分析", "基于", "不同", "其中", "由于", "因此", "然而",
}

FULLWIDTH = str.maketrans(
    {
        "，": ",", "。": ".", "；": ";", "：": ":", "（": "(", "）": ")",
        "［": "[", "］": "]", "｛": "{", "｝": "}", "、": ",",
        "－": "-", "—": "-", "–": "-", "～": "~", "　": " ",
        "“": '"', "”": '"', "‘": "'", "’": "'",
    }
)

SUPERSCRIPT = {
    "\u2070": "0", "\u00b9": "1", "\u00b2": "2", "\u00b3": "3", "\u2074": "4",
    "\u2075": "5", "\u2076": "6", "\u2077": "7", "\u2078": "8", "\u2079": "9",
    "\u207b": "-", "\u207a": "+",
}

# ---------------------------------------------------------------- encoding / IO

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1")


def read_text(path: str | os.PathLike) -> tuple[str, str]:
    """Read a text file, tolerating UTF-8 / GB18030 / BOM. Returns (text, encoding)."""
    raw = Path(path).read_bytes()
    for enc in _ENCODINGS:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if enc == "latin-1" and _looks_like_mojibake(text):
            continue
        return text.replace("\r\n", "\n").replace("\r", "\n"), enc
    return raw.decode("utf-8", "replace").replace("\r\n", "\n"), "utf-8-replace"


def _looks_like_mojibake(text: str) -> bool:
    """Heuristic: Latin-1 decoding of UTF-8 Chinese looks like Ã¤Â¸Â­ etc."""
    return text.count("Ã") + text.count("Â") > 3


def load_json(path: str | os.PathLike, default=None):
    try:
        text, _ = read_text(path)
        return json.loads(text)
    except Exception:
        return default


def dump_json(obj, path: str | os.PathLike) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(p)
    return str(p)


def write_text(text: str, path: str | os.PathLike) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")
    return str(p)


def dump_jsonl(rows, path: str | os.PathLike) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False) for r in rows]
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return str(p)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- normalisation

def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def norm_text(text: str) -> str:
    """Lowercase, fold width, collapse whitespace. Safe for matching only."""
    t = nfkc(text or "").translate(FULLWIDTH)
    t = re.sub(r"[\u00ad\u200b\u200c\u200d\ufeff]", "", t)
    t = t.lower()
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def norm_key(text: str) -> str:
    """Aggressive key: letters/digits/CJK only."""
    t = norm_text(text)
    return re.sub(r"[^0-9a-z\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", "", t)


def strip_doi_prefix(doi: str) -> str:
    d = norm_text(doi or "")
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*|doi\s+)", "", d)
    d = d.strip(" .;,)]}>")
    m = re.match(r"(10\.\d{4,9}/[^\s\"<>]+)", d)
    return m.group(1).rstrip(".,;)") if m else ""


def doi_valid(doi: str) -> bool:
    return bool(re.fullmatch(r"10\.\d{4,9}/[^\s\"<>]{2,}", strip_doi_prefix(doi)))


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = len(re.findall(f"[{CJK}]", text))
    return cjk / max(1, len(re.sub(r"\s", "", text)))


def first_number(text: str):
    m = re.search(r"\d{1,4}", text or "")
    return int(m.group(0)) if m else None


# ---------------------------------------------------------------- similarity

def tokens(text: str) -> set[str]:
    """Latin words + CJK character bigrams. Language-agnostic comparison unit."""
    t = norm_text(text)
    out: set[str] = set()
    for w in re.findall(r"[a-z0-9][a-z0-9\-]{1,}", t):
        if len(w) > 2 and w not in STOP_EN:
            out.add(w)
    for run in re.findall(f"[{CJK}]+", t):
        if len(run) == 1:
            out.add(run)
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if bg not in STOP_ZH:
                out.add(bg)
    return out


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def sim(a: str, b: str) -> float:
    """Title-ish similarity in [0,1]. Returns 0 when either side is empty."""
    if not a or not b:
        return 0.0
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return jaccard(ta, tb)


def containment(needle: str, haystack: str) -> float:
    """Fraction of needle tokens present in haystack (asymmetric, for claim support)."""
    tn, th = tokens(needle), tokens(haystack)
    if not tn:
        return 0.0
    return len(tn & th) / len(tn)


# ---------------------------------------------------------------- sentences

_SENT_END = "。！？；!?;"


def split_sentences(text: str) -> list[tuple[int, int, str]]:
    """Split into (start, end, sentence) with offsets into `text`.

    Keeps offsets exact so citations can be tied back to their owning sentence.
    Newlines also terminate a sentence (list items, captions).
    """
    out: list[tuple[int, int, str]] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _SENT_END or ch == "\n":
            j = i + 1
            # absorb closing quotes/brackets and repeated terminators
            while j < n and text[j] in "”’」』）)]\"'":
                j += 1
            if ch != "\n":
                while j < n and text[j] in _SENT_END:
                    j += 1
            seg = text[start:j]
            if seg.strip():
                out.append((start, j, seg))
            start = j
            i = j
            continue
        i += 1
    if start < n:
        seg = text[start:]
        if seg.strip():
            out.append((start, n, seg))
    return out


def sentence_at(sentences, pos: int):
    for s, e, seg in sentences:
        if s <= pos < e:
            return (s, e, seg)
    return None


# ---------------------------------------------------------------- paths

def resolve_input_path(candidate, bases) -> Path | None:
    """Resolve a possibly-relative path against a list of base dirs. Never raises."""
    if not candidate or not isinstance(candidate, str):
        return None
    cand = Path(candidate.strip().strip('"').strip("'"))
    if cand.is_file():
        return cand
    tried = []
    for base in bases:
        if base is None:
            continue
        p = Path(base) / cand
        tried.append(str(p))
        if p.is_file():
            return p
    # last resort: same basename anywhere under the first base
    for base in bases:
        if base is None:
            continue
        try:
            for p in Path(base).rglob(cand.name):
                if p.is_file():
                    return p
        except Exception:
            continue
    return None


def looks_like_inline_text(value: str) -> bool:
    """A case field may hold the text itself rather than a path."""
    if not isinstance(value, str):
        return False
    if "\n" in value:
        return True
    if len(value) > 180 and not re.search(r"\.(md|txt|json|bib|ris|csv|docx|pdf)$", value.strip(), re.I):
        return True
    return False
