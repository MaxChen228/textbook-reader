#!/usr/bin/env python3
"""Add source-backed catalog aliases for old parsed books.

This pass does not invent fallback catalog ids.  It only acts on refs already
reported by catalog_audit and links them to an existing visual/table block when
there is deterministic evidence:
  - chapter-qualified ref maps to a chapter-local old id (Fig. 1.7 -> Fig. 7 in ch01)
  - the exact ref is in nearby text and the next visual/table block is the target
  - a subfigure/parent ref can point to an existing sibling/child block
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

from book_pipeline import build_catalogs, catalog_audit

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'book_pipeline' / 'mineru_data'

REF_MSG_RE = re.compile(r'正文提到 (Figure|Table) ([^，]+)，')
FALLBACK_ID_RE = re.compile(r'^(?:fig|tbl)-(?:ch\d{2}|app[^-]+)(?:-|$)')


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def _canonical_num(value: str) -> str:
    return re.sub(r'[\-–—]', '.', (value or '').strip())


def _prefix(kind: str) -> str:
    return 'fig' if kind == 'figure' else 'tbl'


def _expected_id(kind: str, ref: str) -> str:
    return f'{_prefix(kind)}-{_canonical_num(ref)}'


def _target_stem(ref: str) -> str | None:
    m = re.match(r'^[A-Za-z]*(\d+)', ref)
    return f"ch{int(m.group(1)):02d}" if m else None


def _local_ref(ref: str) -> str | None:
    ref = _canonical_num(ref)
    parts = ref.split('.', 1)
    return parts[1] if len(parts) == 2 and parts[1] else None


def _parent_id(cid: str) -> str | None:
    m = re.match(r'^((?:fig|tbl)-.+\d)[A-Za-z]$', cid)
    return m.group(1) if m else None


def _block_text(block: dict[str, Any]) -> str:
    parts = []
    for key in ('md', 'text', 'caption', 'html'):
        value = block.get(key)
        if isinstance(value, str):
            parts.append(value)
    return re.sub(r'\s+', ' ', ' '.join(parts)).strip()


def _ref_re(kind: str, ref: str) -> re.Pattern[str]:
    label = r'(?:Fig\.?|Figure)' if kind == 'figure' else r'(?:Tab\.?|Table)'
    escaped = re.escape(ref).replace(r'\.', r'[\.\-–—]')
    return re.compile(rf'\b{label}\s+{escaped}\b', re.IGNORECASE)


def _contains_ref(block: dict[str, Any], kind: str, ref: str) -> bool:
    return bool(_ref_re(kind, ref).search(_block_text(block)))


def _semantic_id(block: dict[str, Any], kind: str) -> str | None:
    raw = str(block.get('id') or '').strip()
    prefix = _prefix(kind)
    if not raw.startswith(f'{prefix}-') or FALLBACK_ID_RE.search(raw):
        return None
    return f"{prefix}-{_canonical_num(raw.removeprefix(prefix + '-').split('--', 1)[0])}"


def _has_alias(block: dict[str, Any], cid: str) -> bool:
    for alias in block.get('catalog_aliases') or []:
        if isinstance(alias, dict) and build_catalogs._canonical_catalog_id(str(alias.get('id') or '')) == cid:
            return True
    return False


def _add_alias(block: dict[str, Any], kind: str, ref: str, source: str, evidence: str) -> bool:
    cid = _expected_id(kind, ref)
    if _semantic_id(block, kind) == cid or _has_alias(block, cid):
        return _stabilize_alias_anchor(block, kind)
    aliases = block.setdefault('catalog_aliases', [])
    if not isinstance(aliases, list):
        aliases = []
        block['catalog_aliases'] = aliases
    label = 'Figure' if kind == 'figure' else 'Table'
    aliases.append({
        'id': cid,
        'type': kind,
        'caption': f'{label} {_canonical_num(ref)}',
        'source': source,
        'evidence': evidence[:240],
    })
    block['catalog_alias_source'] = source
    _stabilize_alias_anchor(block, kind)
    return True


def _stabilize_alias_anchor(block: dict[str, Any], kind: str) -> bool:
    if _semantic_id(block, kind):
        return False
    changed = False
    if block.get('catalog_exclude_reason') != 'catalog_alias_anchor':
        block['catalog_exclude_reason'] = 'catalog_alias_anchor'
        changed = True
    if block.get('catalog_role') != 'alias_anchor':
        block['catalog_role'] = 'alias_anchor'
        changed = True
    return changed


def _iter_groups(data: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: list[tuple[str, list[dict[str, Any]]]] = [('body', data.get('body') or [])]
    for prob in data.get('problems') or []:
        groups.append((f"problem:{prob.get('num')}:body", prob.get('body') or []))
        if prob.get('solution'):
            groups.append((f"problem:{prob.get('num')}:solution", prob.get('solution') or []))
    return groups


def _all_media(data: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    t = 'fig' if kind == 'figure' else 'table'
    out: list[dict[str, Any]] = []
    for _name, blocks in _iter_groups(data):
        out.extend(block for block in blocks if block.get('t') == t)
    return out


def _find_by_id(data: dict[str, Any], kind: str, cid: str) -> dict[str, Any] | None:
    for block in _all_media(data, kind):
        if _semantic_id(block, kind) == cid:
            return block
    return None


def _candidate_local_numbering(data: dict[str, Any], kind: str, ref: str) -> tuple[dict[str, Any], str] | None:
    local = _local_ref(ref)
    if not local:
        return None
    block = _find_by_id(data, kind, f'{_prefix(kind)}-{local}')
    if block:
        return block, f'chapter-local id {_prefix(kind)}-{local}'
    parent = re.sub(r'[A-Za-z]$', '', local)
    if parent != local:
        block = _find_by_id(data, kind, f'{_prefix(kind)}-{parent}')
        if block:
            return block, f'chapter-local parent id {_prefix(kind)}-{parent}'
    return None


def _candidate_sibling(data: dict[str, Any], kind: str, ref: str) -> tuple[dict[str, Any], str] | None:
    cid = _expected_id(kind, ref)
    parent = _parent_id(cid)
    media = _all_media(data, kind)
    if parent:
        for block in media:
            sid = _semantic_id(block, kind)
            if sid and sid.startswith(parent) and sid != cid:
                return block, f'sibling id {sid}'
    children = [block for block in media if (_semantic_id(block, kind) or '').startswith(cid)]
    if children:
        return children[0], f'child id {_semantic_id(children[0], kind)}'
    return None


def _num_key(ref: str) -> tuple[int, int, int] | None:
    m = re.match(r'^(\d+)\.(\d+)([A-Za-z])?$', _canonical_num(ref))
    if not m:
        return None
    suffix = (ord(m.group(3).lower()) - 96) if m.group(3) else 0
    return int(m.group(1)), int(m.group(2)), suffix


def _linear_media(data: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return _all_media(data, kind)


def _candidate_sequence_gap(data: dict[str, Any], kind: str, ref: str) -> tuple[dict[str, Any], str] | None:
    target = _num_key(ref)
    if not target:
        return None
    media = _linear_media(data, kind)
    indexed: list[tuple[int, tuple[int, int, int], dict[str, Any]]] = []
    for i, block in enumerate(media):
        sid = _semantic_id(block, kind)
        if not sid:
            continue
        num = sid.split('-', 1)[1]
        key = _num_key(num)
        if key and key[0] == target[0]:
            indexed.append((i, key, block))
    before = [row for row in indexed if row[1] < target]
    after = [row for row in indexed if row[1] > target]
    if not before or not after:
        return None
    lo = max(before, key=lambda row: row[1])
    hi = min(after, key=lambda row: row[1])
    candidates = [
        block for i, block in enumerate(media)
        if lo[0] < i < hi[0] and not _semantic_id(block, kind)
    ]
    if len(candidates) != 1:
        return None
    return candidates[0], f'sequence gap between {_semantic_id(lo[2], kind)} and {_semantic_id(hi[2], kind)}'


def _candidate_near_ref_text(data: dict[str, Any], kind: str, ref: str) -> tuple[dict[str, Any], str] | None:
    t = 'fig' if kind == 'figure' else 'table'
    for group_name, blocks in _iter_groups(data):
        for i, block in enumerate(blocks):
            if not _contains_ref(block, kind, ref):
                continue
            for direction in (1, -1):
                for dist in range(1, 9):
                    j = i + direction * dist
                    if j < 0 or j >= len(blocks):
                        break
                    probe = blocks[j]
                    if probe.get('t') in {'section', 'subsection'}:
                        break
                    if probe.get('t') == t:
                        return probe, f'{group_name}[{i}] {"next" if direction > 0 else "prev"} visual at distance {dist}'
    return None


def _missing_refs(slug: str) -> list[tuple[str, str]]:
    summary = catalog_audit.audit_catalog(slug, write_report=False)
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for finding in summary.get('findings') or []:
        if finding.code not in {'C4', 'C5'}:
            continue
        m = REF_MSG_RE.search(finding.message)
        if not m:
            continue
        kind = 'figure' if m.group(1) == 'Figure' else 'table'
        ref = _canonical_num(m.group(2))
        key = (kind, ref)
        if key not in seen:
            refs.append(key)
            seen.add(key)
    return refs


def _repair_slug(slug: str, dry_run: bool = False) -> dict[str, int]:
    parsed_dir = DATA_DIR / slug / 'parsed'
    book_path = parsed_dir / 'book.json'
    if not book_path.is_file():
        return {'errors': 1}
    refs = _missing_refs(slug)
    touched: dict[Path, dict[str, Any]] = {}
    counts = Counter()

    for path in parsed_dir.glob('ch*.json'):
        data = _load_json(path)
        stabilized = 0
        for kind in ('figure', 'table'):
            for block in _all_media(data, kind):
                if block.get('catalog_aliases') and _stabilize_alias_anchor(block, kind):
                    stabilized += 1
        if stabilized:
            touched[path] = data
            counts['stabilized'] += stabilized

    for kind, ref in refs:
        stem = _target_stem(ref)
        if not stem:
            counts['unmatched'] += 1
            continue
        path = parsed_dir / f'{stem}.json'
        if not path.is_file():
            counts['unmatched'] += 1
            continue
        data = touched.get(path)
        if data is None:
            data = _load_json(path)

        cid = _expected_id(kind, ref)
        if _find_by_id(data, kind, cid):
            continue

        candidate: tuple[dict[str, Any], str] | None = (
            _candidate_local_numbering(data, kind, ref)
            or _candidate_near_ref_text(data, kind, ref)
            or _candidate_sequence_gap(data, kind, ref)
            or _candidate_sibling(data, kind, ref)
        )
        if not candidate:
            counts['unmatched'] += 1
            continue
        block, evidence = candidate
        source = 'agent-local-numbering-alias'
        if 'distance' in evidence:
            source = 'agent-near-ref-alias'
        elif 'sequence gap' in evidence:
            source = 'agent-sequence-gap-alias'
        elif 'sibling' in evidence or 'child' in evidence:
            source = 'agent-subfigure-alias'
        if _add_alias(block, kind, ref, source, evidence):
            touched[path] = data
            counts[source] += 1
            counts['aliases'] += 1

    if not touched or dry_run:
        counts['touched_files'] = len(touched)
        counts['errors'] = 0
        return dict(counts)

    backup_dir = parsed_dir / '_manual_repair_backups' / datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_dir.mkdir(parents=True, exist_ok=True)
    for path in [book_path, parsed_dir / 'catalogs.json', *touched.keys()]:
        if path.is_file():
            shutil.copy2(path, backup_dir / path.name)
    for path, data in touched.items():
        _write_json(path, data)
    build_catalogs.build_catalogs(slug)
    counts['touched_files'] = len(touched)
    counts['errors'] = 0
    return dict(counts)


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
            print(f'[alias-repair] {slug}: error={exc}')
            total['errors'] += 1
            continue
        if report.get('errors'):
            total['errors'] += int(report.get('errors', 1))
            continue
        total.update(report)
        total['books'] += 1
        print(
            f"[alias-repair] {slug}: touched={report.get('touched_files', 0)} "
            f"aliases={report.get('aliases', 0)} unmatched={report.get('unmatched', 0)} "
            f"local={report.get('agent-local-numbering-alias', 0)} "
            f"near={report.get('agent-near-ref-alias', 0)} "
            f"seq={report.get('agent-sequence-gap-alias', 0)} "
            f"subfig={report.get('agent-subfigure-alias', 0)}"
        )
    print(
        f"[alias-repair] done: books={total['books']} touched={total['touched_files']} "
        f"aliases={total['aliases']} unmatched={total['unmatched']} errors={total['errors']}"
    )
    return 1 if total['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
