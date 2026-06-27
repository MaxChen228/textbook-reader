#!/usr/bin/env python3
"""Audit parsed figure/table/equation catalogs for semantic completeness.

This is intentionally deterministic.  LLM/agent repair may consume the work
queue this produces, but the gate itself must be reproducible and fail closed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textbooks import corpus as reader_corpus

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'book_pipeline' / 'mineru_data'
OVERRIDE_DIR = ROOT / 'book_pipeline' / 'catalog_overrides'

FALLBACK_ID_RE = re.compile(r'^(?:fig|tbl)-(?:ch\d{2,}|app[^-]+)(?:-|$)')
CAT_NUM_PATTERN = r'[A-Z]?\d+[A-Z]?(?:[.\-–—]\d+)+(?:[A-Za-z](?![A-Za-z]))?'
FIG_REF_RE = re.compile(rf'\b(?:Fig\.?|Figure)\s+({CAT_NUM_PATTERN})', re.IGNORECASE)
TBL_REF_RE = re.compile(rf'\b(?:Tab\.?|Table)\s+({CAT_NUM_PATTERN})', re.IGNORECASE)

MAX_REPORT_ITEMS = 80


def _canonical_num(value: str) -> str:
    return re.sub(r'[\-–—]', '.', (value or '').strip())


def _parent_num(value: str) -> str | None:
    value = _canonical_num(value)
    # 折疊單一尾部子圖字母 → 母圖/母表編號（e.g. 15.23B / 33.2c → 15.23 / 33.2）。
    # 大小寫皆折：教科書 panel 標號有用小寫(reif)也有用大寫(醫學/生物參考書 A/B/C/D)。
    m = re.match(r'^(.+\d)[a-zA-Z]$', value)
    return m.group(1) if m else None


def _catalog_covers_ref(ref: str, catalog_nums: set[str]) -> bool:
    ref = _canonical_num(ref)
    if ref in catalog_nums:
        return True
    parent = _parent_num(ref)
    return bool(parent and parent in catalog_nums)


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    entry: dict[str, Any] | None = None
    context: dict[str, str] | None = None


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def _load_ref_classifications(slug: str) -> dict[tuple[str, str], dict[str, Any]]:
    path = OVERRIDE_DIR / f'{slug}.json'
    if not path.is_file():
        return {}
    spec = _load_json(path)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for item in spec.get('ref_classifications') or []:
        if not isinstance(item, dict):
            continue
        typ = str(item.get('type') or '').strip().lower()
        ref = _canonical_num(str(item.get('ref') or ''))
        classification = str(item.get('classification') or '').strip()
        if typ not in {'figure', 'table'} or not ref or not classification:
            continue
        out[(typ, ref)] = item
    return out


def _chunk_file(entry: dict[str, Any]) -> str:
    if entry.get('chunk_kind') == 'ch':
        return f"ch{int(entry['chunk_key']):02d}.json"
    if entry.get('chunk_kind') == 'app':
        return f"app{entry['chunk_key']}.json"
    return f"{entry.get('chunk_key')}.json"


def _entry_label(entry: dict[str, Any]) -> str:
    chunk = _chunk_file(entry).removesuffix('.json')
    bits = [str(entry.get('id') or '<missing-id>'), chunk]
    if entry.get('section'):
        bits.append(f"§{entry['section']}")
    if entry.get('problem'):
        bits.append(f"problem {entry['problem']}")
    return ' / '.join(bits)


def _text_from_block(block: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ('title', 'text', 'md', 'caption', 'tex', 'html'):
        value = block.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ('list_items', 'image_caption', 'table_caption'):
        value = block.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value if v)
    return ' '.join(parts).strip()


def _snippet(text: str, limit: int = 180) -> str:
    text = re.sub(r'\s+', ' ', text or '').strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + '…'


def _walk_lists(data: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    # 重複 problem num 在實際 parsed 資料中常見（parser 把多個習題誤標同號）。
    # 對同 num 的第 occ>0 個 occurrence 加 `#occ` 標記，使 selector 可消歧；
    # occ==0 不加後綴 → 與舊格式（apply 取第一個 match）完全相容。
    groups: list[tuple[str, list[dict[str, Any]]]] = [('body', data.get('body') or [])]
    seen: dict[Any, int] = {}
    for pidx, prob in enumerate(data.get('problems') or []):
        label = prob.get('num') or pidx
        occ = seen.get(label, 0)
        seen[label] = occ + 1
        tag = f'{label}#{occ}' if occ else f'{label}'
        groups.append((f"problem {tag} body", prob.get('body') or []))
        if prob.get('solution'):
            groups.append((f"problem {tag} solution", prob.get('solution') or []))
    return groups


def _visual_contexts(parsed_dir: Path, book: dict[str, Any]) -> dict[tuple[str, str, str, int], dict[str, str]]:
    contexts: dict[tuple[str, str, str, int], dict[str, str]] = {}
    chunks: list[tuple[str, str, Path]] = []
    for ch in book.get('chapters') or []:
        chunks.append(('ch', str(ch['num']), parsed_dir / ch['file']))
    for app in book.get('appendices') or []:
        chunks.append(('app', str(app['id']), parsed_dir / app['file']))

    for kind, key, path in chunks:
        if not path.is_file():
            continue
        data = _load_json(path)
        ordinals = {'figure': 0, 'table': 0}
        for group_name, blocks in _walk_lists(data):
            for idx, block in enumerate(blocks):
                t = block.get('t')
                typ = 'figure' if t == 'fig' else 'table' if t == 'table' else None
                if not typ:
                    continue
                before = ''
                after = ''
                for prev in reversed(blocks[:idx]):
                    before = _text_from_block(prev)
                    if before:
                        break
                for nxt in blocks[idx + 1:]:
                    after = _text_from_block(nxt)
                    if after:
                        break
                ordinal = ordinals[typ]
                ordinals[typ] += 1
                contexts[(kind, key, typ, ordinal)] = {
                    'where': f"{path.name} {group_name} block[{idx}]",
                    'path': _block_path(group_name, idx),
                    'before': _snippet(before),
                    'after': _snippet(after),
                }
    return contexts


def _block_path(group_name: str, idx: int) -> str:
    if group_name == 'body':
        return f'body[{idx}]'
    m = re.match(r'problem\s+(.+)\s+(body|solution)$', group_name)
    if m:
        return f"problem:{m.group(1)}:{m.group(2)}[{idx}]"
    return f'{group_name}[{idx}]'


def _attach_contexts(
    entries: list[dict[str, Any]], contexts: dict[tuple[str, str, str, int], dict[str, str]]
) -> dict[int, dict[str, str]]:
    by_entry: dict[int, dict[str, str]] = {}
    ordinals: dict[tuple[str, str, str], int] = {}
    for entry in entries:
        typ = entry.get('type')
        if typ not in {'figure', 'table'}:
            continue
        key = (entry.get('chunk_kind'), str(entry.get('chunk_key')), typ)
        ordinal = ordinals.get(key, 0)
        ordinals[key] = ordinal + 1
        ctx = contexts.get((key[0], key[1], key[2], ordinal))
        if ctx:
            by_entry[id(entry)] = ctx
    return by_entry


def _collect_text_refs(parsed_dir: Path, book: dict[str, Any]) -> tuple[set[str], set[str]]:
    fig_occ, tbl_occ = _collect_text_ref_occurrences(parsed_dir, book)
    return set(fig_occ), set(tbl_occ)


def _collect_text_ref_occurrences(
    parsed_dir: Path,
    book: dict[str, Any],
) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    fig_refs: dict[str, list[dict[str, str]]] = {}
    tbl_refs: dict[str, list[dict[str, str]]] = {}
    chunk_files = [parsed_dir / ch['file'] for ch in book.get('chapters') or []]
    chunk_files += [parsed_dir / app['file'] for app in book.get('appendices') or []]

    def add(target: dict[str, list[dict[str, str]]], ref: str, context: dict[str, str]) -> None:
        bucket = target.setdefault(ref, [])
        if len(bucket) < 5:
            bucket.append(context)

    def visit(value: Any, strings: list[str]) -> None:
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, list):
            for item in value:
                visit(item, strings)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item, strings)

    for path in chunk_files:
        if path.is_file():
            data = _load_json(path)
            for group_name, blocks in _walk_lists(data):
                for idx, block in enumerate(blocks):
                    strings: list[str] = []
                    visit(block, strings)
                    text = ' '.join(strings)
                    if not text:
                        continue
                    ctx = {
                        'where': f"{path.name} {group_name} block[{idx}]",
                        'path': _block_path(group_name, idx),
                        'snippet': _snippet(text),
                    }
                    for m in FIG_REF_RE.finditer(text):
                        add(fig_refs, _canonical_num(m.group(1)), ctx)
                    for m in TBL_REF_RE.finditer(text):
                        add(tbl_refs, _canonical_num(m.group(1)), ctx)
    return fig_refs, tbl_refs


def _image_exists(root: Path, src: str) -> bool:
    if not src:
        return False
    src_path = Path(src)
    candidates = [
        root / 'unified' / src_path,
        root / 'unified' / 'images' / src_path.name,
        root / 'parsed' / src_path,
        root / 'parsed' / 'images' / src_path.name,
    ]
    if any(p.is_file() for p in candidates):
        return True
    # unified/images 已下沉冷藏（storage_gc archive）→ 讀本地 marker 認得「圖在冷藏、視為存在」，
    # 不誤判圖缺失、不重觸發 pdf_crop（檔名清單在 images.archived.json；要動圖時 restore 拉回）。
    marker = root / 'unified' / 'images.archived.json'
    if marker.is_file():
        try:
            names = set((json.loads(marker.read_text()) or {}).get('files', []))
            if src_path.name in names:
                return True
        except Exception:
            pass
    return False


def _visible_catalog_entry(group: str, entry: dict[str, Any]) -> bool:
    if group in {'figures', 'tables'}:
        return bool(entry.get('id'))
    if group == 'equations':
        return bool(entry.get('label'))
    return False


def _reader_chunk_ids(slug: str, kind: str, key: Any) -> set[str]:
    if kind == 'ch':
        data = reader_corpus.load_chapter(slug, int(key), None)
    elif kind == 'app':
        data = reader_corpus.load_appendix(slug, str(key), None)
    else:
        return set()
    if not data:
        return set()
    ids: set[str] = set()

    def walk(blocks: list[dict[str, Any]] | None) -> None:
        for block in blocks or []:
            if block.get('t') in {'p', 'fig', 'table', 'eq'} and block.get('id'):
                ids.add(str(block['id']))

    walk(data.get('body'))
    for problem in data.get('problems') or []:
        walk(problem.get('body'))
        walk(problem.get('solution'))
    return ids


def audit_catalog(slug: str, write_report: bool = True) -> dict[str, Any]:
    root = DATA_DIR / slug
    parsed_dir = root / 'parsed'
    book_path = parsed_dir / 'book.json'
    catalogs_path = parsed_dir / 'catalogs.json'
    if not book_path.is_file():
        raise FileNotFoundError(f'缺 parsed/book.json：{book_path}')
    if not catalogs_path.is_file():
        raise FileNotFoundError(f'缺 parsed/catalogs.json：{catalogs_path}')

    book = _load_json(book_path)
    catalogs = _load_json(catalogs_path)
    visual_entries = list(catalogs.get('figures') or []) + list(catalogs.get('tables') or [])
    contexts = _visual_contexts(parsed_dir, book)
    entry_contexts = _attach_contexts(visual_entries, contexts)

    findings: list[Finding] = []
    fallback_entries: list[dict[str, Any]] = []
    empty_caption_entries: list[dict[str, Any]] = []
    missing_image_entries: list[dict[str, Any]] = []
    broken_anchor_entries: list[dict[str, Any]] = []
    unresolved_visual_entries: list[dict[str, Any]] = []

    for entry in visual_entries:
        eid = str(entry.get('id') or '')
        typ = entry.get('type')
        ctx = entry_contexts.get(id(entry))
        exclude_reason = str(entry.get('catalog_exclude_reason') or '').strip()
        if FALLBACK_ID_RE.search(eid):
            fallback_entries.append(entry)
            findings.append(Finding(
                'C1',
                'critical',
                f"{typ} 使用 fallback id：{_entry_label(entry)}",
                entry,
                ctx,
            ))
        if not str(entry.get('caption') or '').strip() and not exclude_reason:
            empty_caption_entries.append(entry)
            findings.append(Finding(
                'C2',
                'critical',
                f"{typ} caption 空白：{_entry_label(entry)}",
                entry,
                ctx,
            ))
        if not eid and not exclude_reason:
            unresolved_visual_entries.append(entry)
            findings.append(Finding(
                'C7',
                'critical',
                f"{typ} 有 caption 但缺 semantic id/exclude reason：{_entry_label(entry)}",
                entry,
                ctx,
            ))
        if typ == 'figure' and entry.get('kind') != 'text' and not _image_exists(root, str(entry.get('src') or '')):
            missing_image_entries.append(entry)
            findings.append(Finding(
                'C3',
                'critical',
                f"figure 圖檔不存在：{_entry_label(entry)} src={entry.get('src')!r}",
                entry,
                ctx,
            ))

    catalog_fig_nums = {
        str(e.get('id', '')).removeprefix('fig-').split('--', 1)[0]
        for e in catalogs.get('figures') or []
        if str(e.get('id', '')).startswith('fig-') and not FALLBACK_ID_RE.search(str(e.get('id', '')))
    }
    catalog_fig_nums = {_canonical_num(n) for n in catalog_fig_nums}
    catalog_tbl_nums = {
        str(e.get('id', '')).removeprefix('tbl-').split('--', 1)[0]
        for e in catalogs.get('tables') or []
        if str(e.get('id', '')).startswith('tbl-') and not FALLBACK_ID_RE.search(str(e.get('id', '')))
    }
    catalog_tbl_nums = {_canonical_num(n) for n in catalog_tbl_nums}
    ref_classifications = _load_ref_classifications(slug)
    fig_ref_occurrences, tbl_ref_occurrences = _collect_text_ref_occurrences(parsed_dir, book)
    fig_refs, tbl_refs = set(fig_ref_occurrences), set(tbl_ref_occurrences)
    raw_missing_fig_refs = sorted(ref for ref in fig_refs if not _catalog_covers_ref(ref, catalog_fig_nums))
    raw_missing_tbl_refs = sorted(ref for ref in tbl_refs if not _catalog_covers_ref(ref, catalog_tbl_nums))
    classified_fig_refs = {
        ref: ref_classifications[('figure', ref)]
        for ref in raw_missing_fig_refs
        if ('figure', ref) in ref_classifications
    }
    classified_tbl_refs = {
        ref: ref_classifications[('table', ref)]
        for ref in raw_missing_tbl_refs
        if ('table', ref) in ref_classifications
    }
    missing_fig_refs = [ref for ref in raw_missing_fig_refs if ref not in classified_fig_refs]
    missing_tbl_refs = [ref for ref in raw_missing_tbl_refs if ref not in classified_tbl_refs]
    for ref in missing_fig_refs[:MAX_REPORT_ITEMS]:
        findings.append(Finding(
            'C4',
            'critical',
            f'正文提到 Figure {ref}，但 catalogs 沒有 fig-{ref}',
            context=(fig_ref_occurrences.get(ref) or [None])[0],
        ))
    for ref in missing_tbl_refs[:MAX_REPORT_ITEMS]:
        findings.append(Finding(
            'C5',
            'critical',
            f'正文提到 Table {ref}，但 catalogs 沒有 tbl-{ref}',
            context=(tbl_ref_occurrences.get(ref) or [None])[0],
        ))

    reader_id_cache: dict[tuple[str, str], set[str]] = {}
    for group in ('figures', 'tables', 'equations'):
        for entry in catalogs.get(group) or []:
            if not isinstance(entry, dict) or not _visible_catalog_entry(group, entry):
                continue
            anchor = entry.get('anchor')
            kind = entry.get('chunk_kind')
            key = entry.get('chunk_key')
            if not anchor or not kind or key is None:
                broken_anchor_entries.append(entry)
                findings.append(Finding(
                    'C6',
                    'critical',
                    f"可見 catalog 缺 anchor metadata：{group} {_entry_label(entry)}",
                    entry,
                ))
                continue
            cache_key = (str(kind), str(key))
            if cache_key not in reader_id_cache:
                reader_id_cache[cache_key] = _reader_chunk_ids(slug, str(kind), key)
            if str(anchor) not in reader_id_cache[cache_key]:
                broken_anchor_entries.append(entry)
                findings.append(Finding(
                    'C6',
                    'critical',
                    f"可見 catalog anchor 不存在於 reader chunk：{group} {_entry_label(entry)} anchor={anchor}",
                    entry,
                ))

    critical_count = (
        len(fallback_entries)
        + len(empty_caption_entries)
        + len(missing_image_entries)
        + len(missing_fig_refs)
        + len(missing_tbl_refs)
        + len(broken_anchor_entries)
        + len(unresolved_visual_entries)
    )
    summary = {
        'slug': slug,
        'figures': len(catalogs.get('figures') or []),
        'tables': len(catalogs.get('tables') or []),
        'equations': len(catalogs.get('equations') or []),
        'fallback_ids': len(fallback_entries),
        'empty_captions': len(empty_caption_entries),
        'missing_images': len(missing_image_entries),
        'missing_figure_refs': len(missing_fig_refs),
        'missing_table_refs': len(missing_tbl_refs),
        'classified_figure_refs': len(classified_fig_refs),
        'classified_table_refs': len(classified_tbl_refs),
        'classified_refs': {
            'figures': classified_fig_refs,
            'tables': classified_tbl_refs,
        },
        'broken_anchors': len(broken_anchor_entries),
        'unresolved_visuals': len(unresolved_visual_entries),
        'critical': critical_count,
        'findings': findings,
    }
    if write_report:
        _write_report(parsed_dir / '_catalog_audit.md', summary)
    return summary


def _write_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        f"# Catalog audit — {summary['slug']}",
        '',
        '## Summary',
        f"- figures: {summary['figures']}",
        f"- tables: {summary['tables']}",
        f"- equations: {summary['equations']}",
        f"- fallback figure/table ids: {summary['fallback_ids']}",
        f"- empty figure/table captions: {summary['empty_captions']}",
        f"- missing figure image files: {summary['missing_images']}",
        f"- unresolved Figure refs: {summary['missing_figure_refs']}",
        f"- unresolved Table refs: {summary['missing_table_refs']}",
        f"- classified noninternal/source Figure refs: {summary['classified_figure_refs']}",
        f"- classified noninternal/source Table refs: {summary['classified_table_refs']}",
        f"- broken visible anchors: {summary['broken_anchors']}",
        f"- unresolved visual semantics: {summary['unresolved_visuals']}",
        f"- critical findings: {summary['critical']}",
        '',
    ]
    findings: list[Finding] = summary['findings']
    if not findings:
        lines += ['## OK', 'No catalog findings.', '']
    else:
        lines += ['## Work queue', '']
        for finding in findings[:MAX_REPORT_ITEMS]:
            lines.append(f"- [{finding.code}] {finding.message}")
            if finding.context:
                ctx = finding.context
                if ctx.get('where'):
                    lines.append(f"  - where: {ctx['where']}")
                if ctx.get('path'):
                    lines.append(f"  - path: {ctx['path']}")
                if ctx.get('before'):
                    lines.append(f"  - before: {ctx['before']}")
                if ctx.get('after'):
                    lines.append(f"  - after: {ctx['after']}")
                if ctx.get('snippet'):
                    lines.append(f"  - snippet: {ctx['snippet']}")
        if len(findings) > MAX_REPORT_ITEMS:
            lines.append(f"- ... {len(findings) - MAX_REPORT_ITEMS} more findings omitted")
        lines.append('')
    classified_refs = summary.get('classified_refs') or {}
    classified_items: list[tuple[str, str, dict[str, Any]]] = []
    for typ, refs in (('Figure', classified_refs.get('figures') or {}), ('Table', classified_refs.get('tables') or {})):
        for ref, item in sorted(refs.items()):
            classified_items.append((typ, ref, item))
    if classified_items:
        lines += ['## Classified Noninternal/Source Refs', '']
        for typ, ref, item in classified_items[:MAX_REPORT_ITEMS]:
            classification = item.get('classification') or 'classified'
            reason = item.get('reason') or ''
            lines.append(f"- [{classification}] {typ} {ref}: {reason}")
            if item.get('evidence'):
                lines.append(f"  - evidence: {item['evidence']}")
            if item.get('where'):
                lines.append(f"  - where: {item['where']}")
        if len(classified_items) > MAX_REPORT_ITEMS:
            lines.append(f"- ... {len(classified_items) - MAX_REPORT_ITEMS} more classified refs omitted")
        lines.append('')
    path.write_text('\n'.join(lines), encoding='utf-8')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('slug', nargs='?')
    ap.add_argument('--all', action='store_true')
    args = ap.parse_args()

    slugs = [args.slug] if args.slug else []
    if args.all:
        slugs = sorted(p.parent.parent.name for p in DATA_DIR.glob('*/parsed/catalogs.json'))
    if not slugs:
        ap.print_help()
        return 2

    exit_code = 0
    for slug in slugs:
        try:
            summary = audit_catalog(slug)
        except Exception as exc:
            print(f'[catalog-audit] {slug}: error={exc}', file=sys.stderr)
            exit_code = 1
            continue
        print(
            f"[catalog-audit] {slug}: critical={summary['critical']} "
            f"fallback={summary['fallback_ids']} empty_caption={summary['empty_captions']} "
            f"missing_refs={summary['missing_figure_refs'] + summary['missing_table_refs']} "
            f"classified_refs={summary['classified_figure_refs'] + summary['classified_table_refs']} "
            f"broken_anchors={summary['broken_anchors']} unresolved_visuals={summary['unresolved_visuals']}"
        )
        if summary['critical']:
            exit_code = 1
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
