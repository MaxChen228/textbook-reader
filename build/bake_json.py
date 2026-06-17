#!/usr/bin/env python3
"""把本地 textbooks.corpus 的即時轉換預烤成靜態 JSON。

用法：
    uv run python -m build.bake_json [slug ...]

不帶 slug = 全部書；帶 slug = 只烤指定書（單書驗證用）。
輸出到 ../data/。所有圖片引用的 .jpg 在此一併改寫成 .webp（檔案轉檔由 convert_images.py 負責）。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import nh3

from textbooks import corpus

OUT = Path(__file__).resolve().parent.parent / 'data'
JPG_TO_WEBP = re.compile(r'\.jpg$', re.IGNORECASE)
HTML_IMG_RE = re.compile(r'(src="images/[0-9a-fA-F]+)\.jpg"')

# table.html 是 MinerU OCR 對【自動爬來的任意 PDF】的產出 → 公開站的儲存型 XSS 注入面。
# 烤進 data/ 前以白名單消毒一次（零 runtime 成本、結果可追蹤）：只留表格結構 + 相對 img src，
# 剝 script/on*/style/未知標籤；math $...$ 是純文字、原樣保留。
_TABLE_TAGS = {'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'caption', 'colgroup', 'col',
               'br', 'span', 'sup', 'sub', 'b', 'i', 'em', 'strong', 'u', 'p', 'pre', 'code', 'img'}
_TABLE_ATTRS = {'td': {'colspan', 'rowspan', 'align'}, 'th': {'colspan', 'rowspan', 'align', 'scope'},
                'img': {'src', 'alt'}, 'col': {'span'}, 'colgroup': {'span'}}


def _sanitize_table_html(html: str) -> str:
    return nh3.clean(html, tags=_TABLE_TAGS, attributes=_TABLE_ATTRS)


def _rewrite_blocks(blocks: list) -> None:
    """就地把 fig.src 與 table.html 內的 .jpg 改成 .webp；table.html 先過白名單消毒（XSS）。"""
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        t = b.get('t')
        if t == 'fig' and isinstance(b.get('src'), str):
            b['src'] = JPG_TO_WEBP.sub('.webp', b['src'])
        if t == 'table' and isinstance(b.get('html'), str):
            b['html'] = HTML_IMG_RE.sub(r'\1.webp"', _sanitize_table_html(b['html']))


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


def bake_catalog() -> None:
    """烤 data/catalog.json = 完整收錄表（書單 SoT × 三態，含解答本狀態）。
    與 books.json（已收錄可讀書）並存：books.json 餵 reader 內容、catalog.json 餵 library 收錄表。
    每次 build 都重生（書單/解析狀態會變），冪等。"""
    from book_pipeline import booklists
    cat = booklists.catalog()
    cat['generated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    dump(OUT / 'catalog.json', cat)
    o = cat['overall']
    print(f"baked catalog.json：{o['owned']}/{o['total']} 收錄 · {o['main']} 主書 · "
          f"ready {o['ready']} · absent {o['absent']} · unresolved {o['unresolved']}")


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
    bake_catalog()  # 完整收錄表（書單 SoT × 三態）——每次 build 都重生
    print(f'done: {len(books)} book(s) → {OUT}')


if __name__ == '__main__':
    main(sys.argv[1:])
