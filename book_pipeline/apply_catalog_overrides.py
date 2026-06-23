#!/usr/bin/env python3
"""Apply tracked catalog repair overrides to ignored parsed JSON.

Overrides are the reviewable handoff format for agent/LLM catalog repair.  The
parsed files remain generated/ignored, but each repair can be replayed after a
parser rebuild.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from book_pipeline.build_catalogs import build_catalogs
from book_pipeline.status import raw_slug_map

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'book_pipeline' / 'mineru_data'
OVERRIDE_DIR = ROOT / 'book_pipeline' / 'catalog_overrides'
SLUG_RE = re.compile(r'^[a-z0-9_]{1,64}$')
CHUNK_RE = re.compile(r'^(?:ch\d{2}|app[A-Za-z0-9_]{1,16})$')


def _valid_slug(slug: str) -> bool:
    return isinstance(slug, str) and SLUG_RE.fullmatch(slug) is not None


def _valid_chunk(chunk: str) -> bool:
    return isinstance(chunk, str) and CHUNK_RE.fullmatch(chunk) is not None


def _safe_image_filename(filename: str) -> str:
    if not isinstance(filename, str) or not filename:
        raise ValueError('image filename must be non-empty')
    if '/' in filename or '\\' in filename or filename in {'.', '..'}:
        raise ValueError(f'unsafe image filename: {filename!r}')
    if Path(filename).name != filename:
        raise ValueError(f'unsafe image filename: {filename!r}')
    return filename


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _chunk_path(slug: str, chunk: str) -> Path:
    if not _valid_slug(slug):
        raise ValueError(f'invalid slug: {slug}')
    if not _valid_chunk(chunk):
        raise ValueError(f'invalid chunk: {chunk}')
    path = DATA_DIR / slug / 'parsed' / f'{chunk}.json'
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _select_block(data: dict[str, Any], selector: str) -> dict[str, Any]:
    blocks, idx = _select_list_and_index(data, selector)
    return blocks[idx]


def _select_list_and_index(data: dict[str, Any], selector: str) -> tuple[list[dict[str, Any]], int]:
    if selector.startswith('body[') and selector.endswith(']'):
        idx = int(selector[5:-1])
        return data['body'], idx
    if selector.startswith('problem:'):
        prefix, rest = selector.removeprefix('problem:').rsplit(':', 1)
        problem_num = prefix
        # 可選 occurrence 限定 `#OCC`（消歧重複 num）；無 `#` → occ=0＝取第一個 match（舊格式相容）。
        occ = 0
        if '#' in problem_num:
            problem_num, occ_raw = problem_num.rsplit('#', 1)
            occ = int(occ_raw)
        field, raw_idx = rest.split('[', 1)
        idx = int(raw_idx[:-1])
        seen = 0
        for prob in data.get('problems') or []:
            if str(prob.get('num')) == problem_num:
                if seen == occ:
                    blocks = prob.get(field) or []
                    return blocks, idx
                seen += 1
        raise LookupError(f'problem not found: {problem_num}#{occ}')
    raise ValueError(f'unsupported selector: {selector}')


def _backup_once(path: Path, backup_dir: Path, backed_up: set[Path]) -> None:
    if path in backed_up:
        return
    rel = path.relative_to(DATA_DIR)
    dst = backup_dir / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dst)
    backed_up.add(path)


def _apply_set_fields(slug: str, override: dict[str, Any], backup_dir: Path, backed_up: set[Path]) -> None:
    path = _chunk_path(slug, override['chunk'])
    data = _load_json(path)
    block = _select_block(data, override['selector'])
    desired = override.get('set') or {}
    desired_unset = set(override.get('unset') or [])
    if all((block.get(k) == v if v is not None else k not in block) for k, v in desired.items()) and all(k not in block for k in desired_unset):
        return
    if override.get('expect'):
        for key, expected in override['expect'].items():
            if block.get(key) != expected:
                raise ValueError(
                    f"{override.get('id', '<override>')}: expected {key}={expected!r}, got {block.get(key)!r}"
                )
    allowed = {
        'id',
        'caption',
        'src',
        'kind',
        'aspect',
        'catalog_exclude_reason',
        'catalog_repair_source',
        'catalog_aliases',
    }
    for key, value in desired.items():
        if key not in allowed:
            raise ValueError(f"{override.get('id', '<override>')}: unsupported field {key!r}")
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
    for key in desired_unset:
        if key not in allowed:
            raise ValueError(f"{override.get('id', '<override>')}: unsupported unset field {key!r}")
        block.pop(key, None)
    _backup_once(path, backup_dir, backed_up)
    _write_json(path, data)


def _apply_replace_text(slug: str, override: dict[str, Any], backup_dir: Path, backed_up: set[Path]) -> None:
    path = _chunk_path(slug, override['chunk'])
    data = _load_json(path)
    block = _select_block(data, override['selector'])
    field = override.get('field', 'md')
    if field not in {'md', 'caption', 'tex'}:
        raise ValueError(f"{override.get('id', '<override>')}: unsupported text field {field!r}")
    old = override['old']
    new = override['new']
    value = block.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{override.get('id', '<override>')}: {field} is not a string")
    if old not in value:
        if new in value:
            return
        raise ValueError(f"{override.get('id', '<override>')}: old text not found in {field}")
    block[field] = value.replace(old, new, 1)
    _backup_once(path, backup_dir, backed_up)
    _write_json(path, data)


def _raw_pdf_path(slug: str) -> Path:
    raw = raw_slug_map()
    filename = raw.get(slug)
    if not filename:
        raise FileNotFoundError(f'raw PDF not registered/present for {slug}')
    path = ROOT / 'raw_pdfs' / filename
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _crop_pdf_image(slug: str, override: dict[str, Any]) -> tuple[str, float]:
    import fitz  # type: ignore

    page_num = int(override['page'])
    clip = override['clip']
    if len(clip) != 4:
        raise ValueError(f"{override.get('id', '<override>')}: clip must have four values")
    pdf_path = _raw_pdf_path(slug)
    image_id = override.get('image_id') or override['block']['id']
    safe = str(image_id).replace('/', '-')
    filename = _safe_image_filename(override.get('src') or f'manual_{safe}.png')
    dst_dir = DATA_DIR / slug / 'unified' / 'images'
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / filename
    with fitz.open(pdf_path) as doc:
        page = doc[page_num - 1]
        rect = page.rect
        if all(isinstance(v, (int, float)) and 0 <= float(v) <= 1 for v in clip):
            x0, y0, x1, y1 = clip
            crop = fitz.Rect(
                rect.x0 + rect.width * float(x0),
                rect.y0 + rect.height * float(y0),
                rect.x0 + rect.width * float(x1),
                rect.y0 + rect.height * float(y1),
            )
        else:
            crop = fitz.Rect(*[float(v) for v in clip])
        pix = page.get_pixmap(matrix=fitz.Matrix(float(override.get('zoom', 2.5)), float(override.get('zoom', 2.5))), clip=crop, alpha=False)
        pix.save(dst)
        aspect = round(crop.width / crop.height, 3) if crop.height else 1.0
    return filename, aspect


def _apply_pdf_crop_insert(slug: str, override: dict[str, Any], backup_dir: Path, backed_up: set[Path]) -> None:
    path = _chunk_path(slug, override['chunk'])
    data = _load_json(path)
    blocks, idx = _select_list_and_index(data, override['selector'])
    block = dict(override['block'])
    block_id = str(block.get('id') or '')
    src, aspect = _crop_pdf_image(slug, override)
    if block_id:
        for existing in blocks:
            if str(existing.get('id') or '') == block_id:
                existing.update(block)
                existing['src'] = src
                existing.setdefault('kind', 'line')
                existing['aspect'] = aspect
                existing.setdefault('catalog_repair_source', 'agent-pdf-crop')
                _backup_once(path, backup_dir, backed_up)
                _write_json(path, data)
                return
    if block_id and any(str(existing.get('id') or '') == block_id for existing in blocks):
        return
    block.setdefault('t', 'fig')
    block['src'] = src
    block.setdefault('kind', 'line')
    block.setdefault('aspect', aspect)
    block.setdefault('catalog_repair_source', 'agent-pdf-crop')
    position = override.get('position', 'before')
    insert_at = idx if position == 'before' else idx + 1
    blocks.insert(insert_at, block)
    _backup_once(path, backup_dir, backed_up)
    _write_json(path, data)


def _copy_solution_images(slug: str, override: dict[str, Any]) -> int:
    src_slug = override['from_slug']
    if not _valid_slug(src_slug):
        raise ValueError(f'invalid from_slug: {src_slug}')
    src_dir = DATA_DIR / src_slug / 'unified' / 'images'
    dst_dir = DATA_DIR / slug / 'unified' / 'images'
    if not src_dir.is_dir():
        raise FileNotFoundError(src_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for chunk_path in sorted((DATA_DIR / slug / 'parsed').glob('ch[0-9][0-9].json')):
        data = _load_json(chunk_path)
        for prob in data.get('problems') or []:
            for block in prob.get('solution') or []:
                if block.get('t') != 'fig' or not block.get('src'):
                    continue
                name = Path(block['src']).name
                dst = dst_dir / name
                if dst.is_file():
                    continue
                src = src_dir / name
                if not src.is_file():
                    raise FileNotFoundError(src)
                shutil.copy2(src, dst)
                copied += 1
    return copied


def apply_overrides(slug: str) -> dict[str, Any]:
    if not _valid_slug(slug):
        raise ValueError(f'invalid slug: {slug}')
    path = OVERRIDE_DIR / f'{slug}.json'
    if not path.is_file():
        raise FileNotFoundError(path)
    spec = _load_json(path)
    backup_dir = DATA_DIR / slug / 'parsed' / '_override_backups' / datetime.now().strftime('%Y%m%d-%H%M%S')
    backed_up: set[Path] = set()
    stats = {'set_fields': 0, 'replace_text': 0, 'pdf_crop_insert': 0, 'copied_images': 0}
    for override in spec.get('overrides') or []:
        action = override.get('action')
        if action == 'set_fields':
            _apply_set_fields(slug, override, backup_dir, backed_up)
            stats['set_fields'] += 1
        elif action == 'replace_text':
            _apply_replace_text(slug, override, backup_dir, backed_up)
            stats['replace_text'] += 1
        elif action == 'pdf_crop_insert':
            _apply_pdf_crop_insert(slug, override, backup_dir, backed_up)
            stats['pdf_crop_insert'] += 1
        elif action == 'copy_solution_images':
            stats['copied_images'] += _copy_solution_images(slug, override)
        else:
            raise ValueError(f'unsupported action: {action!r}')
    build_catalogs(slug)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('slug')
    args = ap.parse_args()
    stats = apply_overrides(args.slug)
    print(f"[catalog-overrides] {args.slug}: {stats}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
