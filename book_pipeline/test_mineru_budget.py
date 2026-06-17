"""mineru_budget 單元測試（無外部依賴）：uv run python -m book_pipeline.test_mineru_budget

守住 daemon「async submit / harvest 排程」最核心的契約邊界——`occupied` / `harvestable`
決定哪些書能重提 OCR。判錯的後果是不可逆的資源浪費或卡死：
  - 把 stale 的崩潰殘留誤判為「仍佔用」→ 那本書永遠不重提，卡死在半上傳狀態。
  - 把上傳中（uploading=True）誤收為 harvestable → 收割未完成 batch，組出殘缺 unified。
  - 把壞時戳誤判為「新鮮」→ 意外擋住本該自癒的重提。

故本檔釘住四個時效/集合判斷的 invariant：
  occupied 的 stale-upload 自癒、harvestable 只收就緒、_age_secs 壞時戳 fail-safe、
  以及 record_start 的 RMW 帳本不重複加頁。

hermetic：redirect 模組全域 PENDING_PATH / BUDGET_PATH / STALE_UPLOAD_SECS 到 tmp/固定值，
finally 還原；絕不碰真實 _pending_batches.json / mineru_budget.json。
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from book_pipeline import mineru_budget as mb


def _iso(dt: datetime) -> str:
    """寫成 _age_secs 解析的格式 '%Y-%m-%dT%H:%M:%SZ'（UTC、無微秒、Z 結尾）。"""
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_pending(entries: list) -> str:
    """寫一份 tmp _pending_batches.json，回路徑（供 redirect mb.PENDING_PATH）。"""
    d = tempfile.mkdtemp(prefix='mineru_budget_pending_')
    p = os.path.join(d, '_pending_batches.json')
    with open(p, 'w') as f:
        json.dump(entries, f)
    return p


# ── occupied：崩潰殘留 stale-upload 自癒的關鍵時效判斷 ──────────────────────────
def test_occupied_stale_upload_self_heals():
    """occupied = 不該重提的 slug。fixture 涵蓋全部分支，且**夾住 STALE_UPLOAD_SECS 邊界**
    （非鬆散地放遠 600s）——這樣若 cutoff 常數被改/比較被反轉，邊界兩側其一必翻紅：
      - 新鮮 uploading（submitted_at=now，uploading=True）→ 佔用（正在傳，別重複提）。
      - just-fresh：age 比 STALE 小 120s（`< STALE` 為真）→ 仍佔用（邊界內側）。
      - just-stale：age 比 STALE 大 120s（`< STALE` 為假）→ **不**佔用（邊界外側，崩潰殘留
        須能重提覆寫自癒，否則永卡半上傳）。120s 容差吸收測試執行漂移、又遠小於若把
        判斷誤寫成「跨日/×60」等量級錯誤所需的距離 → 仍能抓常數型 mutation。
      - ready（uploading falsy）→ 佔用（已就緒，等 harvest，重提會浪費）。
      - ready-implicit：完全缺 `uploading` key → `e.get('uploading')` 回 None（falsy）→ 必須
        走 else 視為就緒→佔用（釘住「缺 key == 就緒」而非「缺 key == 跳過」）。
      - 缺 slug → 整筆跳過（沒 slug 無從定址，不該污染集合）。
      - 缺 submitted_at 的 uploading → _age_secs 回 inf → inf < STALE 為假 → 不佔用
        （保守：壞/缺時戳不該意外擋住重提）。
    """
    now = _now()
    S = mb.STALE_UPLOAD_SECS
    entries = [
        {'slug': 'fresh-up', 'uploading': True, 'submitted_at': _iso(now)},
        {'slug': 'just-fresh', 'uploading': True,
         'submitted_at': _iso(now - timedelta(seconds=S - 120))},      # 邊界內側 → 佔用
        {'slug': 'just-stale', 'uploading': True,
         'submitted_at': _iso(now - timedelta(seconds=S + 120))},      # 邊界外側 → 排除
        {'slug': 'ready-book', 'uploading': False, 'submitted_at': _iso(now)},
        {'slug': 'ready-implicit', 'submitted_at': _iso(now)},         # 缺 uploading key
        {'uploading': True, 'submitted_at': _iso(now)},                # 缺 slug
        {'slug': 'noting-up', 'uploading': True},                      # 缺 submitted_at
    ]
    orig = mb.PENDING_PATH
    mb.PENDING_PATH = _write_pending(entries)
    try:
        occ = mb.occupied()
    finally:
        mb.PENDING_PATH = orig

    assert 'fresh-up' in occ, '新鮮 uploading 必佔用，否則同窗重複提交燒配額'
    assert 'just-fresh' in occ, 'age=STALE-120s（內側）必佔用，否則 cutoff 被縮小/比較反轉'
    assert 'just-stale' not in occ, 'age=STALE+120s（外側）須排除→自癒；否則 cutoff 被放大'
    assert 'ready-book' in occ, '就緒（uploading=False）必佔用，等 harvest'
    assert 'ready-implicit' in occ, '缺 uploading key→falsy→視為就緒→佔用（非跳過）'
    assert 'noting-up' not in occ, '缺 submitted_at→inf→stale→不佔用（保守可重提）'
    # 全等集合：一次釘死「恰這四本、缺 slug 整筆跳過、stale/缺時戳皆不混入」。
    assert occ == {'fresh-up', 'just-fresh', 'ready-book', 'ready-implicit'}, \
        '缺 slug 整筆跳過，集合恰為這四本（內側 uploading + 兩本就緒）'
    print('✓ occupied：STALE 邊界夾擊 + 缺 uploading 視為就緒 + 缺時戳保守 + 缺 slug 跳過')


# ── harvestable：只收就緒，別收割未完成 batch ────────────────────────────────
def test_harvestable_only_uploading_falsy():
    """harvestable = uploading falsy（上傳已完整）的 slug。uploading=True 的書 OCR 還沒
    傳完，若被收割會組出殘缺 unified（少頁/缺圖），是靜默資料損毀，故必須排除。
    缺 slug 同樣跳過。"""
    now = _now()
    entries = [
        {'slug': 'ready-1', 'uploading': False, 'submitted_at': _iso(now)},
        {'slug': 'ready-2', 'uploading': False},                       # 無時戳但已就緒也算
        {'slug': 'ready-implicit', 'submitted_at': _iso(now)},         # 缺 uploading key→falsy→可收
        {'slug': 'still-up', 'uploading': True, 'submitted_at': _iso(now)},
        {'slug': 'stale-up', 'uploading': True,                        # 即使 stale 仍非就緒
         'submitted_at': _iso(now - timedelta(seconds=mb.STALE_UPLOAD_SECS + 600))},
        {'uploading': False, 'submitted_at': _iso(now)},               # 缺 slug
    ]
    orig = mb.PENDING_PATH
    mb.PENDING_PATH = _write_pending(entries)
    try:
        harv = mb.harvestable()
    finally:
        mb.PENDING_PATH = orig

    assert harv == {'ready-1', 'ready-2', 'ready-implicit'}, 'harvestable 恰為就緒三本'
    assert 'ready-implicit' in harv, '缺 uploading key→falsy→視為就緒→可收割'
    assert 'still-up' not in harv, 'uploading=True→OCR 未完整→不可收割（防殘缺 unified）'
    assert 'stale-up' not in harv, 'harvestable 只看 uploading，stale 與否不影響（仍未就緒）'
    print('✓ harvestable：只收 uploading falsy（含缺 key）+ 缺 slug 跳過（不收割未完成 batch）')


# ── _age_secs：壞時戳保守 fail-safe（不擋重提）────────────────────────────────
def test_age_secs_bad_timestamp_returns_inf():
    """occupied/harvestable 的 stale 判斷都靠 _age_secs。壞/缺時戳必須回 inf（無窮老舊）
    →被視為 stale→**不**擋重提，這是保守 fail-safe：寧可重提（最多多燒一次），也不要因
    一個無法解析的時戳把書永遠卡在「佔用」。合法 ISO 則回正確秒數。"""
    assert mb._age_secs({}) == float('inf'), '缺 submitted_at → inf'
    assert mb._age_secs({'submitted_at': ''}) == float('inf'), '空字串時戳 → inf'
    assert mb._age_secs({'submitted_at': 'garbage'}) == float('inf'), '壞時戳 → inf'
    # 合法時戳：120 秒前 → age ≈ 120（給 ±5s 容差吸收測試執行時間漂移）。
    ts = _iso(_now() - timedelta(seconds=120))
    age = mb._age_secs({'submitted_at': ts})
    assert 115 <= age <= 125, f'合法時戳應回約 120 秒，得 {age}'
    print('✓ _age_secs：缺/空/壞時戳→inf（保守不擋重提）；合法→正確秒數')


# ── pick_account + record_start：負載均衡 + RMW 帳本不重複加頁 ──────────────────
def _redirect_budget():
    """redirect mb.BUDGET_PATH 到 tmp，回 (path, restore_fn)。"""
    d = tempfile.mkdtemp(prefix='mineru_budget_')
    p = os.path.join(d, 'mineru_budget.json')
    orig = mb.BUDGET_PATH
    mb.BUDGET_PATH = p

    def restore():
        mb.BUDGET_PATH = orig
    return p, restore


def test_pick_account_and_record_start_rmw():
    """pick_account 挑今日 used 最少帳號；空/毀損 budget 仍回合法帳號（不崩、不回 None，
    因 MinerU 無硬 cap）。record_start 是 RMW（_load→改→_save），同 slug 重複呼叫須被
    `if slug not in ent['books']` 守衛擋住——否則 pages 會被重複加，帳本（dashboard 顯示
    今日已送頁數）失準。"""
    today = mb._today()
    path, restore = _redirect_budget()
    try:
        # (a) pick_account 挑 used 最少。兩個方向都測，證明它真讀了 used 計數、而非碰巧
        # 固定回某一個帳號：TOKEN=500/TOKEN2=100→挑 TOKEN2；翻轉計數→挑 TOKEN。
        with open(path, 'w') as f:
            json.dump({today: {'MINERU_API_TOKEN': {'pages': 500},
                               'MINERU_API_TOKEN2': {'pages': 100}}}, f)
        assert mb.pick_account() == 'MINERU_API_TOKEN2', 'TOKEN2 較少→挑 TOKEN2'
        with open(path, 'w') as f:
            json.dump({today: {'MINERU_API_TOKEN': {'pages': 100},
                               'MINERU_API_TOKEN2': {'pages': 500}}}, f)
        assert mb.pick_account() == 'MINERU_API_TOKEN', 'TOKEN 較少→挑 TOKEN（證明真讀計數）'

        # (b) 空 budget {}：兩帳號皆 0 → min 取第一個 ACCOUNTS（穩定回合法帳號，不崩/不 None）。
        with open(path, 'w') as f:
            json.dump({}, f)
        picked = mb.pick_account()
        assert picked in mb.ACCOUNTS, '空 budget 仍回合法帳號'
        assert picked == mb.ACCOUNTS[0], '皆 0 → min 取第一個帳號（穩定）'

        # (c) 毀損 budget：_load 走 jsonio 容錯讀→回 {}→等同空，仍回合法帳號不崩。
        # 釘 ==ACCOUNTS[0]（非鬆散 in ACCOUNTS）：毀損須等同空 budget（全 0→min 取第一個），
        # 若容錯讀回非 {} 或挑錯帳號，`in ACCOUNTS` 會放水、`==` 才抓得到。
        with open(path, 'w') as f:
            f.write('{broken json,,,')
        assert mb.pick_account() == mb.ACCOUNTS[0], '毀損 budget→等同空→回第一個帳號（不崩）'

        # (d) record_start RMW 去重守衛：同 slug 第二次故意帶**不同**頁數（999），若守衛失效會
        # 把 999 加進去→pages=1049。斷言仍為 50 才證明 `if slug not in books` 真擋住了
        # `pages +=`（用相同 50 無法區分「擋住」與「巧合相等」，故必須用不同值）。
        with open(path, 'w') as f:
            json.dump({}, f)
        mb.record_start('book-x', 'MINERU_API_TOKEN', 50)
        mb.record_start('book-x', 'MINERU_API_TOKEN', 999)  # 重複 slug、不同頁數——須被擋
        ent = mb._load()[today]['MINERU_API_TOKEN']
        assert ent['pages'] == 50, f'同 slug 重複呼叫不重複加頁（即使頁數不同），期望 50 得 {ent["pages"]}'
        assert ent['books'] == ['book-x'], 'books 去重，不該出現兩筆 book-x'

        # (e) 不同 slug 累加正確：再記一本 30 頁 → 共 80。
        mb.record_start('book-y', 'MINERU_API_TOKEN', 30)
        ent = mb._load()[today]['MINERU_API_TOKEN']
        assert ent['pages'] == 80, f'不同 slug 應累加，期望 80 得 {ent["pages"]}'
        assert ent['books'] == ['book-x', 'book-y'], 'books 依序兩本'
    finally:
        restore()
    print('✓ pick_account：used 最少/空/毀損皆回合法帳號；record_start RMW 不重複加頁')


if __name__ == '__main__':
    test_occupied_stale_upload_self_heals()
    test_harvestable_only_uploading_falsy()
    test_age_secs_bad_timestamp_returns_inf()
    test_pick_account_and_record_start_rmw()
    print('\n全部通過 ✅')
