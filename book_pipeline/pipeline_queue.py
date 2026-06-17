#!/usr/bin/env python3
"""book_pipeline.pipeline_queue — 跨書全 stage 單一真相（crawl→…→deploy）。

status.py 是 ingest→audit→parse→sol 的真相；本模組在其前後補上自動化迴圈
新增的階段，組成 daemon 與 skill 共用的完整 work-queue：

  raw PDF (crawl_zlib 下載)
    │ triage     pdf_triage 分類（確定性）           → todo=triage
    │ qc         needs_llm 時視覺驗證（LLM）          → todo=qc        [LLM]
    ▼            （verdict=reject 則停、surface）
  ingest/audit/parse/sol   ← 委派 status.assess（不重造）
    │ deploy     parse 完 → textbook-reader build+push → todo=deploy
    ▼
  done

階段判定只認實際資料 + pipeline_state.json（持久化 qc 結果與部署狀態，
避免重複 LLM 呼叫 / 重複部署）。triage 廉價可隨時重算，不持久化結論只快取。

每個 todo 標 [LLM]（需 headless claude）或 [det]（確定性，daemon 直跑）。

用法：
  uv run --with pymupdf python -m book_pipeline.pipeline_queue           # 全表
  uv run --with pymupdf python -m book_pipeline.pipeline_queue --next    # 下一個可動項
  uv run ... python -m book_pipeline.pipeline_queue --json
"""
from __future__ import annotations

import argparse
import fcntl
import json
import tempfile
import os
import sys
from contextlib import contextmanager

from book_pipeline import jsonio
from book_pipeline import status as st

ROOT = st.ROOT
BP = os.path.join(ROOT, 'book_pipeline')
DATA = st.DATA
STATE_PATH = os.path.join(BP, 'pipeline_state.json')
STATE_LOCK = os.path.join(BP, 'pipeline_state.lock')


