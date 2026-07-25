#!/usr/bin/env python3
"""把本地 textbooks.corpus 的即時轉換預烤成靜態 JSON。

用法：
    uv run python -m build.bake_json [slug ...]

不帶 slug = 全部書；帶 slug = 只烤指定書（單書驗證用）。
輸出到 ../data/。所有圖片引用的 .jpg 在此一併改寫成 .webp（檔案轉檔由 convert_images.py 負責）。
"""
from __future__ import annotations

import json
import argparse
from functools import lru_cache
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import nh3
from PIL import Image

from textbooks import corpus

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'data'
IMG = ROOT / 'img'
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


@lru_cache(maxsize=None)
def _webp_size(path: str) -> tuple[int, int] | None:
    """讀 webp 的內在尺寸（PIL 只讀檔頭，不解碼像素）。缺檔/壞檔回 None。"""
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def _rewrite_blocks(blocks: list, slug: str) -> None:
    """就地把 fig.src 與 table.html 內的 .jpg 改成 .webp；table.html 先過白名單消毒（XSS）。

    順帶把圖片的內在寬高（w/h）烤進 block：reader 據此在 <img> 上給 width/height，
    瀏覽器排版時就能先留好版位 → 圖載入不再把整段文字往下推（CLS）。
    convert_images 一定先於 bake 跑（見 build_all docstring），故此時 webp 必已存在；
    真缺檔就不寫欄位，前端自然退回舊行為。"""
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        t = b.get('t')
        if t == 'fig' and isinstance(b.get('src'), str):
            b['src'] = JPG_TO_WEBP.sub('.webp', b['src'])
            size = _webp_size(str(IMG / slug / b['src']))
            if size:
                b['w'], b['h'] = size
        if t == 'table' and isinstance(b.get('html'), str):
            b['html'] = HTML_IMG_RE.sub(r'\1.webp"', _sanitize_table_html(b['html']))


def _rewrite_chunk(chunk: dict, slug: str) -> dict:
    _rewrite_blocks(chunk.get('body', []), slug)
    for prob in chunk.get('problems', []):
        _rewrite_blocks(prob.get('body', []), slug)
        _rewrite_blocks(prob.get('solution', []), slug)
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


# ── 書內全文搜尋索引 ────────────────────────────────────────────────────────
# 分詞規格（**前端 assets/js/search.js 必須逐條鏡像，否則索引查不到**）：
#   1. 全部轉小寫，並把附加符號拆掉（NFD 後丟棄結合字元）→ thévenin 與 thevenin 同一個
#      token。教科書滿是 Thévenin/Schrödinger/Gauß，使用者幾乎都打無附加符號的拼法。
#   2. 取所有「字母或數字」的連續段（Python `[^\W_]+` ≒ JS `\p{L}\p{N}` 類）
#   3. 含 CJK 的段：改取所有 2-gram（單字段則取該字），因為中日韓不以空白斷詞
#   4. 其餘：長度 2–30 才收（單字母噪音大、超長多為 OCR 垃圾）
# 索引只是**候選過濾器**：命中哪一章由它決定，實際片語比對與摘要由前端抓該章
# JSON 後在瀏覽器裡做 → 索引即使略有損失也不會給出錯的結果。
_TOKEN_RE = re.compile(r'[^\W_]+', re.UNICODE)
_CJK_RE = re.compile(r'[぀-ヿ㐀-䶿一-鿿豈-﫿]')
# 出現在超過這個比例的章節 = 對縮小範圍毫無幫助（the/of/section…）→ 移進 common 清單，
# 前端見到 common token 一律當「全部命中」，語意等同停用詞。
_COMMON_RATIO = 0.6


def _fold(text: str) -> str:
    """小寫 + 去附加符號。前端 search.js 的 fold() 必須等價。"""
    nfd = unicodedata.normalize('NFD', (text or '').lower())
    return ''.join(c for c in nfd if unicodedata.category(c) != 'Mn')


