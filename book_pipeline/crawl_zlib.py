#!/usr/bin/env python3
"""book_pipeline.crawl_zlib — z-library eapi 爬取 client（確定性，零 LLM）。

pipeline 最上游：把 z-library 的書下載成 raw_pdfs/<slug>.pdf，銜接既有
mineru_ingest → audit → parse → deploy。設計鐵則：

- 認證只讀 ~/.secrets/zlib.env（ZLIB_EMAIL/ZLIB_PASSWORD/ZLIB_DOMAIN）；
  session（remix_userid/userkey）快取在 ~/.secrets/zlib_session.json（chmod 600），
  避免每次重登；401 自動重登。**絕不把密碼/userkey echo 到 stdout/log。**
- 走 JSON eapi（/eapi/...），不靠 HTML scraping（後者隨版面腐爛）。
- 冪等：已在 slug_map / mineru_data / crawl_manifest（md5）中的書直接跳。
- 每日下載額度（免費 10/日）是真瓶頸：fetch 前查 profile，額度不足乾淨停（rc=4）。

「選哪本/哪版」由 agent 看 search/inventory 輸出後決定；本工具只做確定性
搜尋排序、去重、下載、登錄。

用法：
  uv run --with requests python -m book_pipeline.crawl_zlib limits
  uv run --with requests python -m book_pipeline.crawl_zlib inventory [--json]
  uv run --with requests python -m book_pipeline.crawl_zlib search "<query>" \
      [--ext pdf] [--lang english] [--year-from N] [--limit 20] [--json]
  uv run --with requests python -m book_pipeline.crawl_zlib fetch <id> <hash> --slug <slug>
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BP = os.path.join(ROOT, 'book_pipeline')
DATA = os.path.join(BP, 'mineru_data')
RAW = os.path.join(ROOT, 'raw_pdfs')
SLUG_MAP = os.path.join(BP, 'slug_map.json')
CRAWL_MANIFEST = os.path.join(BP, 'crawl_manifest.json')

ENV_PATH = os.path.expanduser('~/.secrets/zlib.env')
SESSION_PATH = os.path.expanduser('~/.secrets/zlib_session.json')  # legacy 帳號0 快取
ACCOUNTS_PATH = os.path.expanduser('~/.secrets/zlib_accounts.json')
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')

SOLUTION_RE = re.compile(
    r'\b(solutions?\s+manual|instructor\'?s?\s+(solutions?|manual)|answer\s+key|'
    r'\[solutions?\]|solution\s+manual|solutions?\s+to)\b', re.I)


# ── 認證 ──────────────────────────────────────────────────────────────────

def _load_env() -> dict:
    if not os.path.exists(ENV_PATH):
        sys.exit(f'缺 {ENV_PATH}（ZLIB_EMAIL/ZLIB_PASSWORD/ZLIB_DOMAIN）')
    d = {}
    for line in open(ENV_PATH):
        line = line.strip()
        if line and '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            d[k.strip()] = v.strip()
    return d


def _base() -> str:
    return _load_env().get('ZLIB_DOMAIN', 'https://z-library.sk').rstrip('/')


def _accounts() -> list[dict]:
    """多帳號清單 [{email,password}]。優先讀 zlib_accounts.json（輪換用）；
    無則退回 zlib.env 單帳號（向後相容）。每帳號免費 10 下載/日，N 帳號 → 10N/日。"""
    if os.path.exists(ACCOUNTS_PATH):
        try:
            a = json.load(open(ACCOUNTS_PATH)) or []
            if a:
                return a
        except Exception:
            pass
    env = _load_env()
    return [{'email': env['ZLIB_EMAIL'], 'password': env['ZLIB_PASSWORD']}]


def n_accounts() -> int:
    return len(_accounts())


def _session_path(account: int) -> str:
    """帳號別 session 快取路徑。帳號0 沿用 legacy zlib_session.json（向後相容）。"""
    return SESSION_PATH if account == 0 else os.path.expanduser(
        f'~/.secrets/zlib_session_{account}.json')


def _save_session(account: int, uid: str, key: str) -> None:
    old_umask = os.umask(0o077)
    try:
        p = _session_path(account)
        with open(p, 'w') as f:
            json.dump({'remix_userid': uid, 'remix_userkey': key}, f)
        os.chmod(p, 0o600)
    finally:
        os.umask(old_umask)


def _login(account: int = 0) -> tuple[str, str]:
    accts = _accounts()
    if account >= len(accts):
        sys.exit(f'帳號索引 {account} 超出範圍（共 {len(accts)} 個）')
    acc = accts[account]
    base = _base()
    r = requests.post(f'{base}/eapi/user/login',
                      data={'email': acc['email'], 'password': acc['password']},
                      headers={'User-Agent': UA}, timeout=30)
    r.raise_for_status()
    j = r.json()
    if not j.get('success'):
        sys.exit(f'登入失敗（帳號{account} {acc["email"]}）：{j}')
    u = j['user']
    uid, key = str(u['id']), u['remix_userkey']
    _save_session(account, uid, key)
    return uid, key


def _session(account: int = 0) -> tuple[str, str]:
    """回傳該帳號 (uid, key)，優先讀快取，無則登入。"""
    p = _session_path(account)
    if os.path.exists(p):
        try:
            s = json.load(open(p))
            return str(s['remix_userid']), s['remix_userkey']
        except Exception:
            pass
    return _login(account)


class Client:
    def __init__(self, account: int = 0):
        self.account = account
        self.email = _accounts()[account].get('email') if account < n_accounts() else None
        self.base = _base()
        self.uid, self.key = _session(account)
        self.s = requests.Session()
        self.s.headers.update({'User-Agent': UA,
                               'remix-userid': self.uid, 'remix-userkey': self.key})
        self.s.cookies.update({'remix_userid': self.uid, 'remix_userkey': self.key})

    def _relogin(self):
        self.uid, self.key = _login(self.account)
        self.s.headers.update({'remix-userid': self.uid, 'remix-userkey': self.key})
        self.s.cookies.update({'remix_userid': self.uid, 'remix_userkey': self.key})

    def _get(self, path: str, **kw):
        r = self.s.get(self.base + path, timeout=60, **kw)
        if r.status_code in (401, 403):
            self._relogin()
            r = self.s.get(self.base + path, timeout=60, **kw)
        return r

    def profile(self) -> dict:
        j = self._get('/eapi/user/profile').json()
        return j.get('user', {})

    def search(self, q: str, ext=None, lang=None, year_from=None, year_to=None,
               limit=20, page=1) -> list[dict]:
        data = {'message': q, 'limit': limit, 'page': page}
        if ext:
            data['extensions[]'] = ext
        if lang:
            data['languages[]'] = lang
        if year_from:
            data['yearFrom'] = year_from
        if year_to:
            data['yearTo'] = year_to
        r = self.s.post(f'{self.base}/eapi/book/search', data=data, timeout=60)
        if r.status_code in (401, 403):
            self._relogin()
            r = self.s.post(f'{self.base}/eapi/book/search', data=data, timeout=60)
        return r.json().get('books', [])

    def download(self, dl_path: str, dest: str) -> int:
        """下載到 dest，回傳 bytes。dl_path 形如 /dl/xxx。

        z-library 對 /dl/ 加了 JS challenge（「Checking your browser」，回 503 或 HTML，
        純 HTTP client 過不去）。策略：先試 requests（無 challenge 時最快），命中 challenge
        就退到 playwright headless 真瀏覽器執行 JS 後下載。"""
        r = self._get(dl_path, stream=True, allow_redirects=True)
        ctype = r.headers.get('Content-Type', '')
        if r.status_code == 503 or 'text/html' in ctype:
            r.close()  # JS challenge 擋下載 → 真瀏覽器破關
            return self._download_via_browser(self.base + dl_path, dest)
        r.raise_for_status()
        n = 0
        tmp = dest + '.part'
        with open(tmp, 'wb') as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                n += len(chunk)
        os.replace(tmp, dest)
        return n

    def _download_via_browser(self, dl_url: str, dest: str) -> int:
        """playwright headless：帶現有 session cookie 開下載頁，跑完 z-library 的 JS
        challenge 後存檔。chromium 需先裝（`uv run playwright install chromium`）。"""
        from urllib.parse import urlparse
        from playwright.sync_api import sync_playwright
        domain = '.' + (urlparse(self.base).hostname or 'z-library.sk')
        tmp = dest + '.part'
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True)
            try:
                ctx = br.new_context(accept_downloads=True, user_agent=UA)
                ctx.add_cookies([
                    {'name': 'remix_userid', 'value': str(self.uid), 'domain': domain, 'path': '/'},
                    {'name': 'remix_userkey', 'value': self.key, 'domain': domain, 'path': '/'},
                ])
                page = ctx.new_page()
                with page.expect_download(timeout=120000) as di:
                    page.goto(dl_url, wait_until='commit')
                di.value.save_as(tmp)
            finally:
                br.close()
        os.replace(tmp, dest)
        return os.path.getsize(dest)


# ── inventory（去重 + agent context）─────────────────────────────────────

def _slug_map() -> dict:
    try:
        return (json.load(open(SLUG_MAP)) or {}).get('map', {})
    except Exception:
        return {}


def _crawl_manifest() -> list[dict]:
    try:
        return json.load(open(CRAWL_MANIFEST)) or []
    except Exception:
        return []


def _known_slugs() -> set:
    """現有書 slug：slug_map 值 ∪ mineru_data 目錄（含 _sol）∪ crawl_manifest。"""
    slugs = set(_slug_map().values())
    slugs |= {os.path.basename(p.rstrip('/')) for p in glob.glob(f'{DATA}/*/')}
    slugs |= {e['slug'] for e in _crawl_manifest() if e.get('slug')}
    return slugs


def _known_md5() -> set:
    return {e['md5'] for e in _crawl_manifest() if e.get('md5')}


def inventory() -> dict:
    """agent 決策用：現有全部書 slug + 已爬 md5。讓 agent 避免重複爬。"""
    sm = _slug_map()
    return {
        'known_slugs': sorted(_known_slugs()),
        'crawled': [{k: e.get(k) for k in ('slug', 'title', 'author', 'year', 'is_solution')}
                    for e in _crawl_manifest()],
        'raw_filename_to_slug': sm,
    }


# ── search 排序（堪用啟發式）──────────────────────────────────────────────

def _is_solution(title: str) -> bool:
    return bool(SOLUTION_RE.search(title or ''))


def _score(b: dict) -> float:
    """堪用度粗排（最終 type 由下載後 pdf_triage 定）。"""
    s = 0.0
    size = b.get('filesize') or 0
    mb = size / 1e6
    if b.get('extension', '').lower() == 'pdf':
        s += 10
    # 合理檔案大小帶：教科書多在 3–80MB；過小常殘缺/無圖、過大常高解析掃描
    if 3 <= mb <= 80:
        s += 5
    elif 1.5 <= mb < 3 or 80 < mb <= 150:
        s += 1
    elif mb < 1.5:
        s -= 3
    if b.get('publisher'):
        s += 2
    try:
        yr = int(b.get('year') or 0)
        if yr >= 2000:
            s += min((yr - 2000) / 10, 2)
    except Exception:
        pass
    try:
        s += min(float(b.get('interestScore') or 0) / 2, 2.5)
    except Exception:
        pass
    if (b.get('pages') or 0) > 50:
        s += 1
    return s


def _rank(books: list[dict]) -> list[dict]:
    return sorted(books, key=_score, reverse=True)


def _annotate(b: dict, known_md5: set) -> dict:
    return {
        'id': b.get('id'), 'hash': b.get('hash'),
        'title': b.get('title'), 'author': b.get('author'),
        'year': b.get('year'), 'edition': b.get('edition'),
        'publisher': b.get('publisher'),
        'extension': b.get('extension'),
        'mb': round((b.get('filesize') or 0) / 1e6, 1),
        'pages': b.get('pages'),
        'language': b.get('language'),
        'md5': b.get('md5'),
        'interest': b.get('interestScore'),
        'is_solution': _is_solution(b.get('title', '')),
        'have': b.get('md5') in known_md5,
        'score': round(_score(b), 1),
        'dl': b.get('dl'),
    }


# ── CLI ───────────────────────────────────────────────────────────────────

def account_remaining_live(account: int) -> dict:
    """即時查單帳號額度。回 {account,email,used,limit,remaining,premium}；登入失敗 remaining=None。"""
    try:
        u = Client(account).profile()
        used, lim = u.get('downloads_today'), u.get('downloads_limit')
        rem = (lim - used) if (lim is not None and used is not None) else None
        return {'account': account, 'email': _accounts()[account].get('email'),
                'used': used, 'limit': lim, 'remaining': rem,
                'premium': bool(u.get('isPremium'))}
    except SystemExit as e:
        return {'account': account, 'email': _accounts()[account].get('email'),
                'used': None, 'limit': None, 'remaining': None, 'error': str(e)}


def all_remaining() -> list[dict]:
    return [account_remaining_live(i) for i in range(n_accounts())]


def pick_account() -> int | None:
    """挑一個今日仍有額度的帳號（remaining 最多者）做輪換。全耗盡回 None。
    remaining 查不到（登入失敗）的帳號跳過、不誤選。"""
    cand = [a for a in all_remaining() if (a.get('remaining') or 0) > 0]
    if not cand:
        return None
    return max(cand, key=lambda a: a['remaining'])['account']


def cmd_limits(args) -> int:
    accts = all_remaining()
    total = sum(a['remaining'] for a in accts if a.get('remaining') is not None)
    out = {'accounts': accts, 'total_remaining': total,
           'remaining': total}  # remaining=總額（daemon _zlib_remaining 讀此鍵，30/日語意）
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_inventory(args) -> int:
    inv = inventory()
    if args.json:
        print(json.dumps(inv, ensure_ascii=False, indent=2))
    else:
        print(f"已知 slug（{len(inv['known_slugs'])}）：")
        print('  ' + ' '.join(inv['known_slugs']))
        if inv['crawled']:
            print(f"\n已爬（crawl_manifest，{len(inv['crawled'])}）：")
            for e in inv['crawled']:
                tag = ' [SOL]' if e.get('is_solution') else ''
                print(f"  {e['slug']:24} {e.get('title','')}{tag}")
    return 0


def cmd_search(args) -> int:
    books = Client().search(args.query, ext=args.ext, lang=args.lang,
                            year_from=args.year_from, limit=args.limit)
    known = _known_md5()
    rows = [_annotate(b, known) for b in _rank(books)]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"搜尋「{args.query}」→ {len(rows)} 筆（依堪用度排序）\n")
    print(f"{'#':>2} {'id':>9} {'sc':>4} {'mb':>6} {'pg':>5} {'yr':>5} {'ext':>4} {'kind':>4} have  title")
    for i, r in enumerate(rows):
        kind = 'SOL' if r['is_solution'] else 'main'
        have = '✓' if r['have'] else ''
        title = (r['title'] or '')[:60]
        print(f"{i:>2} {r['id']:>9} {r['score']:>4} {r['mb']:>6} "
              f"{str(r['pages'] or ''):>5} {str(r['year'] or ''):>5} "
              f"{(r['extension'] or ''):>4} {kind:>4} {have:>4}  {title}")
    print("\nfetch：crawl_zlib fetch <id> <hash> --slug <slug>")
    return 0


def _register(slug: str, raw_filename: str, book: dict, md5: str) -> None:
    # slug_map 追加（不覆既有）
    try:
        sm = json.load(open(SLUG_MAP))
    except Exception:
        sm = {'map': {}}
    sm.setdefault('map', {})[raw_filename] = slug
    json.dump(sm, open(SLUG_MAP, 'w'), ensure_ascii=False, indent=2)
    # crawl_manifest 追加
    man = _crawl_manifest()
    man = [e for e in man if e.get('slug') != slug]
    man.append({
        'slug': slug, 'raw_filename': raw_filename,
        'zlib_id': book.get('id'), 'hash': book.get('hash'), 'md5': md5,
        'title': book.get('title'), 'author': book.get('author'),
        'year': book.get('year'), 'edition': book.get('edition'),
        'publisher': book.get('publisher'), 'extension': book.get('extension'),
        'filesize': book.get('filesize'), 'pages': book.get('pages'),
        'is_solution': _is_solution(book.get('title', '')),
        'downloaded_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    })
    json.dump(man, open(CRAWL_MANIFEST, 'w'), ensure_ascii=False, indent=2)


def cmd_fetch(args) -> int:
    slug = args.slug
    if slug in _known_slugs() and not args.force:
        print(f'已存在 slug={slug}（slug_map/mineru_data/manifest）→ 跳過。--force 覆寫。')
        return 0
    dest = os.path.join(RAW, f'{slug}.pdf')
    if os.path.exists(dest) and not args.force:
        print(f'已存在檔案 {dest} → 跳過。')
        return 0

    # 多帳號輪換：--account 指定則用之，否則自動挑今日仍有額度者。全耗盡才 clean-stop。
    account = args.account if getattr(args, 'account', None) is not None else pick_account()
    if account is None:
        rems = all_remaining()
        print(f'所有 {len(rems)} 帳號今日額度皆耗盡 → 等明日重置。', file=sys.stderr)
        return 4
    cl = Client(account)
    u = cl.profile()
    used, lim = u.get('downloads_today'), u.get('downloads_limit')
    if lim is not None and used is not None and used >= lim:
        print(f'帳號{account}（{cl.email}）已耗盡（{used}/{lim}）→ 等明日重置。', file=sys.stderr)
        return 4
    print(f'帳號{account}（{cl.email}）額度 {used}/{lim}', file=sys.stderr)

    # 取書詳情拿 dl 路徑（search 也有，但 fetch 走 id 較穩）
    book = None
    detail = cl._get(f'/eapi/book/{args.id}/{args.hash}').json()
    book = detail.get('book') or detail
    dl = book.get('dl')
    if not dl:
        # 退而求其次：用傳入 hash search 再撈（罕見）
        print(f'無 dl 路徑，detail keys={list(detail.keys())}', file=sys.stderr)
        return 1
    ext = (book.get('extension') or 'pdf').lower()
    if ext != 'pdf':
        print(f'警告：extension={ext} 非 pdf，MinerU 吃 PDF；仍下載但需另轉。', file=sys.stderr)

    os.makedirs(RAW, exist_ok=True)
    print(f'下載 id={args.id} → {dest}（額度 {used}/{lim}）…')
    n = cl.download(dl, dest)
    print(f'  完成 {n/1e6:.1f} MB')

    # md5 去重（下載後算）
    import hashlib
    h = hashlib.md5()
    with open(dest, 'rb') as f:
        for c in iter(lambda: f.read(1 << 20), b''):
            h.update(c)
    md5 = h.hexdigest()

    raw_filename = f'{slug}.pdf'
    _register(slug, raw_filename, book, md5)
    print(f'  已登錄 slug_map + crawl_manifest（md5={md5[:8]}…）')
    print(f'  下一步：mineru_ingest raw_pdfs/{slug}.pdf --slug {slug}')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='z-library eapi 爬取 client')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('limits').set_defaults(func=cmd_limits)

    p_inv = sub.add_parser('inventory')
    p_inv.add_argument('--json', action='store_true')
    p_inv.set_defaults(func=cmd_inventory)

    p_s = sub.add_parser('search')
    p_s.add_argument('query')
    p_s.add_argument('--ext', default='pdf')
    p_s.add_argument('--lang', default=None)
    p_s.add_argument('--year-from', type=int, default=None)
    p_s.add_argument('--limit', type=int, default=20)
    p_s.add_argument('--json', action='store_true')
    p_s.set_defaults(func=cmd_search)

    p_f = sub.add_parser('fetch')
    p_f.add_argument('id')
    p_f.add_argument('hash')
    p_f.add_argument('--slug', required=True)
    p_f.add_argument('--force', action='store_true')
    p_f.add_argument('--account', type=int, default=None,
                     help='指定帳號索引（預設自動輪換挑有額度者）')
    p_f.set_defaults(func=cmd_fetch)

    args = ap.parse_args()
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