@contextmanager
def _state_lock():
    """跨進程互斥鎖保護 pipeline_state.json 的 RMW。並行 advance 下，set_qc（LLM 子進程
    寫）與 mark_deployed（主執行緒寫）會同時讀-改-寫 → 不鎖會丟 verdict/deploy 標記。"""
    with open(STATE_LOCK, 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

# pipeline 已搬進 textbook-reader 本體，READER_ROOT == ROOT；env 僅供特例覆寫。
READER_ROOT = os.environ.get('TEXTBOOK_READER_ROOT', ROOT)

# todo 動詞 → (是否需 LLM)
LLM_TODOS = {'qc', 'audit', 'crawl'}


def _load_state() -> dict:
    # 容錯讀：毀損 → 改名 .corrupt 保全後回 {}（絕不讓壞檔靜默清空全部 QC/deploy/catalog 標記）
    return jsonio.read_json(STATE_PATH, {})


def _save_state(s: dict) -> None:
    # 原子寫：launchd/SIGKILL 寫一半只截斷 tmp，正檔永遠完整（_state_lock 已序列化 RMW）
    jsonio.atomic_write_json(STATE_PATH, s, indent=2)


def set_qc(slug: str, verdict: str, note: str = '', by: str = 'claude') -> None:
    """持久化視覺 QC 結果（pass/reject/review）。daemon/agent 完成 qc 後呼叫。"""
    with _state_lock():
        s = _load_state()
        s.setdefault(slug, {})['qc'] = {'verdict': verdict, 'note': note, 'by': by}
        _save_state(s)


def mark_deployed(slug: str) -> None:
    from datetime import datetime, timezone
    with _state_lock():
        s = _load_state()
        s.setdefault(slug, {})['deployed_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        _save_state(s)


def catalog_llm_done(slug: str, state: dict | None = None) -> bool:
    """該書是否已派過 LLM catalog 修復（避免每 tick 重派；殘留則終局 accept）。"""
    s = state if state is not None else _load_state()
    return bool(s.get(slug, {}).get('catalog_llm_at'))


def mark_catalog_llm_done(slug: str) -> None:
    from datetime import datetime, timezone
    with _state_lock():
        s = _load_state()
        s.setdefault(slug, {})['catalog_llm_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        _save_state(s)


def catalog_accepted(slug: str, state: dict | None = None) -> bool:
    """確定性+LLM 修復後仍殘留（多為 MinerU 源頭缺、無法憑空生）→ 已 accept、不再 gate deploy。"""
    s = state if state is not None else _load_state()
    return bool(s.get(slug, {}).get('catalog_accepted'))


def mark_catalog_accepted(slug: str, residual: int) -> None:
    from datetime import datetime, timezone
    with _state_lock():
        s = _load_state()
        s.setdefault(slug, {})['catalog_accepted'] = {
            'residual': residual,
            'at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        _save_state(s)


# ── crawl 下載失敗計數（買書員直讀解析池下載，唯一需持久的下載態）─────────────
# 買書員不再有購物清單 buffer：每 tick 直接 select_next 取解析池待下載書。一本連 MAX_FETCH_FAILS
# 次 fetch 失敗 → 記此計數，select_next 的 caller 把它排除（不再無限重試卡住隊頭）。源頭變化
# （resolution 重解 / 換 id-hash）時架構師可 clear。存 state[slug]['crawl_fails']（int）。
def crawl_fail_count(slug: str, state: dict | None = None) -> int:
    s = state if state is not None else _load_state()
    return int((s.get(slug, {}) or {}).get('crawl_fails') or 0)


def bump_crawl_fail(slug: str) -> int:
    """+1 該書 fetch 失敗計數，回新值。"""
    with _state_lock():
        s = _load_state()
        n = int((s.get(slug, {}) or {}).get('crawl_fails') or 0) + 1
        s.setdefault(slug, {})['crawl_fails'] = n
        _save_state(s)
    return n


def clear_crawl_fail(slug: str) -> None:
    """清除該書 fetch 失敗計數（抓成功 / 架構師重解後）。"""
    with _state_lock():
        s = _load_state()
        if (s.get(slug, {}) or {}).pop('crawl_fails', None) is not None:
            _save_state(s)


def crawl_blocked_slugs(max_fails: int, state: dict | None = None) -> set:
    """fetch 失敗達上限、該排除出下載候選的 slug 集合（select_next 的 exclude）。"""
    s = state if state is not None else _load_state()
    return {slug for slug, v in s.items()
            if isinstance(v, dict) and int(v.get('crawl_fails') or 0) >= max_fails}


# ── math sweep（Phase 2，corpus-level track-only；不 gate deploy）──────────────
# 每書殘餘存 state[slug]['math']（do_math_track 寫，post-deploy）；全域 sweep 進度存
# state['__math__']（'__' 前綴非合法 slug → 不會被 build_queue 誤當書）。corpus_math_residual
# 是 do_math_sweep 的廉價門檻判據（讀一個 state 檔，不重跑 node）。
MATH_STATE_KEY = '__math__'


def mark_math_validated(slug: str, bad_occ: int, macros_version: str) -> None:
    from datetime import datetime, timezone
    with _state_lock():
        s = _load_state()
        # merge（非整段重寫）→ 保留 'accepted'（agent 標記的不可渲染殘餘），免被 re-validate 清掉
        m = s.setdefault(slug, {}).setdefault('math', {})
        m.update(bad_occ=bad_occ, macros_version=macros_version,
                 at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
        if m.get('accepted'):  # 夾住 accepted ≤ 當前殘餘（源頭修復後自癒，免 total-accepted 變負→假收斂）
            m['accepted'] = min(int(m['accepted']), bad_occ)
        _save_state(s)


def math_info(slug: str, state: dict | None = None) -> dict:
    s = state if state is not None else _load_state()
    return (s.get(slug, {}) or {}).get('math') or {}


def math_accepted(slug: str, state: dict | None = None) -> int:
    """該書「已 accept、連 override 成可渲染都做不到」的殘餘 occ（真 0 政策下應極少）。"""
    s = state if state is not None else _load_state()
    return int((((s.get(slug, {}) or {}).get('math') or {}).get('accepted')) or 0)


def mark_math_accepted(slug: str, occ: int, reason: str = '') -> None:
    """agent 判定該書 occ 條殘餘源文已毀、連 override 成可渲染都做不到 → accept；不再計入
    residual_unaccepted（收斂終態）。reason 存稽核用（真 0 政策下 accept 應極少，須留證）。
    夾住 occ ≤ 該書當前殘餘——以 **report 為 ground truth**（非 state.math 快取：存量書 state 無
    bad_occ → 夾值失效 → 接受任意 occ，正是冷啟空窗坑）。report 不存在則 raise，絕不 silent 接受。"""
    from datetime import datetime, timezone
    from book_pipeline import math_validate as mv
    bad = (mv.read_report(slug) or {}).get('stats', {}).get('bad_occ')
    if bad is None:
        raise ValueError(f'{slug} 無 math report，無法 accept（先跑 math_validate）')
    with _state_lock():
        s = _load_state()
        m = s.setdefault(slug, {}).setdefault('math', {})
        m['accepted'] = min(int(occ), int(bad))  # 不超過當前殘餘（report 權威）
        m['accepted_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        if reason:
            m['accepted_reason'] = reason
        _save_state(s)


def math_accepted_total(state: dict | None = None) -> int:
    s = state if state is not None else _load_state()
    return sum(int(((v or {}).get('math') or {}).get('accepted') or 0)
               for k, v in s.items() if k != MATH_STATE_KEY and isinstance(v, dict))


def corpus_math_residual(state: dict | None = None) -> int:
    """全 corpus 殘餘總和 = 各書 _math_report.json 的 bad_occ 加總。**reports 為 ground truth**：
    state.math 是有冷啟空窗的快取（存量書部署在本功能前→state 無紀錄→被算 0），故門檻判定/顯示
    一律改讀 reports（mv.residual_by_book）。state 參數保留呼叫相容，現已不據以加總。"""
    from book_pipeline import math_validate as mv
    return sum(mv.residual_by_book().values())


def math_sweep_state(state: dict | None = None) -> dict:
    s = state if state is not None else _load_state()
    return (s.get(MATH_STATE_KEY) or {}).get('last_sweep') or {}


def mark_math_swept(macros_version: str, residual_before: int, residual_after: int,
                    touched: list[str]) -> None:
    """記錄本輪 sweep：殘餘 before→after + 改動書清單。residual_before 供 _sweep_decision 判
    fixpoint（同 macros 下沒降也沒改書 → 停派，避免 busy-loop）。"""
    from datetime import datetime, timezone
    with _state_lock():
        s = _load_state()
        s.setdefault(MATH_STATE_KEY, {})['last_sweep'] = {
            'macros_version': macros_version,
            'residual_before': residual_before, 'residual_after': residual_after,
            'touched': touched,
            'at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        _save_state(s)


def math_batch_running(state: dict | None = None) -> dict | None:
    """batch 是否正在跑（持久化：do_math_sweep 在 worker thread 設，獨立 devsnapshot 進程要讀得到）。
    回 {at, before} 或 None。crash/SIGKILL 留下的殘 flag 由下次 do_math_sweep 起頭覆蓋、結束清除。"""
    s = state if state is not None else _load_state()
    return (s.get(MATH_STATE_KEY) or {}).get('batch_running') or None


def set_math_batch_running(before: int) -> None:
    from datetime import datetime, timezone
    with _state_lock():
        s = _load_state()
        s.setdefault(MATH_STATE_KEY, {})['batch_running'] = {
            'at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'before': before}
        _save_state(s)


def clear_math_batch_running() -> None:
    with _state_lock():
        s = _load_state()
        if s.get(MATH_STATE_KEY):
            s[MATH_STATE_KEY].pop('batch_running', None)
            _save_state(s)


def math_last_batch(state: dict | None = None) -> dict | None:
    """上次 batch 結果（解了幾條/觸幾書/殘餘 before→after），供 /dev 顯示。"""
    s = state if state is not None else _load_state()
    return (s.get(MATH_STATE_KEY) or {}).get('last_batch') or None


def record_math_batch(result: dict) -> None:
    from datetime import datetime, timezone
    with _state_lock():
        s = _load_state()
        d = dict(result)
        d['at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        s.setdefault(MATH_STATE_KEY, {})['last_batch'] = d
        _save_state(s)


def _deployed(slug: str, state: dict) -> bool:
    """已部署 = textbook-reader/data/<slug>/book.json 存在（真相在 reader repo）。"""
    if os.path.exists(os.path.join(READER_ROOT, 'data', slug, 'book.json')):
        return True
    return bool(state.get(slug, {}).get('deployed_at'))


# pdf_triage.classify 對固定 PDF 是確定性的，但 pymupdf 開整本 ~1.4s/書 → 每份 build_snapshot
# 對全部 pre-ingest 書重跑一遍（21 本≈29s）是 status.json 過期（age 飆 5 分）的元兇。按
# (檔名,mtime,size) 持久快取：re-download 換檔→key 變→自動失效；多進程（devsnapshot/controller）
# 共用同檔，atomic 寫。命中即 ~0s → build_snapshot 34.7s→~6s、60s 心跳追得上。
_TRIAGE_CACHE_PATH = os.path.join(ROOT, 'book_pipeline', '.triage_cache.json')
_triage_cache: dict | None = None


def _triage(slug: str, raw: dict) -> dict | None:
    """對該 slug 的 raw PDF 跑 triage（按檔內容快取，命中免重開 PDF）。無 PDF 回 None。"""
    global _triage_cache
    fn = raw.get(slug)
    if not fn:
        return None
    path = os.path.join(ROOT, 'raw_pdfs', fn)
    if not os.path.isfile(path):
        return None
    try:
        sb = os.stat(path)
        key = f'{fn}:{int(sb.st_mtime)}:{sb.st_size}'
    except OSError:
        key = None
    if _triage_cache is None:
        try:
            _triage_cache = json.load(open(_TRIAGE_CACHE_PATH)) or {}
        except Exception:
            _triage_cache = {}
    if key and key in _triage_cache:
        return _triage_cache[key]
    # pymupdf 對病態 PDF（corrupt / 半下載 / 超大）會無限打轉、無 timeout → 曾整份 build_snapshot
    # 卡死 14min，launchd 因前一實例未退而不觸發新 60s run → status.json 凍結數分鐘（看板假象）。
    # 故隔離進子進程跑硬 timeout：超時/壞檔 → review+needs_llm（轉 qc 視覺驗證），cache 之不再重試。
    import sys
    import subprocess
    try:
        cp = subprocess.run([sys.executable, '-m', 'book_pipeline.pdf_triage', path, '--json'],
                            cwd=ROOT, capture_output=True, text=True, timeout=45)
        arr = json.loads(cp.stdout) if cp.stdout.strip() else []
        res = arr[0] if arr else {'verdict': 'review', 'needs_llm': True,
                                  'error': cp.stderr.strip()[:200] or '無輸出'}
    except subprocess.TimeoutExpired:
        res = {'verdict': 'review', 'needs_llm': True, 'type': 'unknown', 'quality': 'bad',
               'reasons': ['triage 逾時（PDF 病態/超大）→ 轉視覺驗證'], 'error': 'triage timeout'}
    except Exception as e:
        return {'verdict': 'review', 'needs_llm': True, 'error': str(e)}
    if key:
        _triage_cache[key] = res
        try:  # atomic：同目錄 temp + replace，多進程安全
            d = os.path.dirname(_TRIAGE_CACHE_PATH)
            fd, tmp = tempfile.mkstemp(dir=d, prefix='.triage_', suffix='.json')
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump(_triage_cache, fh, ensure_ascii=False)
            os.replace(tmp, _TRIAGE_CACHE_PATH)
        except Exception:
            pass
    return res


def assess_full(slug: str, pending: set, raw: dict, state: dict) -> dict:
    """回傳擴展 stage：含 triage/qc（ingest 前）與 deploy（parse 後）。"""
    has_unified = st._exists(slug, 'unified', 'content_list.json')

    # ── ingest 前：triage / qc ──
    if not has_unified:
        if slug in pending:  # 已 PUT、unified 未組 → 續 ingest
            return {'slug': slug, 'stage': '0.5 OCR處理中', 'todo': 'ingest', 'llm': False}
        tri = _triage(slug, raw)
        if tri is None:
            # 無源 = 殘留 slug 或待補；crawl 由書單 SoT 驅動（見 pipeline_tick），
            # 不從此處觸發，僅 surface。
            return {'slug': slug, 'stage': 'X 無源', 'todo': '—', 'llm': False}
        qc = state.get(slug, {}).get('qc')
        if tri.get('verdict') == 'reject' and not tri.get('needs_llm'):
            return {'slug': slug, 'stage': 'R triage拒', 'todo': '—',
                    'llm': False, 'note': '；'.join(tri.get('reasons', []))}
        if tri.get('needs_llm') and not qc:
            return {'slug': slug, 'stage': '0.2 待qc', 'todo': 'qc', 'llm': True,
                    'note': f"{tri.get('type')}/{tri.get('quality')}"}
        if qc and qc.get('verdict') == 'reject':
            return {'slug': slug, 'stage': 'R qc拒', 'todo': '—', 'llm': False,
                    'note': qc.get('note', '')}
        return {'slug': slug, 'stage': '0.3 待ingest', 'todo': 'ingest', 'llm': False,
                'note': f"{tri.get('type')}/{tri.get('quality')}"}

    # ── ingest 後：委派 status，再判 deploy ──
    base = st.assess(slug, pending, raw)
    todo = base.get('todo', '—')
    stage = base.get('stage', '')
    # 已切章節（3/4）且未部署 → deploy
    if stage.startswith(('3', '4')):
        if not _deployed(slug, state):
            # 解答/翻譯為可選；只要 parsed 就可部署，但 sol 未 merge 先提示
            non_opt = [t for t in todo.split() if t != '—' and not t.startswith('translate')]
            # catalog 已 accept（det+LLM 修完仍殘留、源頭缺不可修）→ 不再 gate deploy
            if catalog_accepted(slug, state):
                non_opt = [t for t in non_opt if not t.startswith('catalog_audit')]
            if non_opt and any(t.startswith('sol_extract') or t.startswith('catalog_audit') for t in non_opt):
                # 仍有非可選上游待辦，先做那個
                return {'slug': slug, 'stage': stage, 'todo': non_opt[0],
                        'llm': non_opt[0].startswith('audit'),
                        'prob': base.get('prob'), 'sol': base.get('sol')}
            return {'slug': slug, 'stage': stage, 'todo': 'deploy', 'llm': False,
                    'prob': base.get('prob'), 'sol': base.get('sol')}
    # ingest/audit/parse 中
    return {'slug': slug, 'stage': stage, 'todo': todo,
            'llm': any(todo.startswith(t) for t in LLM_TODOS),
            'prob': base.get('prob'), 'sol': base.get('sol')}


def build_queue() -> list[dict]:
    pending = st._load_pending()
    raw = st._raw_slug_map()
    state = _load_state()
    slugs = st.all_slugs(pending, raw)
    return [assess_full(s, pending, raw, state) for s in slugs]


def assess_one(slug: str) -> dict:
    """單本即時 stage 判定（縱向推進每步後重算用）。每次重載 pending/raw/state，
    因為 ingest/parse/audit 會改變磁碟狀態 → 下一步判定須看最新真相。"""
    pending = st._load_pending()
    raw = st._raw_slug_map()
    state = _load_state()
    return assess_full(slug, pending, raw, state)


def next_actionable(rows: list[dict]) -> dict | None:
    """pipeline 上游優先：依 stage 前綴排序，回第一個有 todo 的（非拒絕/done）。"""
    order = {'0.2': 0, '0.3': 1, '0.5': 1, '1': 2, '2': 3, '3': 4, '4': 4}
    actionable = [r for r in rows if r['todo'] not in ('—', '') and not r['stage'].startswith('R')]
    if not actionable:
        return None
    def key(r):
        pre = r['stage'].split()[0]
        return order.get(pre, 9)
    return sorted(actionable, key=key)[0]


def main() -> int:
    ap = argparse.ArgumentParser(description='跨書全 stage 單一真相')
    ap.add_argument('--next', action='store_true', help='只印下一個可動項')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    rows = build_queue()
    if args.next:
        nx = next_actionable(rows)
        if args.json:
            print(json.dumps(nx, ensure_ascii=False))
        elif nx:
            tag = '[LLM]' if nx['llm'] else '[det]'
            print(f"{nx['slug']}  →  {nx['todo']} {tag}  ({nx['stage']})")
            if nx.get('note'):
                print(f"  {nx['note']}")
        else:
            print('無可動項（全部 done 或待人工/外部）')
        return 0

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(f"{'slug':24} {'階段':<14} {'todo':<22} kind  note")
    for r in sorted(rows, key=lambda r: r['stage']):
        tag = 'LLM' if r['llm'] else ('det' if r['todo'] not in ('—', '') else '')
        note = r.get('note', '') or (f"{r.get('sol','')}/{r.get('prob','')}" if r.get('prob') else '')
        print(f"{r['slug']:24} {r['stage']:<14} {r['todo']:<22} {tag:>4}  {note}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
