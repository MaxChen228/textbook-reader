#!/usr/bin/env python3
"""book_pipeline/scope_guard.py — 引擎源碼面守衛（worker 越界改核心碼 → 捕成提案 [+還原]）。

第一性原理：機器產物全 gitignore（parsed/data/img/unified/state/leases…），worker 的**合法
輸出只有 per-slug 的 extract_rules.yaml / cover.jpg / catalog_overrides/<slug>.json**（皆非
程式碼面）。`book_pipeline/*.py` 等「核心程式碼面」對**任何** verb 的 worker 都絕不是合法輸出。

把「worker 繞過原始碼」工程化成「worker 只能用工具 + 提出可改進的問題」：工具不夠時它的 patch
變成結構化 engine/patch 提案（improvement 不流失、provenance 留痕），架構師事後收編或駁回。

── 為何用 per-worker bracket，而非全域掃 git ──
本機（felix）同時是開發機與 daemon 機 → 工作樹常態帶著**架構師未提交的 .py 開發**。全域
`git status` 守衛會把架構師自己的開發誤判成越界。故守衛綁進單一 worker 子進程的生命週期：
`snapshot()` 在 spawn 前拍受保護檔內容 hash（**已含架構師既有未提交改動**）→ worker 收尾後
`check_worker()` 再拍、取差集 → **只有「此 worker 存活期間新變動的受保護檔」才歸給它**。架構師
平時（不在任何 worker 視窗內）的開發 pre==post，永不旗標。

── 免責：架構師窗內 commit 不算越界（felix dev=daemon 機核心降噪）──
bracket 比的是內容 hash，故架構師在某 worker 存活期間 commit 受保護檔（HEAD 移動）→ hash 變了 →
舊版會誤判越界（每輪產一批假提案）。鑑別子：post 時該檔若 `git status --porcelain` **乾淨**＝變更
來自 commit（架構師/daemon）；worker 用 Edit/Write **必留未提交髒檔**（無 commit 步驟）→ `_committed_clean`
免責已提交檔。git 出錯保守不免責、照舊旗標，**絕不因 git 故障漏掉真越界**（additive 收斂、單向安全）。

── 安全：先捕後還，還原永不失資料 ──
殘餘風險僅「架構師恰在某 worker 跑時改引擎**且尚未 commit**」→ 該 diff 完整保存在提案裡，一鍵貼回
（已 commit 者由上述免責濾掉）。故即使誤判也可逆。模式（env `BOOK_PIPELINE_SCOPE_GUARD`，daemon 預設 observe）：
  off     — 全關。
  observe — 捕提案 + 大聲 surface，**不還原**（預設；永不與架構師互踩）。
  enforce — 捕提案 + 還原核心（架構師信任後 opt-in）。

CLI：`uv run python -m book_pipeline.scope_guard` 報告當前受保護面 vs HEAD 的 dirty 檔（純唯讀，
供架構師手動稽核；daemon 走 snapshot/check_worker bracket 路徑）。
"""
from __future__ import annotations

import fnmatch
import glob
import hashlib
import os
import subprocess
import sys
import threading

from book_pipeline import proposals
from book_pipeline.jsonio import atomic_write_json, read_json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEN_PATH = os.path.join(ROOT, 'book_pipeline', '.scope_guard_seen.json')
_lock = threading.Lock()  # 並發 worker（_advance_parallel）的 check_worker 序列化

# 受保護程式碼面：對任何 worker 都絕非合法輸出。glob 不跨 '/'，'book_pipeline/*.py' 只匹配該層 .py。
PROTECTED_GLOBS = (
    'book_pipeline/*.py',              # pipeline 引擎 + 測試
    'build/*.py',                      # bake/convert/build_all
    'textbooks/*.py',                  # corpus 唯讀層
    'pyproject.toml', 'uv.lock',       # 依賴宣告
    'book_pipeline/booklists/*.json',  # 書單 SoT（架構師唯一維護，worker 走 crawl proposal）
)
PROTECTED_PREFIXES = ('.claude/skills/',)  # worker 不可改自己的指令


def _mode() -> str:
    m = os.environ.get('BOOK_PIPELINE_SCOPE_GUARD', '').lower()
    if m in ('off', 'observe', 'enforce'):
        return m
    return 'observe' if os.environ.get('BOOK_PIPELINE_REACTIVE') else 'off'


def is_protected(path: str) -> bool:
    if any(path.startswith(p) for p in PROTECTED_PREFIXES):
        return True
    return any(fnmatch.fnmatch(path, g) for g in PROTECTED_GLOBS)


def _git(args: list[str], root: str = ROOT) -> tuple[int, str]:
    try:
        r = subprocess.run(['git', *args], cwd=root, capture_output=True, text=True)
        return r.returncode, r.stdout
    except OSError as e:
        return -1, str(e)


def _protected_files(root: str) -> list[str]:
    """枚舉工作樹中現存的受保護檔（絕對路徑）。"""
    out: list[str] = []
    for g in PROTECTED_GLOBS:
        out += glob.glob(os.path.join(root, g))
    for pre in PROTECTED_PREFIXES:
        out += [p for p in glob.glob(os.path.join(root, pre, '**'), recursive=True)
                if os.path.isfile(p)]
    return out


def _hash_file(path: str) -> str:
    try:
        with open(path, 'rb') as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return ''


def snapshot(root: str = ROOT) -> dict[str, str]:
    """拍受保護面當前內容指紋 {relpath: sha1}。spawn worker 前呼叫（含架構師既有未提交改動）。"""
    return {os.path.relpath(p, root): _hash_file(p) for p in _protected_files(root)}


