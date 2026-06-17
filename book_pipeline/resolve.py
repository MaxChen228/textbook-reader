#!/usr/bin/env python3
"""book_pipeline.resolve — canon 解析器：書單 target →（書名,作者）→ z-lib 具體 id/hash，或標記 absent。

[architect note]
這是整套 crawl **唯一需要『判斷』**的步驟——選書已被 booklists 確定性化（canon ∖ owned ∖ queued），
不再需要 LLM。本步驟把「某書名在 z-lib 上是哪一筆」這個歧義一次性解掉，結果寫進
crawl_resolution.json 永久 cache，之後 refill 純查表、零成本。

確定性優先（covers 易的多數，零 LLM）：search → 標題詞重疊 + 作者姓氏命中算信心分 →
夠高就自動採用最堪用版次（沿用 crawl_zlib._score）。信心不足：
  - 解答本 target → 標 absent（z-lib 無正版題本；**永不再查**，殺掉舊系統每 tick 重新確認的空轉）。
  - 主書 target → 標 review（待 LLM 人工裁決；不再自動重試，避免反覆撞同一歧義）。
每本只處理一次：已有 resolution 條目者跳過（--force 才重解，用於重試 absent/review）。

resolve **只 search、永不 fetch**（不耗下載額度）。下載仍由 daemon 買書員依 resolution 的 id/hash 做。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone

from book_pipeline import booklists as bl
from book_pipeline import crawl_zlib as cz

# 標題比對用：去除無資訊的常見詞，避免「Introduction to X」這類殼字灌高重疊
_STOP = {
    'a', 'an', 'the', 'of', 'to', 'and', 'in', 'for', 'on', 'with', 'its',
    'introduction', 'intro', 'principles', 'fundamentals', 'course', 'first',
    'edition', 'vol', 'volume', 'modern', 'elementary', 'applied', 'theory',
}
_AUTHOR_DROP = {'and', 'jr', 'iii', 'von', 'van', 'der', 'den', 'del'}
MAIN_THRESHOLD = 0.55     # 主書採用信心門檻
SOL_THRESHOLD = 0.50      # 解答本（須先確認 is_solution，門檻可略低）


def _tokens(s: str) -> set:
    return {w for w in re.findall(r'[a-z0-9]+', (s or '').lower())
            if len(w) >= 3 and w not in _STOP}


def _name_tokens(author: str) -> set:
    """作者字串 → 名字 token 集（含 first/last name）：'Sakurai & Napolitano'→{sakurai,napolitano}；
    'J. D. Jackson'→{jackson}。取長度≥3 的英文字、去掉常見連接/縮寫殘渣。author_hit 比對用。"""
    return {w.lower() for w in re.findall(r'[A-Za-z]{3,}', author or '')
            if w.lower() not in _AUTHOR_DROP}


def query_surname(author: str) -> str:
    """查詢用姓氏：取**第一作者**的最後一個名字 token（'John David Jackson'→jackson、
    'Goldstein, Poole & Safko'→goldstein）。比 sorted()[0] 取字母序最前者（常是 first name）準。"""
    first = re.split(r'[,&]', author or '', maxsplit=1)[0]
    toks = [w.lower() for w in re.findall(r'[A-Za-z]{3,}', first)
            if w.lower() not in _AUTHOR_DROP]
    return toks[-1] if toks else ''


def confidence(title: str, author: str, book: dict) -> float:
    """target（書名,作者）對某 z-lib 結果的匹配信心 0~1。
    = 0.6×標題詞重疊（佔 canon 標題詞比例）+ 0.4×作者姓氏是否命中（**詞界**比對結果 author 或 title，
    避免 'Hall'∈'Marshall' 這類子字串假命中）。"""
    ct = _tokens(title)
    if not ct:
        return 0.0
    bt = _tokens(book.get('title', ''))
    overlap = len(ct & bt) / len(ct)
    hay = f"{book.get('author', '')} {book.get('title', '')}".lower()
    author_hit = 1.0 if any(re.search(rf'\b{re.escape(sn)}\b', hay)
                            for sn in _name_tokens(author)) else 0.0
    return round(0.6 * overlap + 0.4 * author_hit, 3)


def _base_title(t: dict) -> str:
    """解答本 target 的 title 是『<主書名> — Solutions』，比對時還原成主書名。"""
    return t['title'].split(' — Solutions')[0] if t['kind'] == 'solution' else t['title']


def pick(target: dict, books: list[dict]) -> tuple[dict | None, float]:
    """從 search 結果挑最佳匹配：主書 target 排除解答本、解答本 target 只收解答本；
    在合格候選中取信心最高者（同分由 crawl_zlib._score 的 _rank 先後決定）。回 (book, conf)。"""
    title, author = _base_title(target), target.get('author', '')
    is_sol = target['kind'] == 'solution'
    best, best_conf = None, 0.0
    for b in cz._rank(books):                      # 已按堪用度排序 → 同信心取較堪用者
        if (b.get('extension') or '').lower() != 'pdf':
            continue
        sol = cz._is_solution(b.get('title', ''))
        if is_sol != sol:                          # 類型不符（主書抓到題本 / 反之）→ 跳
            continue
        conf = confidence(title, author, b)
        if conf > best_conf:
            best, best_conf = b, conf
    return best, best_conf


def resolve_target(client, target: dict) -> tuple[str, dict]:
    """解析單一 target。回 (action, entry)；action ∈ resolved|absent|review。
    entry 直接是要寫進 resolution sidecar 的值。"""
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    title, author = _base_title(target), target.get('author', '')
    sn = query_surname(author)
    q = f"{title} {sn}".strip()                       # 書名 + 第一作者姓氏，提升命中
    if target['kind'] == 'solution':
        q = f"{title} solutions manual"
    books = client.search(q, ext='pdf', lang='english', limit=20)
    best, conf = pick(target, books)
    thr = SOL_THRESHOLD if target['kind'] == 'solution' else MAIN_THRESHOLD
    if best and conf >= thr:
        return 'resolved', {'id': str(best.get('id')), 'hash': best.get('hash'),
                            'title': best.get('title'), 'author': best.get('author'),
                            'mb': round((best.get('filesize') or 0) / 1e6, 1),
                            'conf': conf, 'at': now}
    if target['kind'] == 'solution':
        return 'absent', {'absent': True, 'at': now,
                          'note': f'查無正版解答（best conf {conf}）'}
    return 'review', {'review': True, 'at': now, 'conf': conf,
                      'note': '信心不足，待人工/LLM 裁決',
                      'candidates': [b.get('title') for b in cz._rank(books)[:3]]}


def run(limit: int, field: str | None, only_slug: str | None,
        force: bool, dry: bool) -> dict:
    """解析 unresolved（或 --force 含已解）的 target，最多 limit 本。回 action 計數。"""
    resolution = bl.load_resolution()
    have, queued = bl.have_slugs(), bl.queued_slugs()
    todo = []
    for t in bl.targets():
        if only_slug and t['slug'] != only_slug:
            continue
        if field and t['field_id'] != field:
            continue
        st = bl.status_of(t['slug'], have, queued, resolution)
        if st in (bl.OWNED, bl.QUEUED):
            continue                               # 已有/已排隊 → 不必解析
        if not force and t['slug'] in resolution:
            continue                               # 已解析過（resolved/absent/review）→ 跳，除非 --force
        todo.append(t)
        if len(todo) >= limit:
            break

    if not todo:
        print('resolve：無待解析 target（皆已解析/已收/已排隊）')
        return {}
    if dry:
        print(f'resolve（dry）：將解析 {len(todo)} 本：')
        for t in todo:
            print(f'  {t["slug"]:42} [{t["kind"]}] {_base_title(t)}')
        return {}

    client = cz.Client()                            # 只 search，不耗下載額度
    updates, counts = {}, {'resolved': 0, 'absent': 0, 'review': 0}
    for t in todo:
        try:
            action, entry = resolve_target(client, t)
        except Exception as e:
            print(f'  ✗ {t["slug"]}：解析異常 {e}', file=sys.stderr)
            continue
        updates[t['slug']] = entry
        counts[action] += 1
        tag = {'resolved': '✓', 'absent': '⛔', 'review': '?'}[action]
        extra = f'id={entry.get("id")} conf={entry.get("conf")}' if action == 'resolved' else entry.get('note', '')
        print(f'  {tag} {t["slug"]:42} {extra}')
    if updates:
        bl.save_resolution(updates)
    print(f'\nresolve done：resolved {counts["resolved"]} · absent {counts["absent"]} · '
          f'review {counts["review"]}（寫入 {len(updates)} 筆）')
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description='canon 解析器（書名→z-lib id/hash 或標 absent）')
    ap.add_argument('--limit', type=int, default=25, help='本批最多解析幾本（預設 25）')
    ap.add_argument('--field', default=None, help='只解析某領域 field_id')
    ap.add_argument('--slug', default=None, help='只解析某 slug')
    ap.add_argument('--force', action='store_true', help='重解已有 resolution 者（重試 absent/review）')
    ap.add_argument('--dry', action='store_true', help='只列將解析的 target，不打網路')
    args = ap.parse_args()
    run(args.limit, args.field, args.slug, args.force, args.dry)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
