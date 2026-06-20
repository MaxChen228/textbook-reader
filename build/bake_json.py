#!/usr/bin/env python3
"""把本地 textbooks.corpus 的即時轉換預烤成靜態 JSON。

用法：
    uv run python -m build.bake_json [slug ...]

不帶 slug = 全部書；帶 slug = 只烤指定書（單書驗證用）。
輸出到 ../data/。所有圖片引用的 .jpg 在此一併改寫成 .webp（檔案轉檔由 convert_images.py 負責）。
"""
from __future__ import annotations

import json
import os
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


def _block_text(block: dict) -> str:
    t = block.get('t')
    if t in ('section', 'subsection'):
        return block.get('title') or ''
    if t == 'example':
        return f"Example {block.get('id') or ''}".strip()
    if t == 'p':
        return block.get('md') or ''
    if t == 'eq':
        tex = block.get('tex') or ''
        return f'${tex}$' if tex else ''   # 預覽用 → 包 $ 讓 MathJax 認得（_blocks_text 僅供 preview）
    if t == 'fig':
        return block.get('caption') or ''
    if t == 'table':
        return ' '.join([
            block.get('caption') or '',
            block.get('footnote') or '',
        ]).strip()
    return ''


def _blocks_text(blocks: list) -> str:
    return '\n\n'.join(
        text for text in (_block_text(b) for b in blocks or [])
        if text
    )


def _preview(text: str, limit: int = 200) -> str:
    """壓成單行、截斷成卡片預覽＋搜尋用的短字串（避免每題挾帶整段全文）。
    前端會渲染 LaTeX，故截斷不可切在 $…$ 中間 → 落單的 $ 連同殘段砍掉。"""
    text = ' '.join((text or '').split())
    truncated = len(text) > limit
    if truncated:
        cut = text[:limit]
        sp = cut.rfind(' ')
        if sp > limit * 0.6:
            cut = cut[:sp]
        text = cut
    if text.count('$') % 2 == 1:          # $ 落單 = 數學被截一半 → 砍掉殘段
        text = text[:text.rfind('$')].rstrip()
    return text + ('…' if truncated else '')


def _field_map() -> dict[str, dict]:
    """slug → 領域分類（Field→sublist），與 library 收錄表同源（booklists.catalog）。
    frank/srank = 領域/子單在 SoT 的排序，供前端與 library 一致排列。"""
    from book_pipeline import booklists
    cat = booklists.catalog()
    m: dict[str, dict] = {}
    for fi, f in enumerate(cat.get('fields', [])):
        for si, sl in enumerate(f.get('sublists', [])):
            for b in sl.get('books', []):
                m.setdefault(b.get('slug'), {
                    'field': f.get('field'), 'field_id': f.get('field_id'),
                    'sublist': sl.get('name'), 'frank': fi, 'srank': si,
                })
    return m


def build_problem_index(books: list[dict]) -> tuple[list[dict], list[list]]:
    """烤成輕量索引：books 去重表 + 每題 tuple [bookIdx, chapter, num, hasSol, preview]。

    body/solution 區塊**不**入索引（與 data/<slug>/ch/<n>.json 完全重複，
    detail/匯出時 reader 端按需抓那份既有分片）→ 索引從 ~110MB 砍到 ~15MB。
    books 表帶 field/sublist（與 library 同分類）供前端巢狀側欄。
    """
    fmap = _field_map()
    book_table: list[dict] = []
    book_idx: dict[str, int] = {}
    rows: list[list] = []
    for b in books:
        slug = b['slug']
        book = corpus.load_book(slug, None)
        if not book:
            continue
        for ch in book.get('chapters', []):
            n = ch['num']
            chunk = corpus.load_chapter(slug, n, None)
            if not chunk:
                continue
            problems = chunk.get('problems') or []
            if not problems:
                continue
            if slug not in book_idx:
                book_idx[slug] = len(book_table)
                fm = fmap.get(slug, {})
                book_table.append({
                    'slug': slug,
                    'title': book.get('title') or b.get('title') or slug,
                    'author': book.get('author') or b.get('author'),
                    'subject': book.get('subject') or b.get('subject'),
                    'field': fm.get('field') or '其他',
                    'field_id': fm.get('field_id') or 'other',
                    'sublist': fm.get('sublist') or (book.get('subject') or b.get('subject') or '其他'),
                    'frank': fm.get('frank', 999),
                    'srank': fm.get('srank', 999),
                    'chapters': {},
                })
            bi = book_idx[slug]
            ch_title = ch.get('title') or chunk.get('title')
            if ch_title:
                book_table[bi]['chapters'][str(n)] = ch_title
            for p in problems:
                num = str(p.get('num') or '').strip()
                if not num:
                    continue
                preview = _preview(_blocks_text(p.get('body') or []))
                rows.append([bi, n, num, 1 if p.get('solution') else 0, preview])
    rows.sort(key=lambda r: (
        book_table[r[0]].get('subject') or '',
        book_table[r[0]].get('title') or '',
        r[1] if isinstance(r[1], int) else 0,
        r[2],
    ))
    return book_table, rows


def bake_problems(books: list[dict]) -> None:
    book_table, problems = build_problem_index(books)
    dump(OUT / 'problems.json', {
        'version': 2,
        'fields': ['book', 'chapter', 'num', 'has_solution', 'preview'],
        'books': book_table,
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'count': len(problems),
        'problems': problems,
    })
    print(f'baked problems.json：{len(problems)} problems')


def _rewrite_catalogs(cat: dict) -> dict:
    for key in ('figures', 'tables', 'equations'):
        for e in cat.get(key, []) or []:
            if isinstance(e, dict) and isinstance(e.get('src'), str):
                e['src'] = JPG_TO_WEBP.sub('.webp', e['src'])
    return cat


def dump(path: Path, data) -> None:
    """原子寫：同目錄 tmp（帶 pid 避免並行烤者互踩）寫完 os.replace 到正檔。
    非原子寫一旦 build 被殺（launchd walltime/SIGKILL）會留半截 book.json——nginx 直讀
    抓到半截 JSON、storage_gc._deployed 又據此誤判可刪。原子化同時封掉這兩個風險。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.tmp{os.getpid()}')
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')),
                   encoding='utf-8')
    os.replace(tmp, path)


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
          f"ready {o['ready']} · unresolved {o['unresolved']} · 待重查 {o.get('version_unavailable', 0)} · "
          f"無法收錄 {o['absent']}")


def main(argv: list[str]) -> None:
    all_books = corpus.list_books()
    books = all_books
    wanted = set(argv)
    if wanted:
        books = [b for b in all_books if b['slug'] in wanted]
        if not books:
            sys.exit(f'找不到指定 slug：{wanted}')
    else:
        dump(OUT / 'books.json', books)

    for b in books:
        bake_book(b['slug'], bool(b.get('has_zh')))
        print(f'baked {b["slug"]}  (has_zh={b.get("has_zh")})')
    # 單書模式也刷新 books.json（含全部書，前端 library 需完整清單）
    if wanted:
        dump(OUT / 'books.json', all_books)
    bake_catalog()  # 完整收錄表（書單 SoT × 三態）——每次 build 都重生
    bake_problems(all_books)
    print(f'done: {len(books)} book(s) → {OUT}')


if __name__ == '__main__':
    main(sys.argv[1:])
