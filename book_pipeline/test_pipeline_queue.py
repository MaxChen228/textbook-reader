"""book_pipeline.pipeline_queue 單測：assess_full / next_actionable（daemon work-queue 核心推導）。

跑：uv run python -m book_pipeline.test_pipeline_queue

這兩個純函式是 daemon「每 tick 派哪本書哪個 stage」的真相。判錯的後果都是不可逆的損害：
  · qc reject 漏 gate → 燒 MinerU 付費 OCR 把糊書 ingest 進來（silent-corruption）
  · 被 SIGKILL 截斷沒寫回 qc 卻 fall-through 成「待ingest」→ 未經視覺驗證直接 ingest（wrong-output）
  · deploy gate 三態判錯 → 殘缺書上正式站 books.wordnexus.lol，或書永遠卡著 deploy 不了
  · next_actionable 排序錯 → 上游書餓死（永遠先做下游 deploy，上游永不被選）

設計上易 hermetic：state 用 dict 直接構造、_triage 用 stub 取代（不碰真實 PDF / pdf_triage），
deploy-gate 測試把 st.assess monkeypatch 成構造好的 base dict（避開 _catalog_critical 跑 audit_catalog），
並把 has_unified 的判據（st.DATA 下的 unified/content_list.json）與 _deployed 判據（READER_ROOT/data）
重導到 tmp。所有重導 finally 還原，絕不污染真實 store / 真實狀態檔。
"""
from __future__ import annotations

import os
import tempfile

from book_pipeline import pipeline_queue as pq
from book_pipeline import status as st


# ── 共用 hermetic 工具 ────────────────────────────────────────────────────────

def _mk_unified(data_root: str, slug: str) -> None:
    """在 tmp st.DATA 下造出 unified/content_list.json，使 assess_full 認定 has_unified=True
    （越過 ingest 前的 triage/qc 分支，進到委派 st.assess + deploy gate 的路徑）。"""
    d = os.path.join(data_root, slug, 'unified')
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'content_list.json'), 'w') as f:
        f.write('[]')


# ══ 1. qc verdict gate：reject 短路、pass 放行 ════════════════════════════════

def test_assess_full_qc_reject_short_circuits():
    """qc verdict gate（gate 住付費 OCR 的最關鍵 invariant）：

      · 已 triage（needs_llm=True）且 state qc verdict=='reject' → stage 'R qc拒'、todo '—'。
        絕不放行 ingest——一旦放行就是花 MinerU 預算把人工判定為糊的書 OCR 進來，
        而且結果還會被當成有效書進到下游。'R' 前綴讓 next_actionable 也把它排除。
      · 對照組 verdict=='pass' → '0.3 待ingest'、todo 'ingest'：通過視覺關卡就該放行。
    """
    with tempfile.TemporaryDirectory() as data_root:
        orig_data = st.DATA
        st.DATA = data_root  # has_unified=False（tmp 下沒造 unified）→ 走 triage/qc 分支
        # 直接 stub pipeline_queue._triage：回「需 LLM 視覺驗證」（如掃描 PDF），不碰真實 PDF。
        orig_triage = pq._triage
        pq._triage = lambda slug, raw: {'verdict': 'review', 'needs_llm': True,
                                        'type': 'scan', 'quality': 'low'}
        try:
            slug = 'probe_book'
            raw = {slug: 'probe.pdf'}  # 非空 → 不會落入 'X 無源'（_triage 已 stub，不實際讀）

            # (a) reject → 短路，不放行 ingest
            reject_state = {slug: {'qc': {'verdict': 'reject', 'note': '掃描歪斜不可用'}}}
            r = pq.assess_full(slug, set(), raw, reject_state)
            assert r['stage'] == 'R qc拒', f"qc reject 應回 'R qc拒'，實得 {r['stage']!r}"
            assert r['todo'] == '—', f"qc reject 絕不可放行任何 todo，實得 {r['todo']!r}"
            assert r['llm'] is False
            assert not r['stage'].startswith('0.3'), "reject 絕不可 fall-through 成待ingest"

            # (b) pass → 放行 ingest（對照組，證明 gate 不是「永遠擋」）
            pass_state = {slug: {'qc': {'verdict': 'pass', 'note': ''}}}
            r2 = pq.assess_full(slug, set(), raw, pass_state)
            assert r2['stage'] == '0.3 待ingest', f"qc pass 應放行待ingest，實得 {r2['stage']!r}"
            assert r2['todo'] == 'ingest', f"qc pass 應 todo=ingest，實得 {r2['todo']!r}"
        finally:
            st.DATA = orig_data
            pq._triage = orig_triage


# ══ 2. 缺 qc（SIGKILL 截斷）→ 必重派 qc，絕不靜默跳過視覺關卡 ══════════════════

