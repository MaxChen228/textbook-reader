#!/usr/bin/env python3
"""把 qbank 的 textbooks.corpus 即時轉換預烤成靜態 JSON。

用法：
    QBANK_ROOT=~/project/qbank python3 build/bake_json.py [slug ...]

不帶 slug = 全部書；帶 slug = 只烤指定書（Phase 0 單書驗證用）。
輸出到 ../data/。所有圖片引用的 .jpg 在此一併改寫成 .webp（檔案轉檔由 convert_images.py 負責）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

QBANK = Path(os.environ.get('QBANK_ROOT', '')).expanduser()
if not QBANK.is_dir():
    sys.exit('需設定 QBANK_ROOT 指向 qbank repo，例如 QBANK_ROOT=~/project/qbank')
sys.path.insert(0, str(QBANK))

from textbooks import corpus  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / 'data'
JPG_TO_WEBP = re.compile(r'\.jpg$', re.IGNORECASE)
HTML_IMG_RE = re.compile(r'(src="images/[0-9a-fA-F]+)\.jpg"')


def _rewrite_blocks(blocks: list) -> None:
    """就地把 fig.src 與 table.html 內的 .jpg 改成 .webp。"""
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        t = b.get('t')
        if t == 'fig' and isinstance(b.get('src'), str):
            b['src'] = JPG_TO_WEBP.sub('.webp', b['src'])
        if t == 'table' and isinstance(b.get('html'), str):
            b['html'] = HTML_IMG_RE.sub(r'\1.webp"', b['html'])


def _rewrite_chunk(chunk: dict) -> dict:
    _rewrite_blocks(chunk.get('body', []))
    for prob in chunk.get('problems', []):
        _rewrite_blocks(prob.get('body', []))
        _rewrite_blocks(prob.get('solution', []))
    return chunk


def _rewrite_catalogs(cat: dict) -> dict:
    for key in ('figures', 'tables', 'equations'):
        for e in cat.get(key, []) or []:
            if isinstance(e, dict) and isinstance(e.get('src'), str):
                e['src'] = JPG_TO_WEBP.sub('.webp', e['src'])
    return cat


def dump(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')),
                    encoding='utf-8')


def bake_book(slug: str, has_zh: bool) -> None:
    base = OUT / slug
    dump(base / 'book.json', corpus.load_book(slug, None))
    if has_zh:
        dump(base / 'book.zh.json', corpus.load_book(slug, 'zh'))

    cat = corpus.load_catalogs(slug) or {'figures': [], 'tables': [], 'equations': []}
    dump(base / 'catalogs.json', _rewrite_catalogs(cat))

    book = corpus.load_book(slug, None)
    for ch in book.get('chapters', []):
        n = ch['num']
        dump(base / 'ch' / f'{n}.json', _rewrite_chunk(corpus.load_chapter(slug, n, None)))
        if has_zh:
            dump(base / 'ch' / f'{n}.zh.json', _rewrite_chunk(corpus.load_chapter(slug, n, 'zh')))
            dump(base / 'ch' / f'{n}.bi.json', _rewrite_chunk(corpus.load_chapter(slug, n, 'bi')))
    for ap in book.get('appendices', []):
        aid = ap['id']
        dump(base / 'app' / f'{aid}.json', _rewrite_chunk(corpus.load_appendix(slug, aid, None)))
        if has_zh:
            dump(base / 'app' / f'{aid}.zh.json', _rewrite_chunk(corpus.load_appendix(slug, aid, 'zh')))
            dump(base / 'app' / f'{aid}.bi.json', _rewrite_chunk(corpus.load_appendix(slug, aid, 'bi')))


def main(argv: list[str]) -> None:
    books = corpus.list_books()
    wanted = set(argv)
    if wanted:
        books = [b for b in books if b['slug'] in wanted]
        if not books:
            sys.exit(f'找不到指定 slug：{wanted}')
    else:
        dump(OUT / 'books.json', books)

    for b in books:
        bake_book(b['slug'], bool(b.get('has_zh')))
        print(f'baked {b["slug"]}  (has_zh={b.get("has_zh")})')
    # 單書模式也刷新 books.json（含全部書，前端 library 需完整清單）
    if wanted:
        dump(OUT / 'books.json', corpus.list_books())
    print(f'done: {len(books)} book(s) → {OUT}')


if __name__ == '__main__':
    main(sys.argv[1:])
