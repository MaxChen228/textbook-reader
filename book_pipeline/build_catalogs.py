#!/usr/bin/env python3
"""book_pipeline/build_catalogs.py — 從 parsed/ 產出圖/表/式子目錄。

純本地、零 LLM、冪等。不修改 ch*.json / app*.json，只：
  - 寫 parsed/catalogs.json
  - 更新 parsed/book.json 的 catalog_counts 與章節計數

設計原則（簡單、一致、不過擬合）：
  - anchor 是 reader DOM 跳轉用，可沿用 parser 技術 id。
  - id 是使用者可見的語義索引鍵；不可使用 chapter/source/idx fallback。
  - caption 抽不出圖表號、equation 沒 label 時，id 保持 None，交給 audit/repair queue。

可獨立對已 parsed 書回填，也可被 parser.py 在 parse 結尾自動呼叫。

用法：
  uv run python -m book_pipeline.build_catalogs <slug>
  uv run python -m book_pipeline.build_catalogs --all
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'book_pipeline' / 'mineru_data'
SLUG_RE = re.compile(r'^[a-z0-9_]{1,64}$')
CHUNK_RE = re.compile(r'^(?:ch\d{2}|app[A-Za-z0-9_]{1,16})$')


def _valid_slug(slug: str) -> bool:
    return isinstance(slug, str) and SLUG_RE.fullmatch(slug) is not None


def _valid_chunk_stem(stem: str) -> bool:
    return isinstance(stem, str) and CHUNK_RE.fullmatch(stem) is not None

# 從 caption 任意位置抽正式圖表編號（FIGURE 1.1、Table 2.3、Fig. 3-2 等）。
# 裸章號（Figure 3 / Table 4）不是可索引圖表編號，會造成假 catalog。
CAT_NUM_PATTERN = r'[A-Z]?\d+[A-Z]?(?:[.\-–—]\d+)+(?:[A-Za-z](?![A-Za-z]))?'
FIG_NUM_RE = re.compile(rf'Fig(?:ure|\.)?\s*({CAT_NUM_PATTERN})', re.IGNORECASE)
TBL_NUM_RE = re.compile(rf'(?:Table|Tab\.)\s*({CAT_NUM_PATTERN})', re.IGNORECASE)
FIG_CAPTION_RE = re.compile(rf'^\s*(?:Fig(?:ure|\.)?)\s*({CAT_NUM_PATTERN})\s*[.:：-]?\s*(.*)', re.IGNORECASE | re.DOTALL)
TBL_CAPTION_RE = re.compile(rf'^\s*(?:Table|Tab\.?)\s*({CAT_NUM_PATTERN})\s*[.:：-]?\s*(.*)', re.IGNORECASE | re.DOTALL)
FALLBACK_ID_RE = re.compile(r'^(?:fig|tbl|eq)-(?:ch\d{2}|app[^-]+)(?:-|$)')
EQ_TAG_RE = re.compile(r'\\tag\s*\{([^}]+)\}')


def _catalog_aliases(block: dict, anchor: str, default_type: str) -> list[dict]:
    out: list[dict] = []
    raw = block.get('catalog_aliases') or []
    if not isinstance(raw, list):
        return out
    for alias in raw:
        if not isinstance(alias, dict):
            continue
        alias_id = str(alias.get('id') or '').strip()
        if not alias_id:
            continue
        alias_id = _canonical_catalog_id(alias_id)
        alias_type = str(alias.get('type') or default_type).strip() or default_type
        if alias_type not in {'figure', 'table'}:
            continue
        if not ((alias_type == 'figure' and alias_id.startswith('fig-')) or (alias_type == 'table' and alias_id.startswith('tbl-'))):
            continue
        out.append({
            'id': alias_id,
            'type': alias_type,
            'section': None,
            'problem': None,
            'source': None,
            'caption': str(alias.get('caption') or alias_id),
            'src': block.get('src', ''),
            'kind': block.get('kind', 'line') if alias_type == 'figure' else ('text' if default_type == 'figure' else None),
            'aspect': block.get('aspect'),
            'catalog_exclude_reason': None,
            'anchor': anchor,
            'catalog_alias': True,
            'catalog_alias_source': alias.get('source') or block.get('catalog_alias_source'),
            'catalog_alias_evidence': alias.get('evidence'),
        })
    return out


def _extract_num(caption: str, regex: re.Pattern) -> str | None:
    m = regex.search((caption or '').strip())
    return re.sub(r'[\-–—]', '.', m.group(1)) if m else None


def _canonical_catalog_id(raw_id: str) -> str:
    if raw_id.startswith(('fig-', 'tbl-')):
        prefix, rest = raw_id.split('-', 1)
        if FALLBACK_ID_RE.search(raw_id):
            return raw_id
        return f"{prefix}-{re.sub(r'[\-–—]', '.', rest)}"
    return raw_id


def _caption_labels(caption: str) -> list[tuple[str, str, str, str]]:
    """Extract every formal Figure/Table label from one caption.

    Returns (type, id_prefix, canonical_num, display_caption).  A single
    extracted image can legitimately contain multiple labeled figures; the
    catalog should expose each label while all aliases still point to the same
    reader anchor.
    """
    text = caption or ''
    matches: list[tuple[int, int, str, str, str, str]] = []
    for typ, prefix, label, regex in (
        ('figure', 'fig', 'Figure', FIG_NUM_RE),
        ('table', 'tbl', 'Table', TBL_NUM_RE),
    ):
        for m in regex.finditer(text):
            num = re.sub(r'[\-–—]', '.', m.group(1))
            matches.append((m.start(), m.end(), typ, prefix, label, num))
    matches.sort(key=lambda item: item[0])
    if not matches:
        return []
    leading_offset = len(text) - len(text.lstrip())
    if matches[0][0] > leading_offset:
        return []

    labels: list[tuple[str, str, str, str]] = []
    for i, (_start, end, typ, prefix, label, num) in enumerate(matches):
        next_start = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        tail = _plain_text(text[end:next_start].strip(' .:：-–—\n\t'))
        display = f'{label} {num}: {tail}' if tail else f'{label} {num}'
        labels.append((typ, prefix, num, display))
    return labels


def _eq_label(block: dict) -> str | None:
    raw = (block.get('label') or '').strip()
    if raw:
        return raw
    m = EQ_TAG_RE.search(block.get('tex') or '')
    return m.group(1).strip() if m else None


def _plain_text(value: str) -> str:
    value = re.sub(r'<[^>]+>', ' ', value or '')
    return re.sub(r'\s+', ' ', value).strip()


def _leading_caption(block: dict) -> tuple[str, str, str] | None:
    text = block.get('md') or block.get('text') or ''
    if not isinstance(text, str):
        return None
    for kind, regex, prefix, label in (
        ('figure', FIG_CAPTION_RE, 'fig', 'Figure'),
        ('table', TBL_CAPTION_RE, 'tbl', 'Table'),
    ):
        m = regex.match(text)
        if not m:
            continue
        num = _extract_num(f'{label} {m.group(1)}', FIG_NUM_RE if kind == 'figure' else TBL_NUM_RE)
        if not num:
            continue
        caption = _plain_text(m.group(2)) or f'{label} {num}'
        return kind, f'{prefix}-{num}', f'{label} {num}: {caption}'
    return None


def _visual_semantic(t: str, block: dict) -> tuple[str, str, str | None]:
    labels = _caption_labels(block.get('caption', ''))
    if labels:
        typ, prefix, num, _caption = labels[0]
        return typ, prefix, num
    raw_id = (block.get('id') or '').strip()
    if raw_id and not FALLBACK_ID_RE.search(raw_id):
        if raw_id.startswith('fig-'):
            return 'figure', 'fig', _canonical_catalog_id(raw_id).removeprefix('fig-')
        if raw_id.startswith('tbl-'):
            return 'table', 'tbl', _canonical_catalog_id(raw_id).removeprefix('tbl-')
    if t == 'fig':
        return 'figure', 'fig', None
    return 'table', 'tbl', None


def _fallback_anchor(prefix: str, ch_label: str, source: str, idx: int) -> str:
    return f'{prefix}-{ch_label}-{source}-{idx}' if source != 'body' else f'{prefix}-{ch_label}-{idx}'


def _anchor_id(t: str, block: dict, ch_label: str, source: str, idx: int) -> str:
    """回傳 reader DOM anchor；必須與 textbooks.corpus._ensure_catalog_ids 同規則。"""
    raw_id = (block.get('id') or '').strip()
    if raw_id:
        return raw_id

    if t in {'fig', 'table'}:
        _typ, prefix, num = _visual_semantic(t, block)
        if num:
            return f'{prefix}-{num}'
    else:
        prefix = {'eq': 'eq'}.get(t, t)
    if t == 'eq':
        label = _eq_label(block)
        if label:
            return f'eq-{ch_label}-{label}'

    return _fallback_anchor(prefix, ch_label, source, idx)


def _semantic_id(t: str, block: dict) -> str | None:
    """回傳 catalog 語義 id；無可驗證語義時回 None，不產生 fallback。"""
    labels = _caption_labels(block.get('caption', '')) if t in {'fig', 'table'} else []
    if block.get('catalog_exclude_reason') and not labels:
        return None
    raw_id = (block.get('id') or '').strip()
    if t in {'fig', 'table'}:
        if labels:
            _typ, prefix, num, _caption = labels[0]
            return f'{prefix}-{num}'
        if raw_id and not FALLBACK_ID_RE.search(raw_id):
            return _canonical_catalog_id(raw_id)
    if t == 'eq':
        if raw_id and raw_id.startswith('eq-'):
            return raw_id
        label = _eq_label(block)
        return f'eq-{label}' if label else None
    return None


def _chunk_target(stem: str) -> tuple[str, int | str]:
    if stem.startswith('ch'):
        return 'ch', int(stem[2:])
    if stem.startswith('app'):
        return 'app', stem[3:]
    return 'chunk', stem


def _walk_blocks(blocks: list[dict], section_stack: list[str], ch_label: str,
                 problem_num: str | None = None, source: str = 'body') -> list[dict]:
    """掃描 body blocks，回傳 catalog entries。"""
    entries: list[dict] = []
    for idx, b in enumerate(blocks):
        t = b.get('t')
        if t == 'section':
            sid = (b.get('id') or '').strip()
            # section 出現時，清掉同層或更深的 subsection
            while section_stack and section_stack[-1].count('.') >= 1:
                section_stack.pop()
            if sid:
                section_stack.append(sid)
            continue
        if t == 'subsection':
            sid = (b.get('id') or '').strip()
            if sid:
                section_stack.append(sid)
            continue

        sec_id = section_stack[-1] if section_stack else None

        if t == 'p':
            leading = _leading_caption(b)
            if leading:
                typ, entry_id, caption = leading
                entries.append({
                    'id': entry_id,
                    'type': typ,
                    'section': sec_id,
                    'problem': problem_num,
                    'source': source,
                    'caption': caption,
                    'src': '',
                    'kind': 'text',
                    'anchor': (b.get('id') or '').strip() or entry_id,
                })
            continue

        if t == 'fig':
            labels = _caption_labels(b.get('caption', ''))
            typ, _prefix, _num = _visual_semantic('fig', b)
            anchor = _anchor_id('fig', b, ch_label, source, idx)
            exclude_reason = None if labels else b.get('catalog_exclude_reason')
            entries.append({
                'id': _semantic_id('fig', b),
                'type': typ,
                'section': sec_id,
                'problem': problem_num,
                'source': source,
                'caption': b.get('caption', ''),
                'src': b.get('src', ''),
                'kind': b.get('kind', 'line'),
                'aspect': b.get('aspect'),
                'catalog_exclude_reason': exclude_reason,
                'anchor': anchor,
            })
            for alias_typ, alias_prefix, alias_num, alias_caption in labels[1:]:
                entries.append({
                    'id': f'{alias_prefix}-{alias_num}',
                    'type': alias_typ,
                    'section': sec_id,
                    'problem': problem_num,
                    'source': source,
                    'caption': alias_caption,
                    'src': b.get('src', ''),
                    'kind': b.get('kind', 'line') if alias_typ == 'figure' else None,
                    'aspect': b.get('aspect'),
                    'catalog_exclude_reason': None,
                    'anchor': anchor,
                    'catalog_alias': True,
                })
            for alias in _catalog_aliases(b, anchor, typ):
                alias['section'] = sec_id
                alias['problem'] = problem_num
                alias['source'] = source
                entries.append(alias)

        elif t == 'table':
            labels = _caption_labels(b.get('caption', ''))
            typ, _prefix, _num = _visual_semantic('table', b)
            anchor = _anchor_id('table', b, ch_label, source, idx)
            exclude_reason = None if labels else b.get('catalog_exclude_reason')
            entries.append({
                'id': _semantic_id('table', b),
                'type': typ,
                'section': sec_id,
                'problem': problem_num,
                'source': source,
                'caption': b.get('caption', ''),
                'kind': 'text' if typ == 'figure' else None,
                'catalog_exclude_reason': exclude_reason,
                'anchor': anchor,
            })
            for alias_typ, alias_prefix, alias_num, alias_caption in labels[1:]:
                entries.append({
                    'id': f'{alias_prefix}-{alias_num}',
                    'type': alias_typ,
                    'section': sec_id,
                    'problem': problem_num,
                    'source': source,
                    'caption': alias_caption,
                    'kind': 'text' if alias_typ == 'figure' else None,
                    'catalog_exclude_reason': None,
                    'anchor': anchor,
                    'catalog_alias': True,
                })
            for alias in _catalog_aliases(b, anchor, typ):
                alias['section'] = sec_id
                alias['problem'] = problem_num
                alias['source'] = source
                entries.append(alias)

        elif t == 'eq':
            label = _eq_label(b)
            anchor = _anchor_id('eq', b, ch_label, source, idx)
            entries.append({
                'id': _semantic_id('eq', b),
                'type': 'equation',
                'section': sec_id,
                'problem': problem_num,
                'source': source,
                'label': label,
                'tex': b.get('tex', ''),
                'tex_preview': b.get('tex', '')[:200],
                'anchor': anchor,
            })

    return entries


def _scan_chunk(slug: str, stem: str) -> list[dict]:
    """掃描單一章/附錄，回傳 catalog entries。"""
    if not _valid_slug(slug) or not _valid_chunk_stem(stem):
        return []
    path = DATA_DIR / slug / 'parsed' / f'{stem}.json'
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))

    entries: list[dict] = []
    section_stack: list[str] = []
    chunk_kind, chunk_key = _chunk_target(stem)

    entries.extend(_walk_blocks(data.get('body', []), section_stack, stem, source='body'))

    for pidx, prob in enumerate(data.get('problems', [])):
        num = prob.get('num')
        entries.extend(_walk_blocks(
            prob.get('body', []), list(section_stack), stem,
            problem_num=num, source=f'prob{pidx}'
        ))
        if prob.get('solution'):
            entries.extend(_walk_blocks(
                prob['solution'], list(section_stack), stem,
                problem_num=num, source=f'sol{pidx}'
            ))

    for e in entries:
        e['chunk_kind'] = chunk_kind
        e['chunk_key'] = chunk_key

    # anchor 是 chunk 內 DOM id；只在同一章/附錄內需要唯一。
    seen: dict[str, int] = {}
    for e in entries:
        key = e['anchor']
        if key in seen and not e.get('catalog_alias'):
            seen[key] += 1
            e['anchor'] = f'{key}--{seen[key]}'
        elif key not in seen:
            seen[key] = 0
    return entries


def build_catalogs(slug: str, dry_run: bool = False) -> dict:
    """為 slug 產/更新 catalogs.json 與 book.json 統計。回傳統計摘要。"""
    if not _valid_slug(slug):
        raise ValueError(f'invalid slug: {slug}')
    parsed_dir = DATA_DIR / slug / 'parsed'
    book_path = parsed_dir / 'book.json'
    if not book_path.is_file():
        raise FileNotFoundError(f'缺 parsed/book.json：{book_path}')

    book = json.loads(book_path.read_text(encoding='utf-8'))
    all_entries: list[dict] = []
    chapter_counts: dict = {}

    for ch in book.get('chapters', []):
        stem = f"ch{ch['num']:02d}"
        entries = _scan_chunk(slug, stem)
        for e in entries:
            e['chapter'] = ch['num']
        all_entries.extend(entries)
        counts = {'figures': 0, 'tables': 0, 'equations': 0}
        for e in entries:
            counts[f"{e['type']}s"] += 1
        chapter_counts[ch['num']] = counts
        ch['figure_count'] = counts['figures']
        ch['table_count'] = counts['tables']
        ch['equation_count'] = counts['equations']

    for app in book.get('appendices', []):
        stem = f"app{app['id']}"
        entries = _scan_chunk(slug, stem)
        for e in entries:
            e['chapter'] = app['id']
        all_entries.extend(entries)
        counts = {'figures': 0, 'tables': 0, 'equations': 0}
        for e in entries:
            counts[f"{e['type']}s"] += 1
        chapter_counts[app['id']] = counts
        app['figure_count'] = counts['figures']
        app['table_count'] = counts['tables']
        app['equation_count'] = counts['equations']

    figures = [e for e in all_entries if e['type'] == 'figure']
    tables = [e for e in all_entries if e['type'] == 'table']
    equations = [e for e in all_entries if e['type'] == 'equation']

    # Catalog entry id 需要全書唯一；anchor 則維持 chunk-local DOM id，不可跟著改。
    seen: dict[str, int] = {}
    duplicates = 0
    for e in all_entries:
        key = e.get('id')
        if not key:
            continue
        if key in seen:
            seen[key] += 1
            e['id'] = f'{key}--{seen[key]}'
            duplicates += 1
        else:
            seen[key] = 0

    catalogs = {
        'figures': figures,
        'tables': tables,
        'equations': equations,
    }

    catalog_counts = {
        'figures': len(figures),
        'tables': len(tables),
        'equations': len(equations),
    }

    if not dry_run:
        book['catalog_counts'] = catalog_counts
        book_path.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding='utf-8')
        (parsed_dir / 'catalogs.json').write_text(
            json.dumps(catalogs, ensure_ascii=False, indent=2), encoding='utf-8'
        )

    return {
        'slug': slug,
        'catalog_counts': catalog_counts,
        'chapter_counts': chapter_counts,
        'duplicates': duplicates,
    }


def build_all(dry_run: bool = False) -> list[dict]:
    """對所有已 parsed 的書回填 catalogs。"""
    results = []
    for book_path in sorted(DATA_DIR.glob('*/parsed/book.json')):
        slug = book_path.parent.parent.name
        if not _valid_slug(slug):
            continue
        try:
            results.append(build_catalogs(slug, dry_run=dry_run))
            r = results[-1]
            print(f'[ok] {slug}: {r["catalog_counts"]} (duplicates={r["duplicates"]})')
        except Exception as e:
            print(f'[err] {slug}: {e}', file=sys.stderr)
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('slug', nargs='?', help='單一 slug')
    ap.add_argument('--all', action='store_true', help='全部已 parsed 書')
    ap.add_argument('--dry-run', action='store_true', help='不寫檔，只報告統計')
    args = ap.parse_args()

    if args.all:
        build_all(dry_run=args.dry_run)
        return 0
    if not args.slug:
        ap.print_help()
        return 2

    result = build_catalogs(args.slug, dry_run=args.dry_run)
    print(f"[ok] {args.slug}: {result['catalog_counts']} (duplicates={result['duplicates']})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