def _committed_clean(rel: str, root: str) -> bool:
    """rel 在工作樹是否乾淨（無未提交改動）→ hash 變動來自 commit（架構師/daemon），非 worker 越界。
    worker 用 Edit/Write 必留未提交髒檔（M/??/D），故『git 乾淨』可安全免責；含 new(已 commit→空)/
    deleted(已 commit 刪→空)。git 出錯回 False（保守不免責、照舊旗標）→ 絕不因故障漏掉真越界。"""
    rc, out = _git(['status', '--porcelain', '--', rel], root)
    return rc == 0 and out.strip() == ''


def _diff_text(rel: str, code: str, root: str) -> str:
    if code == 'new':
        full = os.path.join(root, rel)
        try:
            with open(full, encoding='utf-8', errors='replace') as f:
                return f'+++ {rel} (untracked 新檔)\n' + f.read()[:20000]
        except OSError as e:
            return f'(無法讀取新檔 {rel}: {e})'
    _, du = _git(['diff', '--', rel], root)
    _, ds = _git(['diff', '--cached', '--', rel], root)
    return ((ds + du) or f'(無 diff 文本，{rel} {code})')[:20000]


def _seen() -> dict:
    return read_json(SEEN_PATH, default={}) or {}


def check_worker(pre: dict[str, str], root: str = ROOT, *, verb: str = '?',
                 slug: str | None = None, session: str = '', log=None) -> list[dict]:
    """worker 收尾時呼叫：對比 pre 快照，把「此 worker 存活期間新變動的受保護檔」捕成 engine/patch
    提案（attribution=verb/slug/session）+ [enforce] 還原。pre==post 的檔（架構師既有改動、worker
    沒碰）一律不旗標。回 handled 清單。冪等：(path, diff-sha1) 去重。並發由 _lock 序列化。"""
    mode = _mode()
    if mode == 'off':
        return []
    with _lock:
        post = snapshot(root)
        changed = []
        for rel in set(pre) | set(post):
            a, b = pre.get(rel), post.get(rel)
            if a == b:
                continue
            code = 'new' if a is None else ('deleted' if b is None else 'modified')
            changed.append((rel, code))
        if not changed:
            return []
        # 免責架構師窗內 commit：hash 變了但工作樹乾淨 = commit 造成（HEAD 移動），非 worker（worker 無
        # commit、必留髒檔）。濾掉後才捕提案 → 根治「felix dev=daemon 機，架構師 commit 撞 worker bracket」
        # 每輪一批假提案。git 出錯保守保留（_committed_clean 回 False）。
        kept = []
        for rel, code in changed:
            if _committed_clean(rel, root):
                if log:
                    log(f'✓ scope_guard：{rel}（{code}）已提交、工作樹乾淨 → 架構師窗內 commit、免責')
                continue
            kept.append((rel, code))
        changed = kept
        if not changed:
            return []
        seen = _seen()
        handled = []
        for rel, code in changed:
            diff = _diff_text(rel, code, root)
            digest = hashlib.sha1(f'{rel}\n{diff}'.encode('utf-8')).hexdigest()[:16]
            if seen.get(rel) == digest:
                continue
            who = f'{verb} {slug or ""}'.strip()
            try:
                pid = proposals.propose(
                    domain='engine', type_='patch',
                    title=f'worker 越界改核心碼：{rel}（{who}）',
                    slug=slug,
                    evidence=(f'scope_guard bracket：worker [{who}] session={session} 存活期間，受保護'
                              f'程式碼面 {rel}（{code}）被改動。程式碼面對任何 worker 都非合法輸出 → '
                              f'判定為 worker 為通過自身階段而擅改引擎/工具不夠逼它繞過。'),
                    proposal=diff,
                    risk=('已自動還原以保護核心（enforce）。' if mode == 'enforce'
                          else 'observe 模式未還原——待架構師裁決收編/還原。'),
                    source='scope_guard',
                )
            except Exception as e:
                pid = f'(propose 失敗: {e})'
            reverted = False
            if mode == 'enforce':
                if code == 'new':
                    try:
                        os.remove(os.path.join(root, rel)); reverted = True
                    except OSError:
                        pass
                else:
                    rc, _ = _git(['checkout', 'HEAD', '--', rel], root)
                    reverted = (rc == 0)
            seen[rel] = digest
            atomic_write_json(SEEN_PATH, seen)
            if log:
                log(f'⛔ scope_guard 越界 [{who}] {rel}（{code}）→ 提案 {pid}'
                    + ('，已還原' if reverted else '，observe 未還原'))
            handled.append({'path': rel, 'code': code, 'proposal_id': pid, 'reverted': reverted})
        return handled


# ── CLI：架構師手動稽核「當前受保護面 vs HEAD 的 dirty 檔」（純唯讀，無 bracket / 無副作用）──
def _parse_porcelain(out: str) -> list[dict]:
    rows = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        code, rest = line[:2], line[3:]
        path = rest.split(' -> ', 1)[1] if ' -> ' in rest else rest
        rows.append({'path': path.strip().strip('"'), 'code': code})
    return rows


def scan(root: str = ROOT) -> list[dict]:
    rc, out = _git(['status', '--porcelain'], root)
    if rc != 0:
        return []
    return [r for r in _parse_porcelain(out) if is_protected(r['path'])]


def main() -> int:
    breaches = scan()
    if not breaches:
        print('✓ scope_guard：受保護程式碼面 vs HEAD 乾淨（無 dirty）')
        return 0
    print(f'受保護程式碼面當前 dirty {len(breaches)} 檔（純報告，未判定越界——daemon 以 worker '
          f'bracket 才能分辨架構師開發 vs worker 越界）：')
    for b in breaches:
        print(f'  {b["code"]}  {b["path"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
