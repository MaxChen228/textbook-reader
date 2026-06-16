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
import os
import sys
from contextlib import contextmanager

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
    try:
        return json.load(open(STATE_PATH)) or {}
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    json.dump(s, open(STATE_PATH, 'w'), ensure_ascii=False, indent=2)


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


def _deployed(slug: str, state: dict) -> bool:
    """已部署 = textbook-reader/data/<slug>/book.json 存在（真相在 reader repo）。"""
    if os.path.exists(os.path.join(READER_ROOT, 'data', slug, 'book.json')):
        return True
    return bool(state.get(slug, {}).get('deployed_at'))


def _triage(slug: str, raw: dict) -> dict | None:
    """對該 slug 的 raw PDF 跑 triage（廉價）。無 PDF 回 None。"""
    fn = raw.get(slug)
    if not fn:
        return None
    path = os.path.join(ROOT, 'raw_pdfs', fn)
    if not os.path.isfile(path):
        return None
    try:
        from book_pipeline import pdf_triage
        return pdf_triage.classify(path)
    except Exception as e:
        return {'verdict': 'review', 'needs_llm': True, 'error': str(e)}


def assess_full(slug: str, pending: set, raw: dict, state: dict) -> dict:
    """回傳擴展 stage：含 triage/qc（ingest 前）與 deploy（parse 後）。"""
    has_unified = st._exists(slug, 'unified', 'content_list.json')

    # ── ingest 前：triage / qc ──
    if not has_unified:
        if slug in pending:  # 已 PUT、unified 未組 → 續 ingest
            return {'slug': slug, 'stage': '0.5 ingest中斷', 'todo': 'ingest', 'llm': False}
        tri = _triage(slug, raw)
        if tri is None:
            # 無源 = 殘留 slug 或待補；crawl 由 wishlist 驅動（見 pipeline_tick），
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
