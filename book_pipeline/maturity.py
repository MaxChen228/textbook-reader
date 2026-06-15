#!/usr/bin/env python3
"""Book maturity audit.

This module is the fine-grained counterpart to ``book_pipeline.status``:
``status`` answers "where is the book in the pipeline?", while this answers
"which quality gates make it safe to treat the book as learning-ready?".
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_pipeline.math_audit import audit_book as audit_math
from book_pipeline import status
from book_pipeline.translate import audit_coverage

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "book_pipeline" / "mineru_data"
RAW = ROOT / "raw_pdfs"
SLUG_MAP = ROOT / "book_pipeline" / "slug_map.json"


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_slug_map() -> dict[str, str]:
    data = _read_json(SLUG_MAP) or {}
    return data.get("map", {}) if isinstance(data.get("map"), dict) else {}


def _count_images(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(1 for p in path.iterdir() if p.is_file())


def _chunk_count(path: Path) -> int:
    data = _read_json(path)
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        chunks = data.get("chunks")
        if isinstance(chunks, list):
            return len(chunks)
    return 0


def _smoke_stats(path: Path) -> dict[str, Any]:
    """Parse smoke output by section, not by glyphs or localized wording."""
    if not path.is_file():
        return {"exists": False, "critical": 0, "warning": 0, "head": ""}
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    section = None
    critical = warning = 0
    for line in lines:
        if line.startswith("## ") and "CRITICAL" in line:
            section = "critical"
            continue
        if line.startswith("## ") and "WARNING" in line:
            section = "warning"
            continue
        if line.startswith("## "):
            section = None
            continue
        if line.startswith("- [H") and section == "critical":
            critical += 1
        elif line.startswith("- [H") and section == "warning":
            warning += 1
    return {"exists": True, "critical": critical, "warning": warning, "head": " | ".join(lines[:3])}


def _parsed_stats(slug: str) -> dict[str, int]:
    parsed = DATA / slug / "parsed"
    book = _read_json(parsed / "book.json")
    if not isinstance(book, dict):
        return {"chapters": 0, "appendices": 0, "body_blocks": 0, "problems": 0}
    body_blocks = problems = 0
    stems = [Path(ch["file"]).stem for ch in book.get("chapters", [])]
    stems += [Path(ap["file"]).stem for ap in book.get("appendices", [])]
    for stem in stems:
        chunk = _read_json(parsed / f"{stem}.json")
        if not isinstance(chunk, dict):
            continue
        body_blocks += len(chunk.get("body") or [])
        problems += len(chunk.get("problems") or [])
    return {
        "chapters": len(book.get("chapters") or []),
        "appendices": len(book.get("appendices") or []),
        "body_blocks": body_blocks,
        "problems": problems,
    }


def _translation(slug: str, parsed: bool) -> dict[str, Any]:
    if not parsed:
        return {"status": "na", "translated": 0, "translatable": 0, "ratio": 0.0, "real_miss": 0}
    try:
        report = audit_coverage(slug)
    except SystemExit:
        return {"status": "fail", "translated": 0, "translatable": 0, "ratio": 0.0, "real_miss": 0}
    status_name = "pass" if report["translatable"] and report["real_miss"] == 0 else "fail"
    if report["translatable"] == 0:
        status_name = "na"
    return {
        "status": status_name,
        "translated": report["translated"],
        "translatable": report["translatable"],
        "ratio": report["ratio"],
        "real_miss": report["real_miss"],
        "empty_miss": report["empty_miss"],
    }


def _gate(status_name: str, **extra: Any) -> dict[str, Any]:
    return {"status": status_name, **extra}


def _level(gates: dict[str, dict[str, Any]]) -> str:
    if gates["source"]["status"] != "pass":
        return "L0 source-missing"
    if gates["ocr"]["status"] != "pass":
        return "L0 raw"
    if gates["structure"]["status"] == "missing":
        return "L1 ingested"
    if gates["structure"]["status"] != "pass":
        return "L2 rough-parsed"
    if gates["solution"]["status"] == "fail":
        return "L3 structurally-certified"
    if gates["math"]["status"] == "fail":
        return "L4 solution-certified"
    if gates["translation"]["status"] != "pass":
        return "L4 solution-certified"
    if gates["product"]["status"] != "pass":
        return "L5 translation-certified"
    return "L6 learning-ready"


def _next_action(gates: dict[str, dict[str, Any]]) -> str:
    if gates["source"]["status"] != "pass":
        return "register-or-restore-raw"
    if gates["ocr"]["status"] != "pass":
        return "ingest"
    if gates["structure"]["status"] == "missing":
        return "audit"
    if gates["structure"]["status"] == "unparsed":
        return "parse"
    if gates["structure"]["status"] == "fail":
        return "audit-structure"
    if gates["solution"]["status"] == "fail":
        return "sol_extract"
    if gates["math"]["status"] == "fail":
        return "audit-math"
    if gates["translation"]["status"] == "fail":
        return "translate"
    if gates["product"]["status"] != "pass":
        return "product-smoke"
    return "none"


def assess_book(slug: str, raw_by_slug: dict[str, str] | None = None) -> dict[str, Any]:
    raw_by_slug = raw_by_slug or status.raw_slug_map()
    book_dir = DATA / slug
    unified = book_dir / "unified"
    parsed = book_dir / "parsed"
    content = _read_json(unified / "content_list.json")
    has_unified = isinstance(content, list)
    has_rules = (book_dir / "extract_rules.yaml").is_file()
    has_parsed = (parsed / "book.json").is_file()
    sol_total, sol_done = status.sol_stats(slug)
    has_sol_book = (DATA / f"{slug}_sol" / "unified" / "content_list.json").is_file()
    smoke = _smoke_stats(parsed / "_smoke.md")
    parsed_counts = _parsed_stats(slug)

    source_ok = slug in raw_by_slug and (RAW / raw_by_slug[slug]).is_file()
    if not has_unified:
        structure_status = "missing"
    elif not has_rules:
        structure_status = "missing"
    elif not has_parsed:
        structure_status = "unparsed"
    elif not smoke["exists"] or smoke["critical"]:
        structure_status = "fail"
    else:
        structure_status = "pass"

    if not has_sol_book:
        solution_status = "na"
    elif sol_total and sol_done == sol_total:
        solution_status = "pass"
    else:
        solution_status = "fail"

    translation = _translation(slug, has_parsed)
    math_report = audit_math(slug) if has_parsed else {
        "status": "na",
        "counts": {},
        "warning_counts": {},
        "stats": {"math_fragments": 0, "overlay_math_units": 0},
        "issues": [],
        "warnings": [],
    }
    gates = {
        "source": _gate("pass" if source_ok else "fail", raw=raw_by_slug.get(slug)),
        "ocr": _gate(
            "pass" if has_unified else "fail",
            pages=len({b.get("page_idx") for b in content or [] if isinstance(b, dict) and b.get("page_idx") is not None}),
            blocks=len(content or []),
            chunks=_chunk_count(unified / "chunks.json"),
            images=_count_images(unified / "images"),
        ),
        "structure": _gate(
            structure_status,
            rules=has_rules,
            parsed=has_parsed,
            smoke_critical=smoke["critical"],
            smoke_warning=smoke["warning"],
            **parsed_counts,
        ),
        "solution": _gate(solution_status, sol_book=has_sol_book, solved=sol_done, problems=sol_total),
        "math": _gate(
            math_report["status"],
            fragments=math_report["stats"]["math_fragments"],
            overlay_units=math_report["stats"]["overlay_math_units"],
            issues=len(math_report["issues"]),
            warnings=len(math_report["warnings"]),
            counts=math_report["counts"],
            warning_counts=math_report["warning_counts"],
        ),
        "translation": translation,
        "product": _gate("manual", reason="no automated textbook UI learning-flow smoke yet"),
    }
    level = _level(gates)
    return {"slug": slug, "level": level, "next_action": _next_action(gates), "gates": gates}


def assess_all() -> dict[str, Any]:
    raw_by_slug = status.raw_slug_map()
    rows = [assess_book(slug, raw_by_slug) for slug in status.all_slugs(raw=raw_by_slug)]
    slug_map = _load_slug_map()
    raw_files = {p.name for p in RAW.glob("*.pdf")}
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "books": rows,
        "summary": {
            "book_count": len(rows),
            "raw_pdf_count": len(raw_files),
            "slug_map_count": len(slug_map),
            "raw_missing_from_slug_map": sorted(raw_files - set(slug_map)),
            "slug_map_missing_raw": sorted(set(slug_map) - raw_files),
        },
    }


def _print_table(report: dict[str, Any]) -> None:
    print(f"{'slug':20} {'level':28} {'next':18} {'struct':8} {'sol':7} {'math':7} {'zh':>8}")
    for row in report["books"]:
        gates = row["gates"]
        tr = gates["translation"]
        zh = "—"
        if tr["translatable"]:
            zh = f"{tr['ratio'] * 100:.0f}%"
        print(
            f"{row['slug']:20} {row['level']:<28} {row['next_action']:<18} "
            f"{gates['structure']['status']:<8} {gates['solution']['status']:<7} "
            f"{gates['math']['status']:<7} {zh:>8}"
        )
    summary = report["summary"]
    if summary["raw_missing_from_slug_map"] or summary["slug_map_missing_raw"]:
        print("\n=== source registry drift ===")
        for name in summary["raw_missing_from_slug_map"]:
            print(f"raw not in slug_map: {name}")
        for name in summary["slug_map_missing_raw"]:
            print(f"slug_map missing raw: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m book_pipeline.maturity")
    parser.add_argument("slug", nargs="?")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--out", help="write JSON report to this path atomically")
    args = parser.parse_args(argv)

    if args.slug:
        raw_by_slug = status.raw_slug_map()
        report: dict[str, Any] = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "books": [assess_book(args.slug, raw_by_slug)],
            "summary": {},
        }
    else:
        report = assess_all()

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f"{out.name}.tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(out)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