def test_assess_full_missing_qc_reprompts():
    """qc agent 被 SIGKILL 截斷沒寫回 state（常態）時的冪等安全：

      triage 判 needs_llm=True，但 state 無此 slug 的 'qc' key → 必須回 '0.2 待qc'、todo 'qc'，
      讓 daemon 重派視覺驗證。**絕不可** fall-through 成 '0.3 待ingest' 直接 ingest——
      那等於跳過視覺關卡把未驗書送進付費 OCR。invariant：needs_llm 而無 qc 紀錄 ⇒ 永遠先補 qc。
    """
    with tempfile.TemporaryDirectory() as data_root:
        orig_data = st.DATA
        st.DATA = data_root
        orig_triage = pq._triage
        pq._triage = lambda slug, raw: {'verdict': 'review', 'needs_llm': True,
                                        'type': 'scan', 'quality': 'mid'}
        try:
            slug = 'truncated_book'
            raw = {slug: 'truncated.pdf'}
            state = {}  # agent 被截斷：state 完全沒此 slug（更嚴格的「無 qc」情境）

            r = pq.assess_full(slug, set(), raw, state)
            assert r['stage'] == '0.2 待qc', f"缺 qc 應回 '0.2 待qc' 重派，實得 {r['stage']!r}"
            assert r['todo'] == 'qc', f"缺 qc 應 todo=qc，實得 {r['todo']!r}"
            assert r['llm'] is True, "qc 是 LLM 工序，llm 旗標必為 True"
            assert r['stage'] != '0.3 待ingest', "缺 qc 絕不可靜默 fall-through 成待ingest"

            # 加一個更刁鑽情境：slug 在 state 但 'qc' 鍵不存在（只寫了別的標記）
            state2 = {slug: {'deployed_at': None}}  # 有 slug 紀錄但無 qc
            r2 = pq.assess_full(slug, set(), raw, state2)
            assert r2['stage'] == '0.2 待qc', "slug 有紀錄但缺 qc 鍵，仍須重派 qc"
            assert r2['todo'] == 'qc'
        finally:
            st.DATA = orig_data
            pq._triage = orig_triage


# ══ 3. deploy gate 三態：catalog_audit 待辦 / catalog_accepted / sol_extract 待 merge ══