def _tokenize(text: str) -> set[str]:
    out: set[str] = set()
    for raw in _TOKEN_RE.findall(_fold(text)):
        if _CJK_RE.search(raw):
            if len(raw) == 1:
                out.add(raw)
            else:
                out.update(raw[i:i + 2] for i in range(len(raw) - 1))
        elif 2 <= len(raw) <= 30:
            out.add(raw)
    return out


def _build_search_index(entries: list[tuple[str, str, str, str]]) -> dict:
    """entries = [(kind, key, title, plaintext), …] → 倒排索引。

    posting list 存章節序號陣列（不用 bitmask：JS 位元運算只有 32 bit，>32 章的書會靜默錯）。
    """
    postings: dict[str, list[int]] = {}
    for i, (_kind, _key, _title, text) in enumerate(entries):
        for tok in _tokenize(text):
            postings.setdefault(tok, []).append(i)
    n = len(entries)
    cutoff = max(3, int(n * _COMMON_RATIO)) if n > 5 else n + 1
    tokens = {t: p for t, p in postings.items() if len(p) < cutoff}
    common = sorted(t for t, p in postings.items() if len(p) >= cutoff)
    return {
        'v': 1,
        'chunks': [[k, key, title] for k, key, title, _ in entries],
        'tokens': tokens,
        'common': common,
    }


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


def _book_shard(slug: str, meta: dict, fm: dict) -> dict | None:
    """單書題目分片：該書所有題的 [chapter, num, has_solution, preview]。

    分片是題目索引的**唯一原料**：全域索引由各書分片彙整而成（見 bake_problems）。
    好處有二 —— ① 前端啟動只需下載無 preview 的索引（12.7MB gz → ~0.5MB gz），
    preview 依需要按書抓；② 只有這次 build 到的書要重讀 corpus，其餘直接讀既有分片，
    不必每次部署都重掃全站 12000+ 章。
    """
    book = corpus.load_book(slug, None)
    if not book:
        return None
    chapters: dict[str, str] = {}
    rows: list[list] = []
    for ch in book.get('chapters', []):
        n = ch['num']
        chunk = corpus.load_chapter(slug, n, None)
        if not chunk:
            continue
        problems = chunk.get('problems') or []
        if not problems:
            continue
        ch_title = ch.get('title') or chunk.get('title')
        if ch_title:
            chapters[str(n)] = ch_title
        for prob in problems:
            num = str(prob.get('num') or '').strip()
            if not num:
                continue
            rows.append([n, num, 1 if prob.get('solution') else 0,
                         _preview(_blocks_text(prob.get('body') or []))])
    if not rows:
        return None
    return {
        'slug': slug,
        'title': book.get('title') or meta.get('title') or slug,
        'author': book.get('author') or meta.get('author'),
        'subject': book.get('subject') or meta.get('subject'),
        'field': fm.get('field') or '其他',
        'field_id': fm.get('field_id') or 'other',
        'sublist': fm.get('sublist') or (book.get('subject') or meta.get('subject') or '其他'),
        'frank': fm.get('frank', 999),
        'srank': fm.get('srank', 999),
        'chapters': chapters,
        'rows': rows,
    }


