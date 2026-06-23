#!/usr/bin/env python3
"""Repair parsed catalog media metadata by inferring figure/table caption and id.

目標是對舊資料做「機械可逆」修復，不改 schema、不改資料路徑。
流程：
  1. 深度掃描 body/problem（含 solution）內的 fig/table block
  2. 對缺 caption 或 fallback id 的 block，嘗試從上下文鄰近文字抓出
     \"Figure 1.2 ...\" / \"Table 3.4 ...\" 這類可判讀的標題
  3. 更新 parsed block 的 caption / id
  4. 回收建 catalog 統計（build_catalogs）
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

from book_pipeline import build_catalogs

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'book_pipeline' / 'mineru_data'

CAT_NUM_PATTERN = r'[A-Z]?\d+[A-Z]?(?:[.\-–—]\d+)+(?:[A-Za-z](?![A-Za-z]))?'
FIG_REF_RE = re.compile(rf'\b(?:Figure|Fig\.?|FIGURE)\s+({CAT_NUM_PATTERN})', re.IGNORECASE)
TBL_REF_RE = re.compile(rf'\b(?:Table|Tbl\.?|TABLE|Tab\.?)\s+({CAT_NUM_PATTERN})', re.IGNORECASE)
LEADING_NUM_RE = re.compile(rf'^\s*({CAT_NUM_PATTERN})\s*[.:\-–—]\s+(.+)$')
FALLBACK_ID_RE = re.compile(r'^(?:fig|tbl)-(?:ch\d{2,}|app[^-]+)(?:-|$)')
GENERIC_LOCAL_CAPTION_RE = re.compile(
    r'^(?:Problem\s+.+|Solution to Problem\s+.+)\s+(?:figure|table)(?:\s+\d+)?$',
    re.IGNORECASE,
)

# 僅取前後 3 個 block，並偏向鄰近那一側。
WINDOW = 3


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def _normalize(text: str) -> str:
    text = re.sub(r'\s+', ' ', (text or '').strip())
    return text


def _block_text(block: dict, kind: str) -> str:
    if kind == 'fig':
        texts = [block.get('md', ''), block.get('caption', ''), block.get('text', '')]
    else:
        texts = [block.get('md', ''), block.get('caption', ''), block.get('text', ''), block.get('html', '')]
    return _normalize(' '.join(t for t in texts if isinstance(t, str)))


def _extract_reference(kind: str, text: str) -> tuple[str | None, str | None]:
    regex = FIG_REF_RE if kind == 'fig' else TBL_REF_RE
    m = regex.search(text)
    if not m:
        if kind == 'fig':
            lead = LEADING_NUM_RE.search(text)
            if lead:
                num = _canonical_num(lead.group(1))
                caption = _normalize(lead.group(2).strip(' .:-–—\n\t'))
                return num, caption if caption else None
        return None, None
    num = _canonical_num(m.group(1))
    caption = text[m.end():].strip(' .:-–—\n\t')
    caption = _normalize(caption)
    return num, caption if caption else None


def _canonical_num(num: str) -> str:
    return re.sub(r'[\-–—]', '.', (num or '').strip('.-–—'))


def _expected_id(kind: str, num: str | None) -> str | None:
    if not num:
        return None
    return ('fig-' if kind == 'fig' else 'tbl-') + _canonical_num(num)


def _default_caption(kind: str, num: str | None) -> str | None:
    if not num:
        return None
    label = 'Figure' if kind == 'fig' else 'Table'
    return f'{label} {_canonical_num(num)}'


def _id_num(kind: str, block_id: str) -> str | None:
    prefix = 'fig-' if kind == 'fig' else 'tbl-'
    if not block_id.startswith(prefix):
        return None
    if FALLBACK_ID_RE.search(block_id):
        return None
    return _canonical_num(block_id.removeprefix(prefix).split('--', 1)[0])


def _semantic_id(kind: str, block: dict) -> str | None:
    block_id = str(block.get('id', '')).strip()
    if block_id.startswith(('fig-', 'tbl-')) and not FALLBACK_ID_RE.search(block_id):
        return block_id
    return _id_num(kind, block_id)


def _extract_any_visual_reference(text: str) -> tuple[str | None, str | None, str | None]:
    hits: list[tuple[int, str, str, str | None]] = []
    for kind, regex in (('fig', FIG_REF_RE), ('table', TBL_REF_RE)):
        m = regex.search(text)
        if not m:
            continue
        caption = _normalize(text[m.end():].strip(' .:-–—\n\t'))
        hits.append((m.start(), kind, _canonical_num(m.group(1)), caption or None))
    if not hits:
        return None, None, None
    leading_offset = len(text) - len(text.lstrip())
    _start, kind, num, caption = min(hits, key=lambda item: item[0])
    if _start > leading_offset:
        return None, None, None
    return kind, num, caption


def _group_refs(blocks: list[dict], kind: str) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    regex = FIG_REF_RE if kind == 'fig' else TBL_REF_RE
    for block in blocks:
        text = _block_text(block, kind)
        for match in regex.finditer(text):
            ref = _canonical_num(match.group(1))
            if ref and ref not in seen:
                refs.append(ref)
                seen.add(ref)
    return refs


def _assign_group_refs(blocks: list[dict], kind: str) -> int:
    refs = _group_refs(blocks, kind)
    candidates = [
        block for block in blocks
        if block.get('t') == kind
        and not _semantic_id(kind, block)
    ]
    if not refs or not candidates:
        return 0
    if len(refs) != len(candidates):
        if not (len(refs) == 1 and len(candidates) == 1):
            return 0
    actions = 0
    for ref, block in zip(refs, candidates):
        block['id'] = _expected_id(kind, ref)
        current_caption = str(block.get('caption') or '').strip()
        if not current_caption or GENERIC_LOCAL_CAPTION_RE.match(current_caption):
            block['caption'] = _default_caption(kind, ref)
            actions += 1
        block['catalog_repair_source'] = 'agent-group-ref'
        block.pop('catalog_exclude_reason', None)
        block.pop('catalog_role', None)
        actions += 1
    return actions


def _score_candidate(kind: str, text: str, idx: int, block_idx: int, hit: int) -> int:
    s = 0
    norm = text.strip().lower()
    if kind == 'fig':
        prefix = 'figure'
    else:
        prefix = 'table'
    if norm.startswith(prefix):
        s += 12
    if re.match(rf'^\(?{prefix}', norm):
        s += 6
    distance = abs(block_idx - idx)
    s += max(0, 8 - 2 * distance)
    if hit < distance:
        s += (distance - hit) * 1
    return s


def _context_candidates(
    blocks: list[dict], idx: int, kind: str
) -> tuple[str | None, str | None, int]:
    best_score = -1
    best: tuple[str | None, str | None] = (None, None)
    for dist in range(1, WINDOW + 1):
        for sign in (-1, 1):
            j = idx + sign * dist
            if j < 0 or j >= len(blocks):
                continue
            text = _block_text(blocks[j], kind)
            if not text:
                continue
            num, caption = _extract_reference(kind, text)
            if not num and not caption:
                continue
            score = _score_candidate(kind, text, idx, j, dist)
            if score > best_score:
                best = (num, caption)
                best_score = score
            # 分數高代表高度可用時，提前返回（降低誤修）
            if score >= 16:
                return best[0], best[1], best_score
    return best[0], best[1], best_score


def _collect_chunks(parsed_dir: Path) -> list[tuple[Path, str, dict, str, int, list[dict]]]:
    book = _load_json(parsed_dir / 'book.json')
    items: list[tuple[Path, str, dict, str, int, list[dict]]] = []
    for ch in book.get('chapters', []):
        stem = f"ch{ch['num']:02d}"
        path = parsed_dir / f'{stem}.json'
        data = _load_json(path)
        items.append((path, stem, data, 'body', -1, data.get('body', [])))
        for pidx, prob in enumerate(data.get('problems', [])):
            pnum = prob.get('num')
            items.append((path, stem, prob, f'prob{pidx}', pnum, prob.get('body', [])))
            sol = prob.get('solution')
            if sol is not None:
                items.append((path, stem, prob, f'sol{pidx}', pnum, sol))
    for app in book.get('appendices', []):
        stem = f"app{app['id']}"
        path = parsed_dir / f'{stem}.json'
        data = _load_json(path)
        items.append((path, stem, data, 'body', -1, data.get('body', [])))
    return items


def _needs_repair(block: dict, kind: str) -> bool:
    if block.get('t') != kind:
        return False
    explicit_kind, explicit_num, _caption = _extract_any_visual_reference(_block_text(block, kind))
    if block.get('catalog_exclude_reason'):
        return bool(explicit_kind and explicit_num)
    caption = (block.get('caption') or '').strip()
    if not caption:
        return True
    block_id = str(block.get('id', '')).strip()
    if not block_id:
        return True
    if GENERIC_LOCAL_CAPTION_RE.match(caption):
        return True
    return bool(FALLBACK_ID_RE.search(block_id))


def _contextual_caption(kind: str, source: str, pnum: str | int | None, ordinal: int) -> str | None:
    if pnum in (None, -1, ''):
        return None
    noun = 'figure' if kind == 'fig' else 'table'
    suffix = f' {ordinal + 1}' if ordinal else ''
    if source.startswith('sol'):
        return f'Solution to Problem {pnum} {noun}{suffix}'
    if source.startswith('prob'):
        return f'Problem {pnum} {noun}{suffix}'
    return None


def _repair_block(
    block: dict,
    kind: str,
    idx: int,
    blocks: list[dict],
    source: str,
    pnum: str | int | None,
    visual_ordinal: int,
) -> tuple[bool, int]:
    current_id = str(block.get('id', '')).strip()
    caption = (block.get('caption') or '').strip()
    explicit_kind, explicit_num, explicit_caption = _extract_any_visual_reference(_block_text(block, kind))

    self_num, self_caption = _extract_reference(kind, _block_text(block, kind))
    existing_num = _id_num(kind, current_id)
    num, text_caption, _score = _context_candidates(blocks, idx, kind)
    num = self_num or num or existing_num
    text_caption = (
        self_caption
        or text_caption
        or _default_caption(kind, num)
        or None
    )
    desired_id = _expected_id(kind, num)
    id_mismatch = bool(desired_id and existing_num and existing_num != _canonical_num(num or ''))
    needs = _needs_repair(block, kind) or id_mismatch
    if not needs:
        return False, 0

    changed = False
    actions = 0

    if explicit_kind and explicit_num:
        explicit_id = _expected_id(explicit_kind, explicit_num)
        if explicit_id and current_id != explicit_id:
            block['id'] = explicit_id
            current_id = explicit_id
            changed = True
            actions += 1
        if not caption and explicit_caption:
            label = 'Figure' if explicit_kind == 'fig' else 'Table'
            block['caption'] = f'{label} {explicit_num}: {explicit_caption}'
            caption = block['caption']
            changed = True
            actions += 1
        if block.get('catalog_exclude_reason') or block.get('catalog_role'):
            block.pop('catalog_exclude_reason', None)
            block.pop('catalog_role', None)
            changed = True
            actions += 1
        if changed:
            block['catalog_repair_source'] = 'agent-explicit-caption'

    if not text_caption:
        text_caption = _contextual_caption(kind, source, pnum, visual_ordinal)

    if (not caption or GENERIC_LOCAL_CAPTION_RE.match(caption)) and text_caption:
        block['caption'] = text_caption
        block['catalog_repair_source'] = 'agent-context'
        changed = True
        actions += 1

    # caption 明確帶圖表號時，以 caption 編號作可索引 id；這比 parser fallback 穩定。
    if desired_id and ((not current_id) or FALLBACK_ID_RE.search(current_id) or id_mismatch):
        block['id'] = desired_id
        block['catalog_repair_source'] = 'agent-explicit-ref'
        block.pop('catalog_exclude_reason', None)
        block.pop('catalog_role', None)
        changed = True
        actions += 1

    if not desired_id and not explicit_num and source.startswith(('prob', 'sol')):
        reason_prefix = 'solution_inline' if source.startswith('sol') else 'problem_inline'
        reason = f'{reason_prefix}_unlabeled_{kind}'
        if block.get('catalog_exclude_reason') != reason or block.get('catalog_role') != 'local':
            block['catalog_exclude_reason'] = reason
            block['catalog_role'] = 'local'
            block['catalog_repair_source'] = 'agent-reviewed-noncatalog'
            changed = True
            actions += 1

    if not desired_id and not explicit_num and source == 'body':
        reason = f'unnumbered_body_{kind}'
        if block.get('catalog_exclude_reason') != reason or block.get('catalog_role') != 'excluded':
            block['catalog_exclude_reason'] = reason
            block['catalog_role'] = 'excluded'
            block['catalog_repair_source'] = 'agent-reviewed-noncatalog'
            changed = True
            actions += 1

    return changed, actions


def _validate_chunk_payload(path: Path, payload: object) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f'bad parsed payload: {path}')
    if 'body' not in payload or not isinstance(payload.get('body'), list):
        raise ValueError(f'parsed chunk missing body list: {path}')
    if path.name.startswith('ch') and 'problems' not in payload:
        raise ValueError(f'chapter payload missing problems list: {path}')


def _restore_from_backup(backup_root: Path, parsed_dir: Path, files: set[Path]) -> None:
    for src in sorted(files):
        if src.is_file():
            dst = parsed_dir / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _repair_slug(slug: str, dry_run: bool = False) -> dict[str, int]:
    parsed_dir = DATA_DIR / slug / 'parsed'
    book_file = parsed_dir / 'book.json'
    catalogs_file = parsed_dir / 'catalogs.json'
    if not (book_file.is_file() and catalogs_file.is_file()):
        return {'slug': slug, 'errors': 1}

    backup_dir = parsed_dir / '_manual_repair_backups' / datetime.now().strftime('%Y%m%d-%H%M%S')
    touched: set[Path] = set()
    changed_blocks = 0
    repaired_actions = 0

    chunks = _collect_chunks(parsed_dir)
    path_to_payload: dict[Path, dict] = {}
    for path, _stem, container, source, _pnum, _blocks in chunks:
        if source == 'body':
            path_to_payload[path] = container

    for _path, stem, container, source, pnum, blocks in chunks:
        group_actions = 0
        for kind in ('fig', 'table'):
            group_actions += _assign_group_refs(blocks, kind)
        if group_actions:
            target = parsed_dir / f'{stem}.json'
            touched.add(target)
            changed_blocks += max(1, group_actions // 2)
            repaired_actions += group_actions

        visual_ordinals = {'fig': 0, 'table': 0}
        for idx, block in enumerate(blocks):
            kind = block.get('t')
            if kind not in {'fig', 'table'}:
                continue
            visual_ordinal = visual_ordinals[kind]
            visual_ordinals[kind] += 1
            changed, actions = _repair_block(block, kind, idx, blocks, source, pnum, visual_ordinal)
            if changed:
                target = parsed_dir / f'{stem}.json'
                touched.add(target)
                changed_blocks += 1
                repaired_actions += actions

    if not touched:
        return {
            'slug': slug,
            'errors': 0,
            'touched_files': 0,
            'repaired_blocks': 0,
            'repaired_actions': 0,
        }

    if dry_run:
        return {
            'slug': slug,
            'errors': 0,
            'touched_files': len(touched),
            'repaired_blocks': changed_blocks,
            'repaired_actions': repaired_actions,
            'dry_run': 1,
        }

    # 備份後再寫
    backup_dir.mkdir(parents=True, exist_ok=True)
    for path in (book_file, catalogs_file):
        if path.is_file():
            shutil.copy2(path, backup_dir / path.name)
    for path in touched:
        shutil.copy2(path, backup_dir / path.name)
    for path in sorted(touched):
        data = path_to_payload[path]
        _validate_chunk_payload(path, data)
        _write_json(path, data)

    build_catalogs.build_catalogs(slug)
    return {
        'slug': slug,
        'errors': 0,
        'touched_files': len(touched),
        'repaired_blocks': changed_blocks,
        'repaired_actions': repaired_actions,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--slug', nargs='?', help='只修一本文字（預設全量）')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--rollback', help='指定 backup 資料夾，回滾該次修復')
    args = ap.parse_args()

    if args.rollback:
        if not args.slug:
            return 2
        parsed_dir = DATA_DIR / args.slug / 'parsed'
        backup_root = Path(args.rollback)
        if not backup_root.is_dir():
            return 2
        touched = {p for p in backup_root.iterdir() if p.suffix == '.json'}
        _restore_from_backup(backup_root, parsed_dir, touched)
        build_catalogs.build_catalogs(args.slug)
        print(f'[repair] rollback {args.slug} from {backup_root}')
        return 0

    slugs = [args.slug] if args.slug else []
    if args.all or not args.slug:
        slugs = [p.name for p in DATA_DIR.iterdir() if (p / 'parsed' / 'catalogs.json').is_file()]

    total = Counter()
    for slug in sorted(slugs):
        try:
            report = _repair_slug(slug, dry_run=args.dry_run)
        except Exception as exc:
            print(f'[repair] {slug}: error={exc}')
            total['errors'] += 1
            continue
        if report['errors']:
            total['errors'] += 1
            continue
        total['books'] += 1
        total['touched_files'] += report['touched_files']
        total['repaired_blocks'] += report['repaired_blocks']
        total['repaired_actions'] += report['repaired_actions']
        print(
            f"[repair] {slug}: touched={report['touched_files']} "
            f"blocks={report['repaired_blocks']} actions={report['repaired_actions']}"
        )

    print(
        f"[repair] done: books={total['books']} touched_files={total['touched_files']} "
        f"blocks={total['repaired_blocks']} actions={total['repaired_actions']} errors={total['errors']}"
    )
    return 1 if total['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
