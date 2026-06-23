"""textbooks/corpus.py — 參考書 parsed/ 的唯讀層（含 mtime cache）。

讀 `book_pipeline/mineru_data/<slug>/parsed/` 由 parser.py 產出的：
  - book.json         metadata + 章/附錄 index
  - ch{NN}.json       章節內容（流水 body + problems）
  - app{X}.json       附錄內容
  圖片：`book_pipeline/mineru_data/<slug>/unified/images/`

i18n overlay（由 /translate-book skill 產出，可選）：
  - book.zh.json      章節/附錄 title 中譯
  - ch{NN}.zh.json    稀疏 overlay：{title?, body:[{i, md|title|caption|...}], problems:[{num, body:[{i,...}], solution:[{i,...}]}]}
  - app{X}.zh.json    同上
  載入時若 lang='zh' 且 overlay 存在，在記憶體合併後回前端；原檔不動。

下游：`routes/textbooks_api.py` 對外提供 API；模板渲染。

**這層永遠不認識** MinerU 雜訊 type（page_number / header / footer 等）—
那些東西在 parser.py 已被過濾，永不出現在 parsed/*.json 內。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from book_pipeline import build_catalogs
from book_pipeline.translate import overlay_anchor

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'book_pipeline' / 'mineru_data'
SLUG_RE = re.compile(r'^[a-z0-9_]{1,64}$')
APP_ID_RE = re.compile(r'^[A-Za-z0-9_]{1,16}$')


# ── books index ──────────────────────────────────────────────────────────────

_BooksSignature = tuple[tuple[str, float], ...]
_books_cache: tuple[_BooksSignature, list[dict]] | None = None


def _books_signature() -> _BooksSignature:
    """用 parsed/book.json 清單與 mtime 作快取簽章。"""
    if not DATA_DIR.is_dir():
        return ()
    entries: list[tuple[str, float]] = []
    for path in sorted(DATA_DIR.glob('*/parsed/book.json')):
        entries.append((str(path.relative_to(DATA_DIR)), path.stat().st_mtime))
        zh = path.with_name('book.zh.json')
        if zh.is_file():
            entries.append((str(zh.relative_to(DATA_DIR)), zh.stat().st_mtime))
    return tuple(entries)


def list_books() -> list[dict]:
    """掃描所有有 parsed/book.json 的書。回 slim metadata。"""
    global _books_cache
    sig = _books_signature()
    if not sig:
        return []
    if _books_cache and _books_cache[0] == sig:
        return _books_cache[1]
    books: list[dict] = []
    for rel, _ in sig:
        if not rel.endswith('/parsed/book.json'):
            continue
        bf = DATA_DIR / rel
        b = json.loads(bf.read_text(encoding='utf-8'))
        dir_slug = bf.parents[1].name
        slug = b['slug']
        if not _valid_slug(slug) or slug != dir_slug:
            continue
        books.append({
            'slug': slug,
            'title': b['title'],
            'author': b.get('author'),
            'edition': b.get('edition'),
            'subject': b.get('subject'),
            'chapter_count': len(b.get('chapters', [])),
            'appendix_count': len(b.get('appendices', [])),
            'catalog_counts': b.get('catalog_counts', {}),
            'has_cover': (DATA_DIR / slug / 'cover.jpg').is_file(),
            'has_zh': (DATA_DIR / slug / 'parsed' / 'book.zh.json').is_file(),
        })
    _books_cache = (sig, books)
    return books


def _valid_slug(slug: str) -> bool:
    return isinstance(slug, str) and SLUG_RE.fullmatch(slug) is not None


SUPPORTED_LANGS = ('zh', 'bi')
TRANSLATABLE_FIELDS = ('md', 'title', 'caption', 'footnote')
CAT_NUM_PATTERN = r'[A-Z]?\d+[A-Z]?(?:[.\-–—]\d+)+(?:[A-Za-z](?![A-Za-z]))?'
FIG_NUM_RE = re.compile(rf'Fig(?:ure|\.)?\s*({CAT_NUM_PATTERN})', re.IGNORECASE)
TBL_NUM_RE = re.compile(rf'(?:Table|Tab\.)\s*({CAT_NUM_PATTERN})', re.IGNORECASE)
FIG_CAPTION_RE = re.compile(rf'^\s*(?:Fig(?:ure|\.)?)\s*({CAT_NUM_PATTERN})\s*[.:：-]?', re.IGNORECASE)
TBL_CAPTION_RE = re.compile(rf'^\s*(?:Table|Tab\.?)\s*({CAT_NUM_PATTERN})\s*[.:：-]?', re.IGNORECASE)
EQ_TAG_RE = re.compile(r'\\tag\s*\{([^}]+)\}')


def _overlay_path_suffix(lang: str | None) -> str | None:
    """回 i18n overlay 檔名後綴；None 表示用原檔。
    'zh'（就地替換）與 'bi'（雙語並陳）共用同一份 .zh overlay。"""
    if lang in ('zh', 'bi'):
        return '.zh'
    return None


# ── per-book book.json ───────────────────────────────────────────────────────

_book_cache: dict[tuple[str, str], tuple[tuple[float, float | None], dict]] = {}


def load_book(slug: str, lang: str | None = None) -> dict | None:
    if not _valid_slug(slug):
        return None
    path = DATA_DIR / slug / 'parsed' / 'book.json'
    if not path.is_file():
        return None
    # book index（sidebar TOC / 章節清單）一律原文；雙語只作用於章節正文。
    if lang == 'bi':
        lang = None
    suffix = _overlay_path_suffix(lang)
    ov_path = DATA_DIR / slug / 'parsed' / f'book{suffix}.json' if suffix else None
    mtime = path.stat().st_mtime
    ov_mtime = ov_path.stat().st_mtime if ov_path and ov_path.is_file() else None
    key = (slug, suffix or '')
    cached = _book_cache.get(key)
    if cached and cached[0] == (mtime, ov_mtime):
        data = cached[1]
    else:
        data = json.loads(path.read_text(encoding='utf-8'))
        if ov_path and ov_mtime is not None:
            overlay = json.loads(ov_path.read_text(encoding='utf-8'))
            data = _merge_book_overlay(data, overlay)
        _book_cache[key] = ((mtime, ov_mtime), data)
    _attach_sections(data, slug, lang)
    return data


def _attach_sections(book: dict, slug: str, lang: str | None) -> None:
    """為每個章/附錄注入 sections: [{title, anchor, level}]。錨點與前端 renderBlock 同算法。"""
    for ch in book.get('chapters', []):
        chunk = _load_chunk(slug, f"ch{ch['num']:02d}", lang)
        if chunk:
            ch['sections'] = _extract_sections(chunk.get('body', []), str(ch['num']))
    for ap in book.get('appendices', []):
        chunk = _load_chunk(slug, f"app{ap['id']}", lang)
        if chunk:
            ap['sections'] = _extract_sections(chunk.get('body', []), f"app{ap['id']}")


def _extract_sections(blocks: list[dict], ch_label: str) -> list[dict]:
    out: list[dict] = []
    counter = 0
    for b in blocks:
        t = b.get('t')
        if t not in ('section', 'subsection'):
            continue
        counter += 1
        bid = (b.get('id') or '').strip()
        anchor = f'sec-{bid}' if bid else f'sec-{ch_label}-{counter}'
        out.append({
            'title': b.get('title', ''),
            'id': bid or None,
            'anchor': anchor,
            'level': 2 if t == 'section' else 3,
        })
    return out


def _merge_book_overlay(book: dict, overlay: dict) -> dict:
    """淺合併 chapter / appendix title。原 dict 不變動。"""
    out = dict(book)
    ch_map = {c.get('num'): c.get('title') for c in overlay.get('chapters', [])}
    app_map = {a.get('id'): a.get('title') for a in overlay.get('appendices', [])}
    out['chapters'] = [
        {**c, 'title': ch_map.get(c['num'], c['title'])} for c in book.get('chapters', [])
    ]
    out['appendices'] = [
        {**a, 'title': app_map.get(a['id'], a['title'])} for a in book.get('appendices', [])
    ]
    return out


# ── chapter / appendix ───────────────────────────────────────────────────────

_chunk_cache: dict[tuple[str, str, str], tuple[tuple[float, float | None], dict]] = {}


def load_chapter(slug: str, ch_num: int, lang: str | None = None) -> dict | None:
    return _load_chunk(slug, f'ch{ch_num:02d}', lang)


def load_appendix(slug: str, app_id: str, lang: str | None = None) -> dict | None:
    return _load_chunk(slug, f'app{app_id}', lang)


def _chunk_stem(kind: str, key: str | int) -> str | None:
    if kind == 'ch':
        try:
            return f'ch{int(key):02d}'
        except (TypeError, ValueError):
            return None
    if kind == 'app':
        if not APP_ID_RE.fullmatch(str(key)):
            return None
        return f'app{key}'
    return None


def _chunk_path(slug: str, kind: str, key: str | int) -> Path | None:
    if not _valid_slug(slug):
        return None
    stem = _chunk_stem(kind, key)
    if stem is None:
        return None
    return DATA_DIR / slug / 'parsed' / f'{stem}.json'


EDITABLE_FIELDS = {
    'p': {'md'},
    'section': {'title'},
    'subsection': {'title'},
    'eq': {'tex'},
    'fig': {'caption'},
    'table': {'caption', 'footnote'},
}


def _resolve_block(container: Any, path: list[Any]) -> dict | None:
    cur = container
    for part in path:
        if isinstance(part, str):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        elif isinstance(part, int):
            if not isinstance(cur, list) or part < 0 or part >= len(cur):
                return None
            cur = cur[part]
        else:
            return None
    return cur if isinstance(cur, dict) else None


def _backup_chunk(path: Path) -> Path:
    backup_dir = path.parent / '_edit_backups'
    backup_dir.mkdir(exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = backup_dir / f'{path.stem}.{stamp}.json'
    n = 1
    while backup.exists():
        backup = backup_dir / f'{path.stem}.{stamp}.{n}.json'
        n += 1
    backup.write_text(path.read_text(encoding='utf-8'), encoding='utf-8')
    return backup


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def _clear_book_caches(slug: str) -> None:
    global _books_cache
    _books_cache = None
    for key in list(_book_cache):
        if key[0] == slug:
            _book_cache.pop(key, None)
    for key in list(_chunk_cache):
        if key[0] == slug:
            _chunk_cache.pop(key, None)
    _catalog_cache.pop(slug, None)


def update_block(slug: str, kind: str, key: str | int,
                 path_parts: list[Any], field: str, value: str) -> dict:
    """更新單一 parsed block 文字欄位，並保留 chunk 備份。"""
    chunk_path = _chunk_path(slug, kind, key)
    if chunk_path is None or not chunk_path.is_file():
        raise FileNotFoundError('chunk not found')
    if not isinstance(path_parts, list) or not path_parts:
        raise ValueError('bad block path')
    if field not in {'md', 'title', 'tex', 'caption', 'footnote'}:
        raise ValueError('field is not editable')
    if not isinstance(value, str):
        raise ValueError('value must be string')

    data = json.loads(chunk_path.read_text(encoding='utf-8'))
    block = _resolve_block(data, path_parts)
    if block is None:
        raise LookupError('block not found')
    block_type = block.get('t')
    if field not in EDITABLE_FIELDS.get(block_type, set()):
        raise ValueError(f'{block_type}.{field} is not editable')

    old = block.get(field, '')
    if old == value:
        return {'changed': False, 'block': block}

    backup = _backup_chunk(chunk_path)
    block[field] = value
    _write_json_atomic(chunk_path, data)
    build_catalogs.build_catalogs(slug)
    _clear_book_caches(slug)
    return {
        'changed': True,
        'block': block,
        'backup': str(backup.relative_to(DATA_DIR / slug)),
    }


_catalog_cache: dict[str, tuple[float, dict]] = {}


def _normalize_catalogs(data: dict) -> dict:
    for key in ('figures', 'tables', 'equations'):
        entries = data.get(key)
        if not isinstance(entries, list):
            data[key] = []
            continue
        semantic_entries = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            if key in ('figures', 'tables') and not e.get('id'):
                continue
            if key == 'equations' and not e.get('label'):
                continue
            if e.get('anchor') is None and e.get('id') is not None:
                e['anchor'] = e['id']
            if e.get('chunk_kind') is None or e.get('chunk_key') is None:
                chapter = e.get('chapter')
                if isinstance(chapter, int):
                    e['chunk_kind'] = 'ch'
                    e['chunk_key'] = chapter
                elif chapter is not None:
                    e['chunk_kind'] = 'app'
                    e['chunk_key'] = str(chapter)
            semantic_entries.append(e)
        data[key] = semantic_entries
    return data


def load_catalogs(slug: str) -> dict | None:
    """讀 parsed/catalogs.json；不存在時回空目錄，讓舊 parsed 可漸進回填。"""
    if not _valid_slug(slug):
        return None
    parsed_dir = DATA_DIR / slug / 'parsed'
    book_path = parsed_dir / 'book.json'
    if not book_path.is_file():
        return None
    path = parsed_dir / 'catalogs.json'
    if not path.is_file():
        return {'figures': [], 'tables': [], 'equations': []}
    mtime = path.stat().st_mtime
    cached = _catalog_cache.get(slug)
    if cached and cached[0] == mtime:
        return cached[1]
    data = json.loads(path.read_text(encoding='utf-8'))
    data = _normalize_catalogs(data)
    _catalog_cache[slug] = (mtime, data)
    return data


def _load_chunk(slug: str, stem: str, lang: str | None) -> dict | None:
    if not _valid_slug(slug) or '/' in stem or '\\' in stem or '..' in stem:
        return None
    path = DATA_DIR / slug / 'parsed' / f'{stem}.json'
    if not path.is_file():
        return None
    suffix = _overlay_path_suffix(lang)
    bilingual = lang == 'bi'
    ov_path = DATA_DIR / slug / 'parsed' / f'{stem}{suffix}.json' if suffix else None
    mtime = path.stat().st_mtime
    ov_mtime = ov_path.stat().st_mtime if ov_path and ov_path.is_file() else None
    key = (slug, stem, lang or '')
    cached = _chunk_cache.get(key)
    if cached and cached[0] == (mtime, ov_mtime):
        return cached[1]
    data = json.loads(path.read_text(encoding='utf-8'))
    if ov_path and ov_mtime is not None:
        overlay = json.loads(ov_path.read_text(encoding='utf-8'))
        data = _merge_chunk_overlay(data, overlay, bilingual)
    _ensure_catalog_ids(data, stem)
    _chunk_cache[key] = ((mtime, ov_mtime), data)
    return data


def _caption_num(caption: str, regex: re.Pattern) -> str | None:
    m = regex.search((caption or '').strip())
    return re.sub(r'[\-–—]', '.', m.group(1)) if m else None


def _eq_label(block: dict) -> str | None:
    label = (block.get('label') or '').strip()
    if label:
        return label
    m = EQ_TAG_RE.search(block.get('tex') or '')
    return m.group(1).strip() if m else None


def _leading_media_id(block: dict) -> str | None:
    text = block.get('md') or block.get('text') or ''
    if not isinstance(text, str):
        return None
    fig = FIG_CAPTION_RE.match(text)
    if fig:
        num = _caption_num(f'Figure {fig.group(1)}', FIG_NUM_RE)
        return f'fig-{num}' if num else None
    tbl = TBL_CAPTION_RE.match(text)
    if tbl:
        num = _caption_num(f'Table {tbl.group(1)}', TBL_NUM_RE)
        return f'tbl-{num}' if num else None
    return None


def _fallback_media_id(t: str, b: dict, stem: str, source: str, idx: int) -> str:
    caption = b.get('caption', '')
    if t in {'fig', 'table'}:
        fig_num = _caption_num(caption, FIG_NUM_RE)
        if fig_num:
            return f'fig-{fig_num}'
        tbl_num = _caption_num(caption, TBL_NUM_RE)
        if tbl_num:
            return f'tbl-{tbl_num}'
        prefix = 'fig' if t == 'fig' else 'tbl'
    else:
        label = _eq_label(b)
        if label:
            return f'eq-{stem}-{label}'
        prefix = 'eq'
    suffix = f'{source}-{idx}' if source != 'body' else str(idx)
    return f'{prefix}-{stem}-{suffix}'


def _dedupe_media_id(raw_id: str, seen: dict[str, int]) -> str:
    if raw_id not in seen:
        seen[raw_id] = 0
        return raw_id
    seen[raw_id] += 1
    return f'{raw_id}--{seen[raw_id]}'


def _ensure_block_ids(blocks: list[dict], stem: str, source: str, seen: dict[str, int]) -> None:
    for idx, b in enumerate(blocks):
        t = b.get('t')
        if t == 'p':
            raw_id = (b.get('id') or '').strip() or _leading_media_id(b)
            if raw_id:
                b['id'] = _dedupe_media_id(raw_id, seen)
            continue
        if t not in ('fig', 'table', 'eq'):
            continue
        raw_id = (b.get('id') or '').strip() or _fallback_media_id(t, b, stem, source, idx)
        b['id'] = _dedupe_media_id(raw_id, seen)


def _ensure_catalog_ids(chunk: dict, stem: str) -> None:
    seen: dict[str, int] = {}
    _ensure_block_ids(chunk.get('body', []), stem, 'body', seen)
    for pidx, prob in enumerate(chunk.get('problems', [])):
        _ensure_block_ids(prob.get('body', []), stem, f'prob{pidx}', seen)
        _ensure_block_ids(prob.get('solution', []), stem, f'sol{pidx}', seen)


def _merge_chunk_overlay(chunk: dict, overlay: dict, bilingual: bool = False) -> dict:
    """套用稀疏 overlay：chunk.title、body[i] 與 problems[].{body,solution}[i] 的翻譯欄位。
    bilingual=False：就地替換原欄位；True：寫入 `<field>_zh` 並保留原文。"""
    out = dict(chunk)
    if 'title' in overlay:
        out['title_zh' if bilingual else 'title'] = overlay['title']
    if 'body' in overlay and chunk.get('body'):
        out['body'] = _patch_blocks(chunk['body'], overlay['body'], bilingual)
    if 'problems' in overlay and chunk.get('problems'):
        prob_map = {p.get('num'): p for p in overlay['problems']}
        out['problems'] = [
            (_merge_problem(p, prob_map[p['num']], bilingual) if p.get('num') in prob_map else p)
            for p in chunk['problems']
        ]
    return out


def _merge_problem(prob: dict, overlay: dict, bilingual: bool = False) -> dict:
    out = dict(prob)
    if 'body' in overlay and prob.get('body'):
        out['body'] = _patch_blocks(prob['body'], overlay['body'], bilingual)
    if 'solution' in overlay and prob.get('solution'):
        out['solution'] = _patch_blocks(prob['solution'], overlay['solution'], bilingual)
    return out


def _patch_blocks(blocks: list[dict], patches: list[dict], bilingual: bool = False) -> list[dict]:
    """patches: [{'i': idx, 'a'?: anchor, '<field>': value, ...}, ...]，只處理 TRANSLATABLE_FIELDS。
    bilingual=False：覆蓋原欄位；True：寫入 `<field>_zh`，原文保留供並陳。

    防漂移：parser 重跑會打亂 body 索引，使舊 overlay 的 `i` 指到錯 block（中譯掛錯段、
    甚至視覺上「中文先於英文」）。對策：
      - patch 帶 `a`（來源欄位 hash）→ 用當前 block 重算比對，不符就跳過（退化純英文，不錯置）。
      - 無 `a` 的舊 overlay → 至少要求該 block 真有對應來源欄位，擋掉 md 掛到 eq/section 之類。
    """
    out = [dict(b) for b in blocks]
    for patch in patches:
        idx = patch.get('i')
        if not isinstance(idx, int) or idx < 0 or idx >= len(out):
            continue
        fields = [k for k in patch if k in TRANSLATABLE_FIELDS]
        if not fields:
            continue
        src = out[idx]
        anchor = patch.get('a')
        if anchor is not None:
            if overlay_anchor({f: src.get(f, '') for f in fields}) != anchor:
                continue
        elif not all(isinstance(src.get(f), str) and src[f].strip() for f in fields):
            continue
        for f in fields:
            out[idx][f'{f}_zh' if bilingual else f] = patch[f]
    return out


# ── images ───────────────────────────────────────────────────────────────────

def image_dir(slug: str) -> Path:
    if not _valid_slug(slug):
        return DATA_DIR / '__invalid_slug__' / 'unified' / 'images'
    return DATA_DIR / slug / 'unified' / 'images'


def has_image(slug: str, filename: str) -> bool:
    if not _valid_slug(slug) or '/' in filename or '\\' in filename or '..' in filename:
        return False
    return (image_dir(slug) / filename).is_file()


def cover_path(slug: str) -> Path:
    if not _valid_slug(slug):
        return DATA_DIR / '__invalid_slug__' / 'cover.jpg'
    return DATA_DIR / slug / 'cover.jpg'