def bake_problems(books: list[dict], rebuilt: set[str] | None = None) -> None:
    """題目索引：全域 index.json（無 preview）+ 每書一個分片（含 preview）。

    rebuilt = 這次真的重烤過的 slug；其餘書沿用磁碟上既有分片（缺分片者才重讀 corpus）。
    """
    fmap = _field_map()
    shard_dir = OUT / 'problems' / 'book'
    book_table: list[dict] = []
    rows: list[list] = []      # [bookIdx, chapter, [num…]]，num 前綴 '*' = 有解答
    total = 0
    for meta in books:
        slug = meta['slug']
        path = shard_dir / f'{slug}.json'
        shard = None
        if rebuilt is None or slug in rebuilt or not path.is_file():
            shard = _book_shard(slug, meta, fmap.get(slug, {}))
            if shard:
                dump(path, shard)
            elif path.is_file():
                path.unlink()          # 書已無題（重解析後題目消失）→ 撤掉舊分片
        else:
            try:
                shard = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                shard = _book_shard(slug, meta, fmap.get(slug, {}))
                if shard:
                    dump(path, shard)
        if not shard:
            continue
        bi = len(book_table)
        book_table.append({k: shard[k] for k in (
            'slug', 'title', 'author', 'subject', 'field', 'field_id',
            'sublist', 'frank', 'srank', 'chapters')})
        by_ch: dict[int, list[str]] = {}
        for ch, num, has_sol, _preview_text in shard['rows']:
            by_ch.setdefault(ch, []).append(('*' if has_sol else '') + num)
            total += 1
        book_table[bi]['count'] = sum(len(v) for v in by_ch.values())
        for ch, nums in by_ch.items():
            rows.append([bi, ch, nums])
    # 全域排序與舊版一致：subject → title → chapter（章內維持書中原順序）
    rows.sort(key=lambda r: (
        book_table[r[0]].get('subject') or '',
        book_table[r[0]].get('title') or '',
        r[1] if isinstance(r[1], int) else 0,
    ))
    dump(OUT / 'problems' / 'index.json', {
        'version': 3,
        'shape': 'rows=[bookIdx, chapter, [num…]]；num 前綴 * = 有解答；preview 在 problems/book/<slug>.json',
        'books': book_table,
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'count': total,
        'rows': rows,
    })
    legacy = OUT / 'problems.json'
    if legacy.is_file():
        legacy.unlink()        # v2 的 45MB 單檔已被分片取代
    print(f'baked problems/index.json：{total} problems · {len(book_table)} books')


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
    search_entries: list[tuple[str, str, str, str]] = []

    def _chunk_text(chunk: dict) -> str:
        """章節全文＝正文 ⊕ 每題題幹與解答（搜尋要找得到題目裡的字）。"""
        parts = [_blocks_text(chunk.get('body') or [])]
        for p in chunk.get('problems') or []:
            parts.append(_blocks_text(p.get('body') or []))
            parts.append(_blocks_text(p.get('solution') or []))
        return '\n'.join(t for t in parts if t)

    for ch in book.get('chapters', []):
        n = ch['num']
        raw = corpus.load_chapter(slug, n, None)
        dump(base / 'ch' / f'{n}.json', _rewrite_chunk(raw, slug))
        search_entries.append(('ch', str(n), raw.get('title') or '', _chunk_text(raw)))
        if has_zh:
            dump(base / 'ch' / f'{n}.zh.json', _rewrite_chunk(corpus.load_chapter(slug, n, 'zh'), slug))
            dump(base / 'ch' / f'{n}.bi.json', _rewrite_chunk(corpus.load_chapter(slug, n, 'bi'), slug))
    for ap in book.get('appendices', []):
        aid = ap['id']
        raw = corpus.load_appendix(slug, aid, None)
        dump(base / 'app' / f'{aid}.json', _rewrite_chunk(raw, slug))
        search_entries.append(('app', str(aid), raw.get('title') or '', _chunk_text(raw)))
        if has_zh:
            dump(base / 'app' / f'{aid}.zh.json', _rewrite_chunk(corpus.load_appendix(slug, aid, 'zh'), slug))
            dump(base / 'app' / f'{aid}.bi.json', _rewrite_chunk(corpus.load_appendix(slug, aid, 'bi'), slug))

    dump(base / 'search.json', _build_search_index(search_entries))


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
          f"合格 {o.get('qualified', o['ready'])} · 待驗證 {o.get('pending', 0)} · "
          f"待查連結 {o.get('candidate', o['unresolved'])} · 無法收錄 {o['absent']}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description='把本地 corpus 預烤成 reader 靜態 JSON')
    ap.add_argument('slug', nargs='*', help='指定書籍 slug；不給則烤全部')
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    all_books = corpus.list_books()
    books = all_books
    wanted = set(args.slug)
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
    bake_problems(all_books, rebuilt={b['slug'] for b in books})
    print(f'done: {len(books)} book(s) → {OUT}')


if __name__ == '__main__':
    main()
