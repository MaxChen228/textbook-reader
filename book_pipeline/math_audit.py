#!/usr/bin/env python3
"""Math integrity audit for parsed textbook data.

This is a deterministic data-layer gate. It catches math that is already
suspect before browser rendering: delimiter damage, left/right imbalance,
raw TeX leaks, high-risk OCR patterns, and translation overlay math drift.
Browser-only MathJax failures belong in a later product/browser gate.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from book_pipeline.translate import overlay_anchor

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "book_pipeline" / "mineru_data"

TEXT_FIELDS = ("md", "title", "caption", "footnote", "tex")
RAW_TEX_RE = re.compile(r"\\[A-Za-z]+")
HIGH_RISK_RE = re.compile(r"\\(?:left|right)\s*(?:$|[,.，。；;])|(?:\d\s+){3,}\d")
HARD_KINDS = {"delimiter", "left_right", "raw_tex_leak", "overlay_math_drift"}


@dataclass(frozen=True)
class TextUnit:
    slug: str
    stem: str
    source: str
    field: str
    text: str
    locator: str
    problem: str | None = None
    section: str | None = None
    index: int | None = None
    anchor: str | None = None


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _book_stems(slug: str) -> list[str]:
    book = _read_json(DATA / slug / "parsed" / "book.json")
    if not isinstance(book, dict):
        return []
    stems = [Path(ch["file"]).stem for ch in book.get("chapters", [])]
    stems.extend(Path(ap["file"]).stem for ap in book.get("appendices", []))
    return stems


def _field_units(block: dict, base: dict[str, Any]) -> Iterable[TextUnit]:
    for field in TEXT_FIELDS:
        value = block.get(field)
        if isinstance(value, str) and value.strip():
            yield TextUnit(field=field, text=value, anchor=block.get("a"), **base)


def iter_units(slug: str, overlay: bool = False) -> Iterable[TextUnit]:
    parsed = DATA / slug / "parsed"
    suffix = ".zh" if overlay else ""
    source = "overlay" if overlay else "source"
    for stem in _book_stems(slug):
        data = _read_json(parsed / f"{stem}{suffix}.json")
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("title"), str) and data["title"].strip():
            yield TextUnit(slug, stem, source, "title", data["title"], f"{stem}:title")
        for idx, block in enumerate(data.get("body") or []):
            if isinstance(block, dict):
                base = {
                    "slug": slug,
                    "stem": stem,
                    "source": source,
                    "locator": f"{stem}:body[{idx}]",
                    "index": idx,
                    "section": "body",
                }
                yield from _field_units(block, base)
        for pr in data.get("problems") or []:
            if not isinstance(pr, dict):
                continue
            prob = str(pr.get("num"))
            for sec in ("body", "solution"):
                for idx, block in enumerate(pr.get(sec) or []):
                    if not isinstance(block, dict):
                        continue
                    base = {
                        "slug": slug,
                        "stem": stem,
                        "source": source,
                        "locator": f"{stem}:problem[{prob}].{sec}[{idx}]",
                        "problem": prob,
                        "section": sec,
                        "index": idx,
                    }
                    yield from _field_units(block, base)


def _math_regions(text: str, field: str) -> list[tuple[int, int, str]]:
    if field == "tex":
        return [(0, len(text), text.strip())] if text.strip() else []
    spans: list[tuple[int, int, str]] = []
    i = 0
    while i < len(text):
        if text.startswith("$$", i):
            j = text.find("$$", i + 2)
            if j == -1:
                spans.append((i, len(text), text[i:]))
                break
            spans.append((i, j + 2, text[i + 2:j].strip()))
            i = j + 2
        elif text[i] == "$":
            j = text.find("$", i + 1)
            if j == -1 or "\n" in text[i + 1:j]:
                i += 1
                continue
            spans.append((i, j + 1, text[i + 1:j].strip()))
            i = j + 1
        elif text.startswith(r"\(", i):
            j = text.find(r"\)", i + 2)
            if j == -1:
                spans.append((i, len(text), text[i:]))
                break
            spans.append((i, j + 2, text[i + 2:j].strip()))
            i = j + 2
        elif text.startswith(r"\[", i):
            j = text.find(r"\]", i + 2)
            if j == -1:
                spans.append((i, len(text), text[i:]))
                break
            spans.append((i, j + 2, text[i + 2:j].strip()))
            i = j + 2
        else:
            i += 1
    return [s for s in spans if s[2]]


def _math_spans(text: str, field: str) -> list[str]:
    return [span for _, _, span in _math_regions(text, field)]


def _delimiter_issue(text: str) -> str | None:
    counts = {
        "$$": text.count("$$"),
        r"\(": text.count(r"\("),
        r"\)": text.count(r"\)"),
        r"\[": text.count(r"\["),
        r"\]": text.count(r"\]"),
    }
    single_dollars = len(re.findall(r"(?<!\$)\$(?!\$)", text))
    if counts["$$"] % 2:
        return "unbalanced display dollar delimiter"
    if single_dollars % 2:
        return "unbalanced inline dollar delimiter"
    if counts[r"\("] != counts[r"\)"]:
        return "unbalanced \\( ... \\) delimiter"
    if counts[r"\["] != counts[r"\]"]:
        return "unbalanced \\[ ... \\] delimiter"
    return None


def _left_right_issue(expr: str) -> str | None:
    left = len(re.findall(r"\\left\b", expr))
    right = len(re.findall(r"\\right\b", expr))
    if left != right:
        return f"left/right mismatch: left={left} right={right}"
    return None


def _raw_tex_leak(text: str) -> bool:
    regions = _math_regions(text, "md")
    if not regions:
        return bool(RAW_TEX_RE.search(text))
    pieces: list[str] = []
    cursor = 0
    for start, end, _ in regions:
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return bool(RAW_TEX_RE.search("".join(pieces)))


def _issue(unit: TextUnit, kind: str, detail: str, excerpt: str | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "slug": unit.slug,
        "stem": unit.stem,
        "source": unit.source,
        "locator": unit.locator,
        "field": unit.field,
        "problem": unit.problem,
        "detail": detail,
        "excerpt": (excerpt or unit.text).replace("\n", " ")[:240],
    }


def _canonical_spans(spans: list[str]) -> Counter[str]:
    stripped = []
    for span in spans:
        compact = re.sub(r"\s+", "", span)
        compact = compact.strip(".,;:，。；：、)]）}([{（")
        if compact:
            stripped.append(compact)
    return Counter(stripped)


def _contains_source_math(source_spans: list[str], overlay_spans: list[str]) -> bool:
    src = _canonical_spans(source_spans)
    ov = _canonical_spans(overlay_spans)
    return all(ov[k] >= n for k, n in src.items())


def _has_extra_math(source_spans: list[str], overlay_spans: list[str]) -> bool:
    src = _canonical_spans(source_spans)
    ov = _canonical_spans(overlay_spans)
    return any(ov[k] > src.get(k, 0) for k in ov)


def _source_math_index(slug: str) -> dict[tuple[str, str, str, int | None, str], list[str]]:
    out: dict[tuple[str, str, str, int | None, str], list[str]] = {}
    for unit in iter_units(slug, overlay=False):
        spans = _math_spans(unit.text, unit.field)
        if spans:
            out[(unit.stem, unit.section or "", unit.problem or "", unit.index, unit.field)] = spans
    return out


def _source_text_index(slug: str) -> dict[tuple[str, str, str, int | None, str], str]:
    out: dict[tuple[str, str, str, int | None, str], str] = {}
    for unit in iter_units(slug, overlay=False):
        out[(unit.stem, unit.section or "", unit.problem or "", unit.index, unit.field)] = unit.text
    return out


def audit_book(slug: str) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    source_math = _source_math_index(slug)
    source_text = _source_text_index(slug)
    stats = {
        "source_units": 0,
        "overlay_units": 0,
        "math_fragments": 0,
        "overlay_math_units": 0,
    }

    for overlay in (False, True):
        for unit in iter_units(slug, overlay=overlay):
            stats["overlay_units" if overlay else "source_units"] += 1
            spans = _math_spans(unit.text, unit.field)
            stats["math_fragments"] += len(spans)
            if overlay and spans:
                stats["overlay_math_units"] += 1

            delimiter = _delimiter_issue(unit.text)
            if delimiter:
                issues.append(_issue(unit, "delimiter", delimiter))
            if unit.field != "tex" and _raw_tex_leak(unit.text):
                issues.append(_issue(unit, "raw_tex_leak", "TeX command appears outside a recognized math span"))
            for span in spans:
                lr = _left_right_issue(span)
                if lr:
                    issues.append(_issue(unit, "left_right", lr, span))
                if HIGH_RISK_RE.search(span):
                    warnings.append(_issue(unit, "high_risk_pattern", "high-risk OCR or delimiter pattern", span))

            if overlay:
                key = (unit.stem, unit.section or "", unit.problem or "", unit.index, unit.field)
                src_spans = source_math.get(key, [])
                if not spans and not src_spans:
                    continue
                src_text = source_text.get(key)
                if unit.anchor and src_text is not None and overlay_anchor({unit.field: src_text}) != unit.anchor:
                    issues.append(_issue(unit, "overlay_anchor_mismatch", "overlay anchor does not match current source block"))
                    continue
                if spans and src_spans and not _contains_source_math(src_spans, spans):
                    issues.append(_issue(unit, "overlay_math_drift", "overlay math is missing or changed source math spans"))
                elif spans and src_spans and _has_extra_math(src_spans, spans):
                    warnings.append(_issue(unit, "overlay_math_extra", "overlay adds math spans not present in source"))

    by_kind: dict[str, int] = {}
    for issue in issues:
        by_kind[issue["kind"]] = by_kind.get(issue["kind"], 0) + 1
    warning_counts: dict[str, int] = {}
    for warning in warnings:
        warning_counts[warning["kind"]] = warning_counts.get(warning["kind"], 0) + 1
    hard_count = sum(count for kind, count in by_kind.items() if kind in HARD_KINDS)
    return {
        "slug": slug,
        "status": "pass" if hard_count == 0 else "fail",
        "stats": stats,
        "counts": by_kind,
        "warning_counts": warning_counts,
        "issues": issues,
        "warnings": warnings,
    }


def _print_report(report: dict[str, Any]) -> None:
    print(f"{report['slug']}: {report['status']}")
    counts = report["counts"]
    if counts:
        print("issues:", " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    else:
        print("issues: none")
    if report["warning_counts"]:
        print("warnings:", " ".join(f"{k}={v}" for k, v in sorted(report["warning_counts"].items())))
    for issue in report["issues"][:20]:
        print(f"- {issue['kind']} {issue['locator']} {issue['field']}: {issue['detail']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m book_pipeline.math_audit")
    parser.add_argument("slug")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = audit_book(args.slug)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