def test_assess_full_deploy_gate_catalog_and_sol():
    """deploy gate 是 daemon「是否讓殘缺書上正式站」的核心邏輯。書已 parsed（stage 3/4）、未 deployed 時：

      (a) 有非可選 catalog_audit 待辦（未 accept）→ todo=catalog_audit，**先修上游、不 deploy**
          （catalogs 缺章節對映就上站＝殘缺書服務給讀者）。
      (b) state 有 catalog_accepted 標記（det+LLM 修完仍殘、MinerU 源頭缺不可修）
          → catalog_audit 從 gate 移除 → 放行 deploy。這是「終局 accept」逃生閥，
          否則源頭缺的書會永遠卡著 deploy 不了。
      (c) 只有 sol（sol_ingest/sol_extract）待辦、無 catalog → **放行 deploy**（option B：解答本綁母書、
          異步晚到，**不擋母書首次上站**；母書秒上站後，sol 作為 post-deploy 非可選階段在背景補 merge、
          重烤即時生效。避免「注定 merge 不上的爛解答本反過來卡死好母書上站」）。
      (f) 已部署母書仍有非可選 sol 待辦 → assess_full 落 fallthrough、原樣回傳 sol todo（llm=True）→
          advance 撿來補 merge。這是「晚到/漏做的解答本喚醒已部署母書」的關鍵通路。

    為避免真實 _catalog_critical 跑昂貴的 audit_catalog，直接 monkeypatch st.assess 回構造好的 base。
    """
    with tempfile.TemporaryDirectory() as data_root, tempfile.TemporaryDirectory() as reader_root:
        orig_data, orig_reader, orig_assess = st.DATA, pq.READER_ROOT, st.assess
        st.DATA = data_root
        pq.READER_ROOT = reader_root  # _deployed 看 reader_root/data/<slug>/book.json（tmp 下無 → 未部署）
        try:
            slug = 'parsed_book'
            _mk_unified(data_root, slug)  # has_unified=True → 進到委派 st.assess + deploy gate 路徑

            # (a) catalog_audit 非可選、未 accept → 先做上游、不 deploy
            st.assess = lambda s, p, r: {
                'slug': s, 'stage': '3 parsed', 'todo': 'catalog_audit(5)',
                'prob': 10, 'sol': 0, 'sol_book': False}
            ra = pq.assess_full(slug, set(), {}, {})  # state 無 catalog_accepted
            assert ra['stage'].startswith('3'), f"應維持 parsed stage，實得 {ra['stage']!r}"
            assert ra['todo'] == 'catalog_audit(5)', \
                f"未 accept 的 catalog_audit 必先做、不 deploy，實得 todo={ra['todo']!r}"
            assert ra['todo'] != 'deploy', "殘缺 catalog 絕不可放行 deploy"

            # (b) state 標記 catalog_accepted → catalog_audit 移除 → 放行 deploy
            accepted_state = {slug: {'catalog_accepted': {'residual': 5, 'at': 'x'}}}
            rb = pq.assess_full(slug, set(), {}, accepted_state)
            assert rb['todo'] == 'deploy', \
                f"catalog_accepted 後應放行 deploy，實得 todo={rb['todo']!r}"
            assert rb['stage'].startswith('3')

            # (c) 只有 sol（sol_ingest/sol_extract）、無 catalog → 放行 deploy（sol 不擋首次上站，option B）
            for sol_todo in ('sol_extract(parsed_book_sol)', 'sol_ingest(parsed_book_sol)'):
                st.assess = lambda s, p, r, _t=sol_todo: {
                    'slug': s, 'stage': '3 parsed', 'todo': _t,
                    'prob': 10, 'sol': 0, 'sol_book': True}
                rc = pq.assess_full(slug, set(), {}, {})  # 未 deployed
                assert rc['todo'] == 'deploy', \
                    f"sol 不擋母書首次上站（背景補 merge）：todo={_t!r} 應放行 deploy，實得 {rc['todo']!r}"

            # (d) 對照：純可選待辦（translate）→ 不 gate，放行 deploy（證明 gate 只擋 catalog/sol）
            st.assess = lambda s, p, r: {
                'slug': s, 'stage': '3 parsed', 'todo': 'translate(可選)',
                'prob': 10, 'sol': 0, 'sol_book': False}
            rd = pq.assess_full(slug, set(), {}, {})
            assert rd['todo'] == 'deploy', \
                f"只剩可選 translate 應放行 deploy，實得 todo={rd['todo']!r}"

            # (e) catalog_audit 與可選 translate 並存、且 translate **排在前面**：
            #     translate 必須先被 filter 掉，non_opt[0] 才會是真正該做的 catalog_audit。
            #     若漏掉「排除 translate」這條 filter，non_opt[0] 會錯成 translate(可選)，
            #     daemon 就會去派一個可選翻譯、把該擋的殘缺 catalog gate 放掉 → 殘書上站。
            #     （case (d) 只證明 translate 不觸發 gate；唯有本例能證明 translate 真的被剔出
            #      非可選清單、不會頂替掉同列的強制 gate 待辦——直接守住 non_opt 的純度。）
            st.assess = lambda s, p, r: {
                'slug': s, 'stage': '3 parsed', 'todo': 'translate(可選) catalog_audit(5)',
                'prob': 10, 'sol': 0, 'sol_book': False}
            re_ = pq.assess_full(slug, set(), {}, {})
            assert re_['todo'] == 'catalog_audit(5)', \
                f"translate 須被剔出非可選清單，gate 待辦不可被 translate 頂替，實得 todo={re_['todo']!r}"
            assert not re_['todo'].startswith('translate'), \
                "絕不可把可選 translate 當成該先做的強制待辦回傳"

            # (f) 已部署母書仍有非可選 sol_extract → fallthrough 原樣回傳（llm=True），不被 deploy gate 吞掉。
            #     這是「晚到/漏做的解答本喚醒已部署母書補 merge」的關鍵通路（state.deployed_at → _deployed True）。
            st.assess = lambda s, p, r: {
                'slug': s, 'stage': '3 parsed', 'todo': 'sol_extract(parsed_book_sol)',
                'prob': 10, 'sol': 0, 'sol_book': True}
            rf = pq.assess_full(slug, set(), {}, {slug: {'deployed_at': 'x'}})
            assert rf['todo'] == 'sol_extract(parsed_book_sol)', \
                f"已部署母書的非可選 sol todo 須原樣回傳供 advance 補 merge，實得 todo={rf['todo']!r}"
            assert rf['llm'] is True, "sol_extract 屬 LLM_TODOS → llm 旗標須為 True"
        finally:
            st.DATA, pq.READER_ROOT, st.assess = orig_data, orig_reader, orig_assess


# ══ 4. next_actionable：上游優先排序、R 前綴/'—' 跳過、未知前綴 fallback、全不可動回 None ══

