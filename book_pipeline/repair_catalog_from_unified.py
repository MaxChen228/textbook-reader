#!/usr/bin/env python3
"""Recover missing formal catalog blocks from unified MinerU content.

This is a deterministic old-data repair pass.  It only acts on refs already
reported by catalog_audit and only when unified/content_list.json contains a
formal Figure/Table caption for that exact ref.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from book_pipeline import build_catalogs, catalog_audit, parser

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'book_pipeline' / 'mineru_data'

REF_MSG_RE = re.compile(r'正文提到 (Figure|Table) ([^，]+)，')
FIG_TEXT_RE = re.compile(r'\b(?:Fig\.?|Figure)\s+%s\b', re.IGNORECASE)
TBL_TEXT_RE = re.compile(r'\b(?:Tab\.?|Table)\s+%s\b', re.IGNORECASE)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def _canonical_num(value: str) -> str:
    return re.sub(r'[\-–—]', '.', (value or '').strip())


def _caption_text(item: dict[str, Any]) -> str:
    typ = item.get('type')
    if typ == 'chart':
        captions = item.get('chart_caption') or []
    elif typ == 'image':
        captions = item.get('image_caption') or []
    elif typ == 'table':
        captions = item.get('table_caption') or []
    elif typ == 'code':
        captions = item.get('code_caption') or []
    else:
        captions = []
    return ' '.join(str(c).strip() for c in captions if c).strip()


def _caption_ref(item: dict[str, Any]) -> tuple[str, str] | None:
    caption = _caption_text(item)
    if not caption:
        return None
    fid = parser.fig_id_from_caption(caption)
    if fid:
        return 'figure', _canonical_num(fid.removeprefix('fig-'))
    tid = parser.table_id_from_caption(caption)
    if tid:
        return 'table', _canonical_num(tid.removeprefix('tbl-'))
    return None


def _text_contains_ref(item: dict[str, Any], kind: str, ref: str) -> bool:
    text = ' '.join(str(v) for v in item.values() if isinstance(v, str))
    if not text:
        return False
    escaped = re.escape(ref).replace(r'\.', r'[\.\-–—]')
    regex = FIG_TEXT_RE if kind == 'figure' else TBL_TEXT_RE
    return bool(re.search(regex.pattern % escaped, text, regex.flags))


def _target_stem(ref: str) -> str | None:
    m = re.match(r'^[A-Za-z]*(\d+)', ref)
    if not m:
        return None
    return f"ch{int(m.group(1)):02d}"


def _parent_ref(ref: str) -> str | None:
    ref = _canonical_num(ref)
    m = re.match(r'^(.+\d)[A-Za-z]$', ref)
    return m.group(1) if m else None


def _missing_refs(slug: str) -> list[tuple[str, str, dict[str, str]]]:
    summary = catalog_audit.audit_catalog(slug, write_report=False)
    refs: list[tuple[str, str, dict[str, str]]] = []
    for finding in summary.get('findings') or []:
        if finding.code not in {'C4', 'C5'}:
            continue
        m = REF_MSG_RE.search(finding.message)
        if not m:
            continue
        kind = 'figure' if m.group(1) == 'Figure' else 'table'
        refs.append((kind, _canonical_num(m.group(2)), finding.context or {}))
    return refs


def _compiled_label_re(slug: str) -> re.Pattern[str]:
    rules = parser.load_rules(slug)
    regexes = parser.compile_regexes(rules)
    return regexes['eq_label']


def _convert_item(slug: str, item: dict[str, Any]) -> dict[str, Any] | None:
    rules = parser.load_rules(slug)
    return parser.block_to_struct(
        item,
        _compiled_label_re(slug),
        bool(rules.get('ignore_image_content', False)),
        bool(rules.get('ignore_chart_content', False)),
    )


def _walk_block_lists(data: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: list[tuple[str, list[dict[str, Any]]]] = [('body', data.get('body') or [])]
    for prob in data.get('problems') or []:
        groups.append((f"problem:{prob.get('num')}:body", prob.get('body') or []))
        if prob.get('solution'):
            groups.append((f"problem:{prob.get('num')}:solution", prob.get('solution') or []))
    return groups


def _find_same_src(data: dict[str, Any], src: str) -> dict[str, Any] | None:
    if not src:
        return None
    for _name, blocks in _walk_block_lists(data):
        for block in blocks:
            if block.get('src') == src:
                return block
    return None


def _context_target(data: dict[str, Any], context_path: str) -> tuple[list[dict[str, Any]], int] | None:
    if not context_path:
        return None
    m = re.match(r'^body\[(\d+)\]$', context_path)
    if m:
        return data.setdefault('body', []), int(m.group(1))
    m = re.match(r'^problem:(.+):(body|solution)\[(\d+)\]$', context_path)
    if not m:
        return None
    pnum, target, idx = m.groups()
    for prob in data.get('problems') or []:
        if str(prob.get('num')) == pnum:
            return prob.setdefault(target, []), int(idx)
    return None


def _candidate_index(blocks: list[dict[str, Any]], kind: str, ref: str) -> int | None:
    for i, item in enumerate(blocks):
        cap = _caption_ref(item)
        if cap == (kind, ref):
            return i
    return None


def _first_ref_index(blocks: list[dict[str, Any]], kind: str, ref: str) -> int | None:
    for i, item in enumerate(blocks):
        if _text_contains_ref(item, kind, ref):
            return i
    return None


def _repair_slug(slug: str, dry_run: bool = False) -> dict[str, int | str]:
    root = DATA_DIR / slug
    parsed_dir = root / 'parsed'
    unified_path = root / 'unified' / 'content_list.json'
    book_path = parsed_dir / 'book.json'
    if not (unified_path.is_file() and book_path.is_file()):
        return {'slug': slug, 'errors': 1}

    missing = _missing_refs(slug)
    if not missing:
        return {'slug': slug, 'errors': 0, 'touched_files': 0, 'updated': 0, 'inserted': 0, 'unmatched': 0}

    unified = _load_json(unified_path)
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for item in unified:
        cap = _caption_ref(item)
        if cap and cap not in candidates:
            candidates[cap] = item

    touched: dict[Path, dict[str, Any]] = {}
    updated = 0
    inserted = 0
    unmatched = 0

    for kind, ref, context in missing:
        item = candidates.get((kind, ref))
        if item is None:
            parent = _parent_ref(ref)
            if parent:
                item = candidates.get((kind, parent))
        if not item:
            unmatched += 1
            continue
        stem = _target_stem(ref)
        if not stem:
            unmatched += 1
            continue
        path = parsed_dir / f'{stem}.json'
        if not path.is_file():
            unmatched += 1
            continue
        data = touched.get(path)
        if data is None:
            data = _load_json(path)
            touched[path] = data
        struct = _convert_item(slug, item)
        if not struct:
            unmatched += 1
            continue
        struct['catalog_repair_source'] = 'agent-unified-recovery'

        existing = _find_same_src(data, str(struct.get('src') or ''))
        if existing is not None:
            existing.update(struct)
            existing.pop('catalog_exclude_reason', None)
            existing.pop('catalog_role', None)
            updated += 1
            continue

        target = _context_target(data, context.get('path', ''))
        if not target:
            unmatched += 1
            continue
        blocks, idx = target
        cand_idx = _candidate_index(unified, kind, ref)
        ref_idx = _first_ref_index(unified, kind, ref)
        insert_at = idx
        if cand_idx is None or ref_idx is None or cand_idx >= ref_idx:
            insert_at = idx + 1
        insert_at = max(0, min(insert_at, len(blocks)))
        blocks.insert(insert_at, struct)
        inserted += 1

    if not touched or dry_run:
        return {
            'slug': slug,
            'errors': 0,
            'touched_files': len(touched),
            'updated': updated,
            'inserted': inserted,
            'unmatched': unmatched,
            'dry_run': int(dry_run),
        }

    backup_dir = parsed_dir / '_manual_repair_backups' / datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_dir.mkdir(parents=True, exist_ok=True)
    for path in [book_path, parsed_dir / 'catalogs.json', *touched.keys()]:
        if path.is_file():
            shutil.copy2(path, backup_dir / path.name)
    for path, data in touched.items():
        _write_json(path, data)
    build_catalogs.build_catalogs(slug)
    return {
        'slug': slug,
        'errors': 0,
        'touched_files': len(touched),
        'updated': updated,
        'inserted': inserted,
        'unmatched': unmatched,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('slug', nargs='?')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    slugs = [args.slug] if args.slug else []
    if args.all or not args.slug:
        slugs = sorted(p.parent.parent.name for p in DATA_DIR.glob('*/parsed/catalogs.json'))

    total = Counter()
    for slug in slugs:
        try:
            report = _repair_slug(slug, dry_run=args.dry_run)
        except Exception as exc:
            print(f'[unified-repair] {slug}: error={exc}')
            total['errors'] += 1
            continue
        if report.get('errors'):
            total['errors'] += 1
            continue
        for key in ('touched_files', 'updated', 'inserted', 'unmatched'):
            total[key] += int(report.get(key, 0))
        total['books'] += 1
        print(
            f"[unified-repair] {slug}: touched={report.get('touched_files', 0)} "
            f"updated={report.get('updated', 0)} inserted={report.get('inserted', 0)} "
            f"unmatched={report.get('unmatched', 0)}"
        )

    print(
        f"[unified-repair] done: books={total['books']} touched={total['touched_files']} "
        f"updated={total['updated']} inserted={total['inserted']} "
        f"unmatched={total['unmatched']} errors={total['errors']}"
    )
    return 1 if total['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
