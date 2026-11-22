#!/usr/bin/env python3
"""Self-test suite for citation-toolkit.

Runs entirely in-process (no subprocess pipes) so it works under restrictive sandboxes
and in the evaluation image alike.

    python tests/run_tests.py            # full suite
    python tests/run_tests.py -k dedup   # filter by name

What it proves, in the order a reviewer would care:
  1. the pipeline runs end to end on a messy fixture and on a clean one;
  2. numbering is continuous AND byte-level idempotent (re-run on its own output);
  3. duplicate merging has zero false merges (explicit negative control);
  4. a fabricated entry is caught, and an unresolvable one is preserved verbatim;
  5. DOI network failure degrades to offline without crashing or mislabelling;
  6. every declared output_contract field is present, unknown ones flagged.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from pathlib import Path

SUBMISSION = Path(__file__).resolve().parents[1]
SCRIPTS = SUBMISSION / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _pick_tmp() -> Path:
    """First writable scratch dir: explicit override, system temp, then beside the skill."""
    candidates = []
    if os.environ.get("CITATION_SELFTEST_TMP"):
        candidates.append(Path(os.environ["CITATION_SELFTEST_TMP"]))
    candidates.append(Path(tempfile.gettempdir()) / f"citation-selftest-{os.getpid()}")
    candidates.append(SUBMISSION.parent / ".build" / "selftest")
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return cand
        except Exception:
            continue
    raise RuntimeError("no writable scratch directory for self-test")


TMP = _pick_tmp()
sys.path.insert(0, str(SCRIPTS))

import run as runner  # noqa: E402
from pk import verify as verify_mod  # noqa: E402
from pk.contract import detect_contract  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  — {detail}" if detail else ""))


def run_case(fixture: str | Path, style: str | None = None, network: str = "offline",
             out: Path | None = None) -> tuple[dict, dict]:
    fx = Path(fixture)
    case = fx / "case.json" if fx.is_dir() else fx
    out = out or (TMP / f"out_{abs(hash((str(case), style, network))) % 10**8}")
    work = out.with_name(out.name + "_work")
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    ctx = runner.run(case, out, work, network, style, None)
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    return ctx, result


# ---------------------------------------------------------------- tests

def test_clean_success():
    ctx, res = run_case(FIXTURES / "clean")
    ok = (ctx["status"] == "success"
          and len(ctx["entities"]) == 8
          and ctx["numbering"]["idempotency"]["idempotent"]
          and ctx["numbering"]["continuity"]["ok"]
          and ctx["verify"]["summary"]["fabrication_suspected"] == 0)
    check("clean fixture → success, 8 entities, idempotent, continuous", ok,
          f"status={ctx['status']} entities={len(ctx['entities'])} "
          f"idem={ctx['numbering']['idempotency']['idempotent']}")


def test_broken_partial():
    ctx, _ = run_case(FIXTURES / "broken")
    ok = (ctx["status"] == "partial"
          and len(ctx["entities"]) == 9
          and ctx["dedup_stats"]["duplicates_collapsed"] == 3
          and ctx["verify"]["summary"]["fabrication_suspected"] == 1)
    check("broken fixture → partial, 3 collapsed, 1 fabrication", ok,
          f"status={ctx['status']} refs={len(ctx['entities'])} "
          f"collapsed={ctx['dedup_stats']['duplicates_collapsed']} "
          f"fabricated={ctx['verify']['summary']['fabrication_suspected']}")


def test_merge_precision():
    ctx, _ = run_case(FIXTURES / "broken")
    merged = {frozenset((d["a_index"], d["b_index"]))
              for d in ctx["decisions"] if d["decision"] == "merged"}
    expected = {frozenset({3, 9}), frozenset({4, 10}), frozenset({5, 7})}
    false_merges = merged - expected
    rate = len(false_merges) / max(1, len(merged))
    ok = merged == expected and not false_merges
    check("dedup precision: exactly 3 expected merges, 0 false merges", ok,
          f"merged={sorted(tuple(sorted(m)) for m in merged)} "
          f"误合并率={rate:.3f}")


def test_negative_control():
    ctx, _ = run_case(FIXTURES / "broken")
    pair = [d for d in ctx["decisions"] if {d["a_index"], d["b_index"]} == {2, 12}]
    ok = bool(pair) and pair[0]["decision"] == "distinct" and pair[0].get("veto")
    detail = ""
    if pair:
        veto = ",".join(f"{v['field']}:{v['relation']}" for v in pair[0]["veto"])
        detail = f"decision={pair[0]['decision']} veto={veto}"
    check("negative control: same-author same-year pair stays distinct", ok, detail)


def test_contract_declared():
    _, res = run_case(FIXTURES / "clean")
    declared = ["case_id", "status", "manuscript", "references",
                "merge_decisions", "citation_ledger", "uncertainty"]
    missing = [f for f in declared if f not in res]
    nullish = [f for f in declared if res.get(f) in (None, "", [], {})]
    ok = not missing and not nullish
    check("contract: every declared field present and populated", ok,
          f"missing={missing} empty={nullish}")


def test_contract_unknown():
    _, res = run_case(FIXTURES / "broken")
    rep = res.get("envelope", {}).get("contract", {})
    unresolved = rep.get("unresolved", [])
    resolved = {r["field"]: r["resolved_via"] for r in rep.get("resolved", [])}
    # an unknown field is present as null and reported; a synonym-ish one is resolved
    ok = ("quantum_flux_index" in res and res["quantum_flux_index"] is None
          and "quantum_flux_index" in unresolved
          and resolved.get("dedupe_report") == "synonym")
    check("contract: unknown field → null + reported; alias field resolved by synonym", ok,
          f"unresolved={unresolved} dedupe_report={resolved.get('dedupe_report')}")


def test_offline_degrade():
    ctx, res = run_case(FIXTURES / "clean", network="offline")
    net = ctx["verify"]["network"]
    metrics = res["envelope"]["metrics"]
    ok = (net["mode"] == "offline_degraded"
          and net["attempted"] == 0
          and metrics["network_mode"] == "offline_degraded"
          and ctx["status"] == "success")
    check("offline degradation: --no-network → offline_degraded, run still succeeds", ok,
          f"mode={net['mode']} attempted={net['attempted']} status={ctx['status']}")


def test_network_failure_no_crash():
    ctx, _ = run_case(FIXTURES / "clean")
    entities = ctx["entities"]
    original = verify_mod.check_doi
    calls = {"n": 0}

    def boom(doi, timeout=2.5):
        calls["n"] += 1
        return {"doi": doi, "status": "network_unavailable", "detail": "simulated-offline"}

    verify_mod.check_doi = boom
    try:
        report = verify_mod.run_doi_checks(entities, mode="auto")
        raised = None
    except Exception as exc:  # must never happen
        report, raised = {}, exc
    finally:
        verify_mod.check_doi = original

    ok = (raised is None
          and report.get("network_unavailable") is True
          and report.get("mode") == "offline_degraded"
          and calls["n"] <= 10)
    check("network failure → network_unavailable, no exception, stops early", ok,
          f"raised={raised} calls={calls['n']} mode={report.get('mode')}")


def test_network_failure_not_fabrication():
    """A DOI we could not resolve must stay 'unresolved', never become 'fabricated'."""
    ctx, _ = run_case(FIXTURES / "clean", network="offline")
    baseline_fab = ctx["verify"]["summary"]["fabrication_suspected"]
    original = verify_mod.check_doi
    verify_mod.check_doi = lambda doi, timeout=2.5: {
        "doi": doi, "status": "network_unavailable", "detail": "simulated"}
    try:
        ctx2, _ = run_case(FIXTURES / "clean", network="auto",
                           out=TMP / "out_netfail")
    finally:
        verify_mod.check_doi = original
    ok = (ctx2["verify"]["summary"]["fabrication_suspected"] == baseline_fab
          and ctx2["status"] == "success")
    check("network failure never reclassifies a reference as fabricated", ok,
          f"fab offline={baseline_fab} fab netfail={ctx2['verify']['summary']['fabrication_suspected']} "
          f"status={ctx2['status']}")


def test_idempotent_files():
    ctx1, _ = run_case(FIXTURES / "clean", out=TMP / "idem_a")
    ctx2, _ = run_case(FIXTURES / "clean", out=TMP / "idem_b")
    a = (TMP / "idem_a" / "manuscript_fixed.md").read_bytes()
    b = (TMP / "idem_b" / "manuscript_fixed.md").read_bytes()
    ok = a == b and ctx1["numbering"]["idempotency"]["idempotent"]
    check("idempotency: two runs produce byte-identical manuscripts", ok,
          f"equal={a == b} bytes={len(a)}")


def test_apa_mode():
    ctx, _ = run_case(FIXTURES / "clean", style="apa", out=TMP / "out_apa")
    text = (TMP / "out_apa" / "manuscript_fixed.md").read_text(encoding="utf-8")
    body = text.split("## References")[0]
    ok = (ctx["status"] == "success"
          and ctx["numbering"]["idempotency"]["idempotent"]
          and "(Wang, 2020a)" in body
          and "(Wang, 2020b)" in body
          and "[1]" not in body)
    check("APA mode: author-year markers with a/b disambiguation, no [n]", ok,
          f"a/b={'(Wang, 2020a)' in body}/{'(Wang, 2020b)' in body} status={ctx['status']}")


def test_dangling_marker():
    d = TMP / "dangling"
    d.mkdir(parents=True, exist_ok=True)
    (d / "case.json").write_text(json.dumps({
        "case_id": "selftest-dangling",
        "manuscript": "m.md", "library": "l.json",
        "output_contract": {"case_id": "s", "status": "s", "manuscript": "s"},
    }, ensure_ascii=False), encoding="utf-8")
    (d / "m.md").write_text(
        "# T\n\n有来源的句子[1]。无对应条目的句子[99]。\n\n## 参考文献\n\n"
        "[1] 张伟. 二维半导体光致发光研究进展[J]. 物理学报, 2021, 70(8): 087801.\n",
        encoding="utf-8")
    (d / "l.json").write_text(json.dumps([{
        "title": "二维半导体光致发光研究进展", "authors": ["张伟"], "journal": "物理学报",
        "year": 2021, "volume": "70", "issue": "8", "pages": "087801"}], ensure_ascii=False),
        encoding="utf-8")
    ctx, _ = run_case(d, out=TMP / "out_dangling")
    text = (TMP / "out_dangling" / "manuscript_fixed.md").read_text(encoding="utf-8")
    kinds = [u["kind"] for u in ctx["verify"].get("uncertainty", [])] if "uncertainty" in ctx["verify"] else []
    env_kinds = [u["kind"] for u in json.loads(
        (TMP / "out_dangling" / "result.json").read_text(encoding="utf-8")
    )["envelope"]["uncertainty"]]
    ok = "[99]" in text and "dangling_marker" in env_kinds
    check("dangling marker preserved verbatim and reported", ok,
          f"kept={'[99]' in text} reported={'dangling_marker' in env_kinds}")


def test_no_library():
    d = TMP / "nolib"
    d.mkdir(parents=True, exist_ok=True)
    (d / "case.json").write_text(json.dumps({
        "case_id": "selftest-nolib", "manuscript": "m.md",
        "output_contract": {"case_id": "s", "status": "s"},
    }, ensure_ascii=False), encoding="utf-8")
    (d / "m.md").write_text(
        "# T\n\n句子[1]。\n\n## 参考文献\n\n[1] 某人. 某文[J]. 某刊, 2020, 1(1): 1-2.\n",
        encoding="utf-8")
    ctx, res = run_case(d, out=TMP / "out_nolib")
    ok = (ctx["status"] in ("partial", "unable")
          and (res.get("case_id") == "selftest-nolib"))
    check("missing library → graceful partial, result.json still written", ok,
          f"status={ctx['status']}")


def test_no_manuscript():
    d = TMP / "noms"
    d.mkdir(parents=True, exist_ok=True)
    (d / "case.json").write_text(json.dumps({"case_id": "selftest-empty"}), encoding="utf-8")
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = runner.main(["--input", str(d / "case.json"), "--out",
                            str(TMP / "out_noms"), "--work", str(TMP / "out_noms_work")])
    ok = code == 2
    check("missing manuscript → exit code 2 (unable), no traceback", ok, f"exit={code}")


def test_garbage_input():
    d = TMP / "garbage"
    d.mkdir(parents=True, exist_ok=True)
    (d / "case.json").write_text("{ this is not json ", encoding="utf-8")
    try:
        ctx, res = run_case(d, out=TMP / "out_garbage")
        ok = ctx["status"] == "unable" and "case_id" in res
        detail = f"status={ctx['status']}"
    except Exception as exc:
        ok, detail = False, f"raised {type(exc).__name__}: {exc}"
    check("malformed case.json → unable, no crash", ok, detail)


def test_numbers_continuous():
    ctx, _ = run_case(FIXTURES / "clean")
    nums = sorted(ctx["numbering"]["numbers"].values())
    expected = list(range(1, len(ctx["entities"]) + 1))
    check("numbering covers 1..N with no gaps or duplicates", nums == expected,
          f"numbers={nums}")


def test_ledger_complete():
    ctx, _ = run_case(FIXTURES / "clean")
    ledger = ctx["verify"]["ledger"]
    occ = ctx["numbering"]["occurrences"]
    allowed = {"ok", "needs_review", "fail"}
    ok = (len(ledger) == len(occ)
          and all(r["verdict"] in allowed for r in ledger)
          and all(r["entity_key"] and r["ref_no"] for r in ledger))
    check("citation ledger: one row per citation, valid verdicts, numbers set", ok,
          f"rows={len(ledger)} occurrences={len(occ)}")


def test_artifact_set():
    run_case(FIXTURES / "clean", out=TMP / "out_art")
    need = {"result.json", "manuscript_fixed.md", "references.json", "references.md",
            "merge_decisions.json", "citation_ledger.jsonl", "citation_ledger.csv",
            "changes.json", "verify_report.md", "run_manifest.json"}
    have = {p.name for p in (TMP / "out_art").iterdir() if p.is_file()}
    check("all declared artifacts written", need <= have, f"missing={sorted(need - have)}")


def test_compliance():
    sub = SUBMISSION
    files = [p for p in sub.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    no_suffix = [str(p.relative_to(sub)) for p in files if p.suffix == ""]
    over = [str(p.relative_to(sub)) for p in files if p.stat().st_size > 500 * 1024]
    total = sum(p.stat().st_size for p in files)
    links = [str(p) for p in sub.rglob("*") if p.is_symlink()]
    ok = (not no_suffix and not over and total <= 2 * 1024 * 1024
          and not links and len(files) <= 50)
    check("submission compliance: ≤50 files, ≤500KB each, ≤2MB, no symlink, no suffixless file",
          ok, f"files={len(files)} total={total / 1024:.0f}KB no_suffix={no_suffix} "
              f"over={over} symlinks={len(links)}")


def test_contract_detection_shapes():
    shapes = [
        [{"name": "case_id"}, {"name": "status"}],
        {"case_id": "string", "status": "enum"},
        ["case_id", "status"],
        {"required_outputs": {"case_id": "x"}},
    ]
    ok = True
    detail = []
    for i, raw in enumerate(shapes):
        case = {"output_contract": raw} if i < 3 else raw
        c = detect_contract(case)
        if not c.names:
            ok = False
        detail.append(f"#{i}:{c.names}")
    check("contract parser accepts dict/list/string/aliased shapes", ok, " ".join(detail))


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    only = None
    if "-k" in argv and argv.index("-k") + 1 < len(argv):
        only = argv[argv.index("-k") + 1]

    TMP.mkdir(parents=True, exist_ok=True)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        if only and only not in fn.__name__:
            continue
        try:
            fn()
        except Exception:
            check(fn.__name__, False, "EXCEPTION: " + traceback.format_exc().splitlines()[-1])

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print("\n" + "=" * 70)
    print(f"RESULT  {passed}/{len(RESULTS)} passed" + (f"  ·  {len(failed)} FAILED" if failed else ""))
    for name, detail in failed:
        print(f"  ✗ {name}: {detail}")
    print("=" * 70)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