def test_next_actionable_upstream_priority_and_skip():
    """next_actionable 是純確定性排序，守 daemon「先推上游、不讓上游書餓死」的 invariant：

      · order dict 用 stage 前綴排上游優先：'0.2'<'0.3'<…<'3'/'4'。給一堆 row，
        應選 '0.2 待qc'（最上游）而非 '3 parsed' 的 deploy——否則永遠先做下游、上游餓死。
      · 'R' 前綴（triage/qc 拒）整列跳過；todo=='—' 的列跳過（無可動工序）。
      · 未知 stage 前綴 fallback=9 → 排最後（怪 stage 不會插隊搶到上游位置）。
      · 全部不可動（todo 皆 '—' 或全 R）→ 回 None。
    """
    rows = [
        {'stage': '3 parsed', 'todo': 'deploy'},        # 下游：deploy
        {'stage': '0.2 待qc', 'todo': 'qc'},            # 最上游：應雀屏中選
        {'stage': 'R qc拒', 'todo': '—'},               # R 前綴 → 跳過
        {'stage': '4 sol已merge', 'todo': '—'},         # todo '—' → 跳過
        {'stage': '9.9 怪', 'todo': 'x'},               # 未知前綴 → fallback 9 排最後
    ]
    nx = pq.next_actionable(rows)
    assert nx is not None
    assert nx['stage'] == '0.2 待qc', \
        f"應選最上游 '0.2 待qc'（防上游餓死），實得 {nx['stage']!r}"

    # 證明未知前綴排最後：把 '0.2' 拿掉後，'3 parsed'(order 4) 仍勝過 '9.9 怪'(fallback 9)
    rows2 = [r for r in rows if r['stage'] != '0.2 待qc']
    nx2 = pq.next_actionable(rows2)
    assert nx2['stage'] == '3 parsed', \
        f"無上游時應選 order 較小的 '3 parsed' 而非 fallback 9 的怪 stage，實得 {nx2['stage']!r}"

    # 證明 R 前綴 + todo '—' 真的被排除：只剩這兩種 → None
    only_dead = [
        {'stage': 'R qc拒', 'todo': '—'},
        {'stage': 'R triage拒', 'todo': '—'},
        {'stage': '4 sol已merge', 'todo': '—'},
        {'stage': 'X 無源', 'todo': '—'},
    ]
    assert pq.next_actionable(only_dead) is None, "全不可動（R/'—'）應回 None"

    # 刁鑽：R 前綴即使 todo 非 '—' 也必須跳過（拒絕的書不該被任何排序撈回來動）。
    # 關鍵設計：R 列的 order key = order.get('R',9) = fallback 9，是所有真實 stage 的最差值；
    # 它對任何「order < 9」的競爭者（如 '3 parsed' → 4）本來就會在排序上輸 → 那種對照無法
    # 區分「真被 R-filter 排除」與「只是排序輸了」。故這裡刻意讓 R 列與一個同樣 fallback 9 的
    # 未知前綴列「打平」，且把 R 列放在輸入**第一個**：sorted 穩定排序會保留輸入序，
    # 若 R-filter 失效，打平時 R 列就會被選中 → 唯有真正的 R 前綴排除條款能讓未知列勝出。
    r_first_tie = [
        {'stage': 'R qc拒', 'todo': 'x'},   # 異常：拒絕卻有 todo，且排在最前、order 與下列打平
        {'stage': '9.9 怪', 'todo': 'y'},   # 未知前綴 → fallback 9，與 R 列同 key
    ]
    assert pq.next_actionable(r_first_tie)['stage'] == '9.9 怪', \
        "R 前綴必須被明確排除：即使排在最前、order 與他列打平、且自身有 todo，也絕不可被選"

    # 補一個直觀對照：R 列與下游 deploy 並存仍選 deploy（不靠打平、單純驗 R 不被撈回）
    r_with_todo = [
        {'stage': 'R qc拒', 'todo': 'x'},
        {'stage': '3 parsed', 'todo': 'deploy'},
    ]
    assert pq.next_actionable(r_with_todo)['stage'] == '3 parsed', \
        "R 前綴整列跳過，即使該列有 todo 也不可被選"

    assert pq.next_actionable([]) is None, "空 rows 回 None"


if __name__ == '__main__':
    test_assess_full_qc_reject_short_circuits()
    print('✓ qc verdict gate：reject 短路擋付費 OCR、pass 放行 ingest')
    test_assess_full_missing_qc_reprompts()
    print('✓ 缺 qc（SIGKILL 截斷）必重派 qc、絕不靜默跳過視覺關卡直接 ingest')
    test_assess_full_deploy_gate_catalog_and_sol()
    print('✓ deploy gate 三態：catalog_audit gate / catalog_accepted 放行 / sol_extract 先 merge')
    test_next_actionable_upstream_priority_and_skip()
    print('✓ next_actionable：上游優先、R/—跳過、未知前綴排尾、全不可動回 None')
    print('\n全部通過 ✅')
