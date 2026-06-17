"""book_pipeline.status 單測：assess（daemon frontier 的 stage 判定矩陣）+ sol_stats。

跑：uv run python -m book_pipeline.test_status

status.assess 是 pipeline dashboard 的單一真相源——它「讀實際磁碟狀態」決定每本書
卡在哪個 stage、daemon 該派哪個 LLM 工。分支極多（待ingest/audit/parse/parsed/sol-merged
× catalog/sol/zh 可選 todo 拼接），判錯 = 派錯昂貴 LLM 工或卡死。本檔釘住整個轉移矩陣。

兩條最高含金量的 invariant（engineer-away-operational-anxiety 的具體守衛）：
  1. catalog_audit / sol_extract 在「上站前」是強制 gate（todo 無『(可選)』後綴 → main() 列入待辦
     → daemon 派工修到可接受才服務）；「已上站 / 已 accept」後降『(可選)』。若這條判反了，
     一本已部署但殘留修不掉的書會讓 reactive advance loop 每輪重派昂貴 LLM、永不 idle、燒錢。
  2. sol_stats 對「截斷壞檔」try/except 跳過、不連坐其他章（壞檔頂多靜默低報，不該讓整本歸零崩潰）；
     problem 的 solution 三態（缺鍵/空 list/None = 無解答；非空 list = 有）用 truthy 判斷。

hermetic：每個 test 用 tmp 構造 mineru_data/<slug> 目錄樹，重導 st.DATA / st.ROOT 到 tmp，
並 monkeypatch _catalog_critical / _math_residual 隔離重活（catalog_audit 全文 regex、math report 讀檔）。
finally 一律還原全域，絕不污染真實 mineru_data / pipeline_state.json。
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile

from book_pipeline import status as st


# ── tmp 沙盒：重導 st.ROOT + st.DATA + 隔離重活 helper ──────────────────────────────
# assess/sol_stats/_deployed/_catalog_accepted 全靠模組全域 ROOT、DATA 定位磁碟。
# 重導這兩者即可把整個判定搬進 tmp。_catalog_critical / _math_residual 是 spec 指定要
# monkeypatch 的重活（前者跑 audit_catalog 全文 regex + 逐圖存在檢查；後者讀 _math_report.json），
# 在此用 lambda 釘成確定值，讓測試只聚焦「stage/todo 拼接邏輯」本身。
@contextlib.contextmanager
def _sandbox(catalog_critical: int = 0, math_residual=None):
    d = tempfile.mkdtemp(prefix='status_test_')
    data = os.path.join(d, 'book_pipeline', 'mineru_data')
    os.makedirs(data, exist_ok=True)
    saved = (st.ROOT, st.DATA, st._catalog_critical, st._math_residual, st._crit_cache)
    st.ROOT = d
    st.DATA = data
    st._catalog_critical = lambda slug: catalog_critical
    st._math_residual = lambda slug: math_residual
    st._crit_cache = {}  # 避免污染到真實快取
    try:
        yield d, data
    finally:
        (st.ROOT, st.DATA, st._catalog_critical, st._math_residual, st._crit_cache) = saved


def _write(path: str, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f)


def _mk_unified(data: str, slug: str):
    """unified/content_list.json 在 = 已 ingest（過 stage 0/X/0.5）。"""
    _write(os.path.join(data, slug, 'unified', 'content_list.json'), [])


def _mk_rules(data: str, slug: str):
    """extract_rules.yaml 在 = 已 audit（過 stage 1）。內容無關，存在性才是判據。"""
    _write(os.path.join(data, slug, 'extract_rules.yaml'), 'rules: []')


def _mk_book(data: str, slug: str):
    """parsed/book.json 在 = 已 parse（過 stage 2）。"""
    _write(os.path.join(data, slug, 'parsed', 'book.json'), {'chapters': []})


def _mk_ch(data: str, slug: str, name: str, problems: list):
    """寫一個章節檔（ch*.json / app*.json），problems 為 problem dict 陣列。"""
    _write(os.path.join(data, slug, 'parsed', name), {'problems': problems})


def _mk_deployed(root: str, slug: str):
    """data/<slug>/book.json 在 = 已上站（build_all 產物）。"""
    _write(os.path.join(root, 'data', slug, 'book.json'), {})


def _mk_sol_book(data: str, slug: str):
    """<slug>_sol/unified/content_list.json 在 = 有解答本（has_sol_book=True）。"""
    _write(os.path.join(data, f'{slug}_sol', 'unified', 'content_list.json'), [])


def _set_state(root: str, slug: str, **kv):
    """寫 pipeline_state.json（_catalog_accepted 讀此檔）。"""
    _write(os.path.join(root, 'book_pipeline', 'pipeline_state.json'), {slug: kv})


def _catalog_token(todo: str) -> str:
    """從 todo 字串挑出 catalog_audit 那個 token（'(可選)' 後綴黏在 token 末，須整 token 比對，
    因 todo 同時含 'translate(可選)'，不可用整串掃可選）。"""
    for tok in todo.split():
        if tok.startswith('catalog_audit'):
            return tok
    return ''


def _sol_token(todo: str) -> str:
    """從 todo 字串挑出 sol_extract 那個 token（同 _catalog_token 之理）。"""
    for tok in todo.split():
        if tok.startswith('sol_extract'):
            return tok
    return ''


def _non_optional(todo: str) -> str:
    """複刻 main() L239 的過濾：剔除所有 '(可選)' 後綴 token，回非可選殘餘。
    這是 gate 的**真正後果**——main() 只把這串非空者列入 daemon 待辦（→ 派 LLM）。
    『(可選)』後綴開關不是字串美觀，而是這條過濾的觸發器：gate token 留在待辦 → 派工；
    降可選 → 整本從待辦消失 → reactive loop 不再每輪重派 = 不 churn。對 assess 的字串後綴
    斷言只驗了一半；綁到這條過濾才真正釘住『防 post-deploy busy-loop』的因果。"""
    return ' '.join(p for p in todo.split() if not p.endswith('(可選)'))


# ── test 1：純磁碟狀態驅動的 stage 轉移矩陣（daemon frontier 的地基）──────────────────
def test_assess_stage_transitions_by_disk_state():
    """逐態構造磁碟、斷言 assess 回對的 stage 字串 + todo 動詞。

    這是整個 status 模組的核心：stage 是「讀實際資料」推出的，不是檔名臆測。
    判定順序 = unified→rules→book.json→sol_stats，任一前置缺即短路回對應待辦動詞。
    這條矩陣錯一格 = daemon 對該書派錯 stage 的 LLM 工（如沒 audit 就叫去 parse）。
    """
    with _sandbox() as (root, data):
        # (a) 僅 raw（無 unified）：raw 註冊表把它標 0 待ingest，動詞 ingest
        r = st.assess('bk', pending=frozenset(), raw={'bk': 'bk.pdf'})
        assert r['stage'] == '0 待ingest' and r['todo'] == 'ingest', r
        # 同態但在 pending（已 PUT、OCR 處理中）→ stage 0.5、動詞仍 ingest（中性動詞統一）
        r = st.assess('bk', pending={'bk'}, raw={})
        assert r['stage'] == '0.5 OCR處理中' and r['todo'] == 'ingest', r
        # 同態但既不在 raw 也不在 pending → X 未ingest（雲端無 raw_pdfs 時的書）
        r = st.assess('bk', pending=frozenset(), raw={})
        assert r['stage'] == 'X 未ingest' and r['todo'] == 'ingest', r

        # (b) unified 在、rules 不在 → 1 待audit
        _mk_unified(data, 'bk')
        r = st.assess('bk')
        assert r['stage'] == '1 待audit' and r['todo'] == 'audit', r

        # (c) rules 在、book.json 不在 → 2 待parse
        _mk_rules(data, 'bk')
        r = st.assess('bk')
        assert r['stage'] == '2 待parse' and r['todo'] == 'parse', r

        # (d) book.json + 一章無 solution 的題目 → 3 parsed（有題、無解 → 非 sol已merge）
        _mk_book(data, 'bk')
        _mk_ch(data, 'bk', 'ch01.json', [{'num': '1.1'}])
        r = st.assess('bk')
        assert r['stage'] == '3 parsed', r
        assert r['prob'] == 1 and r['sol'] == 0, r
        # 無 sol_book → 不提示 sol_extract；catalog_critical=0；但無 zh overlay → 永遠補
        # 'translate(可選)'（翻譯一律可選，不 gate）。故唯一 todo = translate(可選)。
        assert r['todo'] == 'translate(可選)', r

        # (e) 章節題目含 solution → 4 sol已merge（tot 且 sol 皆真）
        _mk_ch(data, 'bk', 'ch01.json', [{'num': '1.1', 'solution': ['ans']}])
        r = st.assess('bk')
        assert r['stage'] == '4 sol已merge', r
        assert r['prob'] == 1 and r['sol'] == 1, r

        # (f) 附錄檔（app*.json）的題目也必須計入 prob/sol（schema 明載 ch*.json/app*.json 兩源）。
        # 若 sol_stats 漏掉 app*，附錄全是題的書會被低報、stage 誤判。加一附錄含一有解題：
        _mk_ch(data, 'bk', 'appA.json', [{'num': 'A.1', 'solution': ['z']}])
        r = st.assess('bk')
        assert r['prob'] == 2 and r['sol'] == 2, f"app*.json 題目須計入：{r}"

        # (g) 有 .zh.json overlay → stage 帶 ' +zh' 後綴、且 translate todo 消失（不再補可選）。
        # 這是「已翻譯」的可視標記 + 不重複派 translate 的開關，整章判定的最後一維。
        _write(os.path.join(data, 'bk', 'parsed', 'ch01.zh.json'),
               {'problems': [{'num': '1.1', 'solution': ['ans']}]})
        r = st.assess('bk')
        assert r['stage'] == '4 sol已merge +zh', f"有 zh overlay 應帶 +zh 後綴：{r}"
        assert 'translate' not in r['todo'], f"已有 zh overlay 不該再補 translate：{r}"
        # 反證：zh overlay 不污染題數（仍 2 題 2 解，非把 ch01.zh.json 的題重複計成 3）
        assert r['prob'] == 2 and r['sol'] == 2, f"zh overlay 不該重複計題：{r}"


# ── test 2：catalog gate — 上站前強制 / 上站後降可選（防 post-deploy busy-loop 核心）──
def test_assess_catalog_gate_pre_vs_post_deploy():
    """parsed 完整 + catalog_critical=3 時，catalog_audit todo 的『(可選)』後綴開關。

    這是 engineer-away-operational-anxiety 的具體守衛：
      - 未 deploy → 'catalog_audit(3)'（無後綴）→ main() 視為非可選待辦 → daemon 必派工修。
        catalog 須修到可服務品質才上站，這是 gate。
      - 已 deploy 或 pipeline_state.catalog_accepted → 'catalog_audit(3)(可選)' → main() 過濾掉、
        daemon 不再派工。因殘留多是 MinerU OCR 源頭缺（空 caption / 缺 id），重審也補不出，
        若還當強制待辦 → reactive advance loop 每輪重審、燒 LLM、永不 idle = 燒錢 churn。
    這條判反 = post-deploy busy-loop。
    """
    with _sandbox(catalog_critical=3) as (root, data):
        _mk_unified(data, 'bk')
        _mk_rules(data, 'bk')
        _mk_book(data, 'bk')
        _mk_ch(data, 'bk', 'ch01.json', [{'num': '1.1'}])  # 有題無解，stage=3 parsed

        # 未 deploy（無 data/bk/book.json）+ 未 accept → gate（catalog token 無『(可選)』後綴）。
        # 注意：todo 字串還含 'translate(可選)'（翻譯一律可選），故須對 catalog token 本身斷言，
        # 不能用整串 '(可選)' not in；用 split() 取 catalog_audit 那個 token 精準驗。
        r = st.assess('bk')
        cat_tok = _catalog_token(r['todo'])
        assert cat_tok == 'catalog_audit(3)', f"上站前 catalog 必須是強制 gate（token 無可選）：{r}"
        # 真正後果：gate token 必須穿過 main() 的非可選過濾 → 進 daemon 待辦 → 派 LLM 修。
        # 只驗字串後綴不夠（後綴拼錯成 (optional) 也會「無可選」卻不被過濾）——綁到過濾才釘住因果。
        assert _non_optional(r['todo']) == 'catalog_audit(3)', \
            f"上站前 catalog gate 必須進 main() 待辦（非可選殘餘恰為它）：{r}"

        # 已 deploy → catalog token 降可選
        _mk_deployed(root, 'bk')
        r = st.assess('bk')
        assert _catalog_token(r['todo']) == 'catalog_audit(3)(可選)', f"已上站後 catalog 應降可選：{r}"
        # 真正後果：降可選後 main() 過濾掉它 → 整本從待辦消失（此例別無其他 gate）→ 不再每輪重派。
        # 這條 == '' 才是「防 post-deploy busy-loop」的硬證據（assess 後綴存在 ≠ daemon 真會略過）。
        assert _non_optional(r['todo']) == '', \
            f"已上站後 catalog 降可選須讓 main() 待辦清空（不 churn）：{r}"

        # 即使未 deploy，但 pipeline_state catalog_accepted=True → 也降可選
        # （det+LLM 修完仍殘、源頭缺、人工 accept → 不再強制 churn）
        with _sandbox(catalog_critical=3) as (root2, data2):
            _mk_unified(data2, 'bk')
            _mk_rules(data2, 'bk')
            _mk_book(data2, 'bk')
            _mk_ch(data2, 'bk', 'ch01.json', [{'num': '1.1'}])
            _set_state(root2, 'bk', catalog_accepted=True)
            r = st.assess('bk')
            assert _catalog_token(r['todo']) == 'catalog_audit(3)(可選)', f"catalog_accepted 應降可選：{r}"
            assert _non_optional(r['todo']) == '', \
                f"catalog_accepted 降可選須讓 main() 待辦清空（人工 accept 後不再 churn）：{r}"
            # 反證：catalog_accepted=False（不同 key、或顯式 False）不該降可選——確認降級確由該 flag 觸發、
            # 非 _set_state 一寫 state 就降（套套邏輯防線）。重置 state 為無關鍵：
            _set_state(root2, 'bk', some_other_flag=True)
            r = st.assess('bk')
            assert _catalog_token(r['todo']) == 'catalog_audit(3)', \
                f"catalog_accepted 未設（僅有無關 flag）時仍須是 gate，證降級確由該 flag 觸發：{r}"


# ── test 3：sol gate — 與 catalog 同構的 post-deploy 降級 ──────────────────────────
def test_assess_sol_gate_post_deploy_optional():
    """has_sol_book 且 sol==0 且非 _pending 時，sol_extract todo 的『(可選)』後綴開關。

    與 catalog_audit 同構：解答本存在但主書一題都沒 merge 到 solution（sol==0）→
      - 未 deploy → 'sol_extract(<slug>_sol)' gate（解答併入主書才完整，上站前該修）。
      - 已 deploy → 'sol_extract(<slug>_sol)(可選)'。否則一本「已部署、解答書卻 merge 不成」的書
        （如 griffiths_qm3）會讓 advance loop 每輪重派昂貴 sol_extract LLM、reactive loop 永不 idle。
    """
    with _sandbox(catalog_critical=0) as (root, data):
        _mk_unified(data, 'bk')
        _mk_rules(data, 'bk')
        _mk_book(data, 'bk')
        _mk_ch(data, 'bk', 'ch01.json', [{'num': '1.1'}])  # 有題、sol==0
        _mk_sol_book(data, 'bk')  # 有解答本 → has_sol_book=True

        # 未 deploy → sol_extract token 是 gate（無『(可選)』後綴）。同 catalog，須對 token 本身斷言
        # （todo 含 'translate(可選)'），不能用整串 '(可選)' not in。
        r = st.assess('bk')
        assert _sol_token(r['todo']) == 'sol_extract(bk_sol)', f"上站前 sol_extract 必須是強制 gate：{r}"
        # 真正後果：sol gate token 穿過 main() 過濾 → 進待辦派工（catalog_critical=0 → 唯一非可選殘餘）。
        assert _non_optional(r['todo']) == 'sol_extract(bk_sol)', \
            f"上站前 sol gate 必須進 main() 待辦：{r}"

        # 已 deploy → sol token 降可選
        _mk_deployed(root, 'bk')
        r = st.assess('bk')
        assert _sol_token(r['todo']) == 'sol_extract(bk_sol)(可選)', f"已上站後 sol_extract 應降可選：{r}"
        # 真正後果：降可選後 main() 待辦清空 → griffiths_qm3 那種「已部署、解答 merge 不成」的書
        # 不再每輪重派昂貴 sol_extract（reactive loop 得以 idle）。這條 == '' 才是 busy-loop 守衛的硬證。
        assert _non_optional(r['todo']) == '', \
            f"已上站後 sol 降可選須讓 main() 待辦清空（不每輪重派 sol_extract）：{r}"

    # 反證一：_sol_pending（sol_rules.yaml 標 _pending: true，主書品質不足、不該 merge）→
    # 即便 has_sol_book 且 sol==0 也**完全不提示 sol_extract**（連可選都不該有）。
    # 漏這條 = 對品質不足、刻意不 merge 的書硬派 sol_extract = 浪費且違背 pending 語義。
    with _sandbox(catalog_critical=0) as (root, data):
        _mk_unified(data, 'bk')
        _mk_rules(data, 'bk')
        _mk_book(data, 'bk')
        _mk_ch(data, 'bk', 'ch01.json', [{'num': '1.1'}])  # sol==0
        _mk_sol_book(data, 'bk')
        _write(os.path.join(data, 'bk_sol', 'sol_rules.yaml'), '_pending: true')
        r = st.assess('bk')
        assert _sol_token(r['todo']) == '', f"_sol_pending 時不該出現任何 sol_extract token：{r}"

    # 反證二：sol>0（解答已 merge 進主書）→ 不再提示 sol_extract（gate 條件是 sol==0）。
    # 漏這條 = 對已完成 merge 的書重派 sol_extract。
    with _sandbox(catalog_critical=0) as (root, data):
        _mk_unified(data, 'bk')
        _mk_rules(data, 'bk')
        _mk_book(data, 'bk')
        _mk_ch(data, 'bk', 'ch01.json', [{'num': '1.1', 'solution': ['ans']}])  # sol==1
        _mk_sol_book(data, 'bk')
        r = st.assess('bk')
        assert _sol_token(r['todo']) == '', f"sol>0（已 merge）不該再提示 sol_extract：{r}"


# ── test 4：sol_stats 壞檔不連坐 + solution 三態語義 ─────────────────────────────────
def test_sol_stats_corrupt_json_and_solution_tristate():
    """sol_stats 讀 parsed/ch*.json 計 (題總數, 有解數)；守兩條 invariant：

    1. 截斷壞檔（'{not valid'，模擬 SIGKILL/未完寫）try/except 跳過、不崩潰、不連坐——
       其他正常章照常計數。否則壞一章 = 整本假 0 題（silent low-report），dashboard 誤判 stage。
    2. solution 三態：缺鍵 / 空 list / None 皆判「無解答」（truthy 判斷 `pr.get('solution')`）；
       唯非空 list 才算有解。這語義錯 = stage 在 3 parsed / 4 sol已merge 間判反。
    """
    with _sandbox() as (root, data):
        # 壞檔：截斷 JSON。一正常章含四種 solution 態。
        _write(os.path.join(data, 'bk', 'parsed', 'ch00.json'), '{not valid')
        _mk_ch(data, 'bk', 'ch01.json', [
            {'num': '1.1'},                    # 缺 solution 鍵 → 無解
            {'num': '1.2', 'solution': []},    # 空 list → 無解（falsy）
            {'num': '1.3', 'solution': None},  # None → 無解（falsy）
            {'num': '1.4', 'solution': ['x']},  # 非空 list → 有解
        ])
        tot, sol = st.sol_stats('bk')
        # 壞檔 ch00 整個跳過（不貢獻、不崩潰）；ch01 的 4 題全計入 tot，僅 1 題有解
        assert tot == 4, f"壞檔應靜默跳過、正常章 4 題全計：tot={tot}"
        assert sol == 1, f"僅非空 list solution 算有解：sol={sol}"

        # 壞檔不連坐的反證：把壞檔換成另一正常章，tot 應增加（證明它本可被讀、是壞才跳）
        _mk_ch(data, 'bk', 'ch00.json', [{'num': '0.1', 'solution': ['y']}])
        tot2, sol2 = st.sol_stats('bk')
        assert tot2 == 5 and sol2 == 2, f"修好原壞檔後該多計 1 題 1 解：{(tot2, sol2)}"

    # .zh.json overlay 檔不該被 sol_stats 計入（避免雙語檔重複計題）
    with _sandbox() as (root, data):
        _mk_ch(data, 'bk', 'ch01.json', [{'num': '1.1', 'solution': ['x']}])
        _write(os.path.join(data, 'bk', 'parsed', 'ch01.zh.json'),
               {'problems': [{'num': '1.1', 'solution': ['x']}]})
        tot, sol = st.sol_stats('bk')
        assert tot == 1 and sol == 1, f".zh.json 不該重複計入：{(tot, sol)}"


if __name__ == '__main__':
    test_assess_stage_transitions_by_disk_state()
    print('✓ stage 轉移矩陣（X/0/0.5 待ingest → 1 待audit → 2 待parse → 3 parsed → 4 sol已merge）')
    test_assess_catalog_gate_pre_vs_post_deploy()
    print('✓ catalog gate：上站前強制 / 已deploy|已accept 降可選（防 post-deploy busy-loop）')
    test_assess_sol_gate_post_deploy_optional()
    print('✓ sol gate：上站前強制 / 已deploy 降可選（與 catalog 同構）')
    test_sol_stats_corrupt_json_and_solution_tristate()
    print('✓ sol_stats：壞檔不連坐靜默跳過 + solution 三態（缺/空/null=無，非空list=有）+ .zh 不重計')
    print('\n全部通過 ✅')
