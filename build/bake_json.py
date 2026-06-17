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
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

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
        return block.get('tex') or ''
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


def _problem_id(slug: str, kind: str, key: str | int, num: str) -> str:
    return f'tb:{slug}:{kind}:{key}:p:{num}'


def _problem_reader_href(slug: str, kind: str, key: str | int, num: str) -> str:
    return f'index.html#{slug}/{kind}/{key}?problem={quote(num, safe="")}'


def build_problem_index(books: list[dict]) -> list[dict]:
    """Flatten baked textbook problems into a static qbank-like index."""
    problems: list[dict] = []
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
            chunk = _rewrite_chunk(deepcopy(chunk))
            for p in chunk.get('problems') or []:
                num = str(p.get('num') or '').strip()
                if not num:
                    continue
                body = p.get('body') or []
                solution = p.get('solution') or []
                problems.append({
                    'id': _problem_id(slug, 'ch', n, num),
                    'book_slug': slug,
                    'book_title': book.get('title') or b.get('title') or slug,
                    'author': book.get('author') or b.get('author'),
                    'subject': book.get('subject') or b.get('subject'),
                    'kind': 'ch',
                    'key': str(n),
                    'chapter': n,
                    'chapter_title': ch.get('title') or chunk.get('title'),
                    'num': num,
                    'body': body,
                    'solution': solution,
                    'question_text': _blocks_text(body),
                    'solution_text': _blocks_text(solution),
                    'has_solution': bool(solution),
                    'href_reader': _problem_reader_href(slug, 'ch', n, num),
                })
    problems.sort(key=lambda p: (
        p.get('subject') or '',
        p.get('book_title') or '',
        p.get('chapter') or 0,
        p.get('num') or '',
    ))
    return problems


def bake_problems(books: list[dict]) -> None:
    problems = build_problem_index(books)
    dump(OUT / 'problems.json', {
        'version': 1,
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
