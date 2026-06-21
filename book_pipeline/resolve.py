#!/usr/bin/env python3
"""book_pipeline.resolve — **crawl agent 的 harness**：把「書名→z-lib 哪一筆」這個唯一需判斷的步驟
做成一套查詢/落盤工具，讓 LLM crawl agent 親自查、親自挑。

[architect note]
crawl 三段分工：選書（booklists 確定性，canon ∖ owned）→ **解析（本模組：agent 判斷）**
→ 買書（買書員確定性 drain）。解析曾試圖確定性化（標題重疊+信心門檻自動採用），但解答本標題泛化、
主書短題名會撞「假 1.0」（'Chemistry'→《Food Chemistry》、Gallian 題解→Dummit）——**確定性不可靠，
故交回 agent 判斷**。本模組退成工具：
  - 查詢素材：`target`（canonical 規格）、`search`（候選+完整 metadata+**advisory** 信心分）、`inspect`（深查）。
  - 落盤：`commit`——**resolution 已退化為純連結快取**（合格存在四維模型）：只落「維②連結」二態
    `found`（--id+--hash）/ `not_found`（z-lib 真無）。**維①夠格、維③版本、維④解答對齊全落 editions/<slug>.json**
    （`editions set`），不在此。歧義/需裁決 → 開 proposal（domain=crawl）+ 留空（CANDIDATE，下輪 /restock 重查）。
    flock 並發安全、只收書單 target（杜絕 ghost）。
  - 快速路徑：`auto` 只把**零歧義 exact_match 主書**自動採用（省 agent token），其餘全留 agent。
信心分（confidence/MAIN_THRESHOLD/SOL_THRESHOLD）**降級為 advisory 參考帶、不再是裁決閘**：
≥0.7=作者有佐證、~0.6=純標題重疊（可疑，多半跨書誤配）。

全程 **只 search、永不 fetch**（不耗下載額度）；下載由 daemon 買書員依 commit 的 id/hash 做。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from book_pipeline import booklists as bl
from book_pipeline import book_qc
from book_pipeline import crawl_zlib as cz

def enrich_links(bid: str, bhash: str) -> dict:
    """eapi detail → 公開 book 頁 href + 封面 cover（best-effort、純 metadata 不耗下載額度）。
    sidecar 存的 id/短hash 推不出公開 URL（href 用另一組 hash + title-slug + .html）→ 必須撈 detail。
    任何失敗回 {}（commit 不因網路掛掉；缺 href 的 entry 由 backfill 補）。"""
    try:
        d = cz.Client()._get(f'/eapi/book/{bid}/{bhash}').json()
        b = d.get('book') or d
        out = {}
        if b.get('href'):
            out['href'] = b['href']
        if b.get('cover'):
            out['cover'] = b['cover']
        return out
    except Exception:
        return {}


# 標題比對用：去除無資訊的常見詞，避免「Introduction to X」這類殼字灌高重疊
_STOP = {
    'a', 'an', 'the', 'of', 'to', 'and', 'in', 'for', 'on', 'with', 'its',
    'introduction', 'intro', 'principles', 'fundamentals', 'course', 'first',
    'edition', 'vol', 'volume', 'modern', 'elementary', 'applied', 'theory',
}
_AUTHOR_DROP = {'and', 'jr', 'iii', 'von', 'van', 'der', 'den', 'del'}
# advisory 參考帶（**非裁決閘**——agent 自己判斷；只在 search 輸出當 advisory_conf 素材）：
# conf = 0.6×標題重疊 + 0.4×作者命中 → 作者不命中者上限 0.6（純標題分）。實測解答本/短題名在 ~0.6
# 全是跨書誤配（Dummit↔Gallian、Shankar↔Griffiths、Sipser↔Lewis…）→ 看到 ~0.6 要警覺、別採用。
# ≥0.7=作者有佐證較可信。agent 看候選 metadata 綜合判斷，不照分數機械式採用。
MAIN_THRESHOLD = 0.55     # 主書「值得一看」參考線
SOL_THRESHOLD = 0.65      # 解答本「值得一看」參考線（須作者佐證才上 0.7+）


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


def resolution_qc(target: dict, cand_title: str) -> dict:
    """候選書名 vs SoT 的下載前書況預檢（純書名比對、零額度），治本上游 gate。
    回 {'advisory': [...], 'block': [...]}。**整道閘只套 main 目標**——解答本書名天生發散
    （含 "Solutions Manual"、常不重複正書名 → companion 命中、title 與正書名零重疊都是常態），
    一律豁免，交回 kind_match + agent 判斷（見 crawl skill）。main 目標：
      - companion：候選書名含 Study Guide/Manual/Instructor… → block（主書不該是週邊書）。
      - title_mismatch：書名重疊過低 → advisory；重疊=0（零共享區別性 token、鐵定配錯書）→ block。
    cand_title 空 / 非 main → 回空（fail-open 不擋）。"""
    advisory, block = [], []
    if not cand_title or target.get('kind') == 'solution':
        return {'advisory': advisory, 'block': block}
    base = _base_title(target)
    if book_qc.companion_reason(cand_title):
        block.append('companion：主書目標卻抓到週邊/解答書（書名含 Study Guide/Manual…）')
    ov = book_qc.title_overlap(base, cand_title)
    if ov is not None and ov < book_qc.TITLE_OVERLAP_MIN:
        advisory.append(f'title_mismatch({ov:.0%})')
        if ov == 0.0:
            block.append('title_mismatch(0%)：候選書名與 SoT 零重疊，鐵定配錯書')
    return {'advisory': advisory, 'block': block}


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


# ── 嚴格快速路徑（exact match）：確定性只在「零歧義」時自動採用，其餘全交 agent ──────────────
def _norm_title(s: str) -> str:
    """標題正規化（比對用）：切掉副標題（首個 : . , — ( 之前）、只留 alnum+空白、收斂空白、小寫。"""
    s = s or ''
    cut = len(s)
    for sep in (':', ' — ', ' (', '(', ',', '. '):
        i = s.find(sep)
        if i != -1:
            cut = min(cut, i)
    s = re.sub(r'[^a-z0-9 ]', ' ', s[:cut].lower())
    return re.sub(r'\s+', ' ', s).strip()


def exact_match(target: dict, books: list[dict]) -> dict | None:
    """**只服務主書**（解答本一律交 agent——跨書假陽性高發區）。回唯一「正規化標題完全相等 + 作者
    姓氏命中 + 是 pdf 非解答本」的候選，否則 None。比 confidence 門檻嚴得多：
      - 用『完全相等』非『重疊』→ 'Chemistry'(正規化) ≠ 'Principles of Food Chemistry' → 不誤採；
      - canonical 少於 2 詞（Calculus/Chemistry 這類單詞題名）天生歧義 → 永不自動採用，必交 agent；
      - 同名候選 >1（版次/再刷歧義）→ 不自動採，交 agent。
    確定性絕不在有任何歧義時替 agent 決定（呼應「別全盤相信確定性工具」）。"""
    if target['kind'] == 'solution':
        return None
    canon = _norm_title(_base_title(target))
    if len(canon.split()) < 2:
        return None
    sn = query_surname(target.get('author', ''))
    hits = []
    for b in cz._rank(books):
        if (b.get('extension') or '').lower() != 'pdf' or cz._is_solution(b.get('title', '')):
            continue
        if _norm_title(b.get('title', '')) != canon:
            continue
        hay = f"{b.get('author', '')} {b.get('title', '')}".lower()
        if sn and not re.search(rf'\b{re.escape(sn)}\b', hay):
            continue
        hits.append(b)
    return hits[0] if len(hits) == 1 else None


# ── crawl agent 的 harness CLI（agent 在 headless session 內呼叫；輸出 JSON 供其判讀）──────────
def _find_target(slug: str) -> dict | None:
    return next((t for t in bl.targets() if t['slug'] == slug), None)


def _build_query(t: dict) -> str:
    """由 target 自動組查詢字串：主書=書名+第一作者姓氏；解答本=書名+solutions manual。"""
    base = _base_title(t)
    if t['kind'] == 'solution':
        return f'{base} solutions manual'
    return f"{base} {query_surname(t.get('author', ''))}".strip()


def _emit(obj) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0


def cmd_queue(args) -> int:
    """agent 工作清單（書單序）。--state candidate（預設，無連結＝待查連結）/ pending（有連結但維③④①未
    全過＝存量回查母體，/restock 升級用）。daemon 通常已在 prompt 指定批次，此為自查備援。"""
    state = getattr(args, 'state', None) or 'candidate'
    todo = bl.pending_targets() if state == 'pending' else bl.unresolved_targets()
    if args.field:
        todo = [t for t in todo if t.get('field_id') == args.field]
    if args.limit:
        todo = todo[:args.limit]
    return _emit({'count': len(todo), 'state': state,
                  'targets': [{'slug': t['slug'], 'kind': t['kind'], 'title': _base_title(t),
                               'author': t.get('author', ''), 'field': t.get('field', ''),
                               'edition_pref': t.get('edition_pref', '')} for t in todo]})


def cmd_target(args) -> int:
    """看單一 target 的 canonical 規格（你在找什麼）+ 狀態。解答本附主書身份/是否已收。"""
    t = _find_target(args.slug)
    if not t:
        return _emit({'error': f'{args.slug} 非書單 target'}) or 2
    have, res = bl.have_slugs(), bl.load_resolution()
    out = {'slug': t['slug'], 'kind': t['kind'], 'title': _base_title(t), 'title_full': t['title'],
           'author': t.get('author', ''), 'edition_pref': t.get('edition_pref', ''),
           'field': t.get('field', ''), 'of': t.get('of'),
           'status': bl.status_of(t['slug'], have, res),
           'suggested_query': _build_query(t)}
    if t['kind'] == 'solution' and t.get('of'):
        out['main_status'] = bl.status_of(t['of'], have, res)
        out['main_owned'] = t['of'] in have
    return _emit(out)


def cmd_search(args) -> int:
    """查 z-lib 候選（**只 search、不下載、不耗額度**）。每筆帶完整 metadata + advisory_conf
    （標題重疊×0.6+作者命中×0.4，**只是參考素材、不是裁決**）+ kind_match（類型是否相符）。"""
    t = _find_target(args.slug)
    if not t:
        return _emit({'error': f'{args.slug} 非書單 target'}) or 2
    q = args.query or _build_query(t)
    try:
        books = cz.Client().search(q, ext=(None if args.any_ext else 'pdf'),
                                   lang=(None if args.any_lang else 'english'), limit=args.limit)
    except Exception as e:
        return _emit({'error': f'search 失敗：{e}'}) or 1
    known = cz._known_md5()
    base, author = _base_title(t), t.get('author', '')
    rows = []
    for b in cz._rank(books):
        r = cz._annotate(b, known)
        r['advisory_conf'] = confidence(base, author, b)
        r['kind_match'] = (cz._is_solution(b.get('title', '')) == (t['kind'] == 'solution'))
        r['book_qc'] = resolution_qc(t, b.get('title', ''))  # 下載前書況預檢（block→不可採用）
        rows.append(r)
    return _emit({'query': q, 'target': {'slug': t['slug'], 'kind': t['kind'],
                  'title': base, 'author': author}, 'candidates': rows})


def cmd_inspect(args) -> int:
    """單一候選的完整 metadata（id/hash 深查 z-lib detail）——版次/語言/描述歧義時 disambiguate 用。"""
    try:
        detail = cz.Client()._get(f'/eapi/book/{args.id}/{args.hash}').json()
    except Exception as e:
        return _emit({'error': f'inspect 失敗：{e}'}) or 1
    b = detail.get('book') or detail
    keys = ('id', 'hash', 'title', 'author', 'year', 'edition', 'publisher', 'language',
            'extension', 'filesize', 'pages', 'isbn', 'isbns', 'series', 'categories', 'description')
    out = {k: b.get(k) for k in keys if b.get(k) is not None}
    out['mb'] = round((b.get('filesize') or 0) / 1e6, 1)
    out['is_solution'] = cz._is_solution(b.get('title', ''))
    return _emit(out)


def cmd_commit(args) -> int:
    """落盤「維②連結」判斷 → crawl_resolution.json（**純連結快取**，flock 並發安全）。二態：
      found：--id <id> --hash <hash>（+選填 --title/--author/--mb；寫 status:'found'）= 找到可下載連結
      --status not_found（或 legacy --absent）：z-lib 真無此書/解答 → REJECTED、**永不再查**（殺空轉）
    只接受書單 target slug（拒絕寫非 target，杜絕 ghost）。
    **維①夠格 / 維③版本 / 維④解答對齊不在此**——落 editions/<slug>.json（`editions set`，多源 LLM 親查）。
    舊 version_unavailable/review 二態已廢（合格存在模型）：只有別版 → 仍 commit 該連結為 found、再
    `editions set --no-matches-pref`（維③ 不過 → 自然 PENDING、可重查）；歧義/需裁決 → 開 proposal
    （domain=crawl）+ 不 commit（留 CANDIDATE，下輪 /restock 重查）。"""
    t = _find_target(args.slug)
    if not t:
        return _emit({'error': f'{args.slug} 非書單 target → 拒絕落盤（不寫 ghost）'}) or 2

    found = bool(args.id or args.hash)
    status = getattr(args, 'status', None)
    if getattr(args, 'absent', False):            # legacy 別名 → not_found
        status = status or 'not_found'
    if found and status:
        return _emit({'error': '模式衝突：(--id+--hash) 與 --status/--absent 不可並用'}) or 2
    valid = ('not_found',)
    if status and status not in valid:
        return _emit({'error': f'--status 僅 {valid}（找到連結用 --id+--hash；版本/夠格/對齊落 editions set）'}) or 2

    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    by = getattr(args, 'by', None) or 'agent'   # provenance：/restock skill 傳 'restock'
    if found:
        if not (args.id and args.hash):
            return _emit({'error': 'found 須同時給 --id 與 --hash'}) or 2
        # 下載前書況閘（護欄不可繞）：候選書名鐵定配錯（main 抓週邊 / 書名零重疊）→ 拒落盤，逼改挑或
        # 開 proposal 留 CANDIDATE。純書名比對、零誤判；治本省後續整條鏈浪費。
        qcres = resolution_qc(t, args.title or '')
        if qcres['block'] and not args.force:
            return _emit({'error': '書況閘擋下：此候選鐵定配錯書 → 改挑候選或開 proposal 留空（確認無誤可 --force）',
                          'slug': t['slug'], 'cand_title': args.title or '',
                          'block': qcres['block'], 'advisory': qcres['advisory']}) or 2
        entry = {'status': 'found', 'id': str(args.id), 'hash': str(args.hash),
                 'title': args.title or '', 'author': args.author or '', 'mb': args.mb,
                 'by': by, 'at': now}
        entry.update(enrich_links(str(args.id), str(args.hash)))   # 補 href/cover（公開 book 頁 + 封面）
        if args.note:
            entry['note'] = args.note
    elif status:
        entry = {'status': status, 'note': args.note or '', 'by': by, 'at': now}
    else:
        return _emit({'error': '須 --id+--hash（found）| --status not_found 之一'}) or 2

    bl.save_resolution({t['slug']: entry})
    return _emit({'ok': True, 'slug': t['slug'], 'action': entry['status'], 'entry': entry})


def cmd_auto(args) -> int:
    """確定性快速路徑：對 unresolved **主書**搜尋，只把 exact_match（零歧義）自動採用，其餘留
    unresolved 交 agent。供 daemon 派 agent 前先撿掉零歧義者、省 token。**絕不碰解答本、絕不下載**。"""
    todo = [t for t in bl.unresolved_targets() if t['kind'] == 'main']
    if args.field:
        todo = [t for t in todo if t.get('field_id') == args.field]
    todo = todo[:args.limit]
    if not todo:
        print('auto：無 unresolved 主書')
        return 0
    if args.dry:
        for t in todo:
            print(f'  would try {t["slug"]:42} {_build_query(t)}')
        return 0
    cl = cz.Client()
    updates, n = {}, 0
    for t in todo:
        try:
            books = cl.search(_build_query(t), ext='pdf', lang='english', limit=20)
        except Exception as e:
            print(f'  ✗ {t["slug"]} search 異常 {e}', file=sys.stderr)
            continue
        m = exact_match(t, books)
        if m:
            updates[t['slug']] = {'id': str(m.get('id')), 'hash': m.get('hash'),
                                  'title': m.get('title'), 'author': m.get('author'),
                                  'mb': round((m.get('filesize') or 0) / 1e6, 1),
                                  'by': 'auto-exact', 'at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
            n += 1
            print(f'  ✓ exact {t["slug"]:42} id={m.get("id")}')
    if updates:
        bl.save_resolution(updates)
    print(f'\nauto done：exact 自動採用 {n}/{len(todo)}（其餘 {len(todo) - n} 本留 unresolved 交 agent）')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description='crawl agent harness：書名→z-lib 連結的查詢/判斷/落盤工具（只 search 不下載）')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('queue', help='列工作清單：--state candidate（待查連結）/ pending（存量回查）')
    p.add_argument('--limit', type=int, default=None)
    p.add_argument('--field', default=None)
    p.add_argument('--state', choices=('candidate', 'pending'), default='candidate',
                   help='candidate=無連結待查（預設）；pending=有連結但維③④①未全過（/restock 回查）')
    p.set_defaults(fn=cmd_queue)

    p = sub.add_parser('target', help='看單一 target 的 canonical 規格 + 狀態')
    p.add_argument('slug')
    p.set_defaults(fn=cmd_target)

    p = sub.add_parser('search', help='查 z-lib 候選（metadata + advisory 信心分；不下載）')
    p.add_argument('slug')
    p.add_argument('--query', default=None, help='自訂查詢字串（預設由 target 自動組）')
    p.add_argument('--limit', type=int, default=20)
    p.add_argument('--any-ext', action='store_true', help='不限 pdf（預設只 pdf）')
    p.add_argument('--any-lang', action='store_true', help='不限 english（預設只 english）')
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser('inspect', help='單一候選完整 metadata（disambiguate）')
    p.add_argument('id')
    p.add_argument('hash')
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser('commit', help='落盤維②連結：--id+--hash（found）| --status not_found')
    p.add_argument('slug')
    p.add_argument('--id')
    p.add_argument('--hash')
    p.add_argument('--title', default=None)
    p.add_argument('--author', default=None)
    p.add_argument('--mb', type=float, default=None)
    p.add_argument('--status', choices=('not_found',),
                   help='z-lib 真無 → not_found（永不再查）。找到連結用 --id+--hash；'
                        '版本/夠格/解答對齊落 editions set；歧義開 proposal 留空')
    p.add_argument('--absent', action='store_true', help='[legacy] = --status not_found')
    p.add_argument('--force', action='store_true', help='繞過書況閘（確認候選無誤時才用）')
    p.add_argument('--note', default=None)
    p.add_argument('--by', default='agent', help="provenance 戳記（書單管理 skill 傳 'booklist-manager'）")
    p.set_defaults(fn=cmd_commit)

    p = sub.add_parser('auto', help='確定性快速路徑：只自動採用 exact 主書（其餘留 agent）')
    p.add_argument('--limit', type=int, default=200)
    p.add_argument('--field', default=None)
    p.add_argument('--dry', action='store_true')
    p.set_defaults(fn=cmd_auto)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == '__main__':
    raise SystemExit(main())
