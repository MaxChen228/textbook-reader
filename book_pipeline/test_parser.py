"""parser 韌性單測：uv run python -m book_pipeline.test_parser

parser.py 是整條 pipeline 最核心的「LLM↔確定性碼」消費端——吃 audit agent 產的
extract_rules.yaml（regex 規則）+ MinerU OCR 出的 content_list block，純 regex/idx
化成 parsed/*.json。LLM/OCR 輸出變異極大，本檔守住一條鐵則：

    **單一畸形 block 或一條誤判規則，絕不該炸掉或靜默損毀整本書。**

parser 一旦在某 block 拋例外，整章/整本 parse 中斷、書卡在 parse 階段（daemon 不會
自動繞過）；一旦把非法型別靜默寫進輸出，壞資料一路漏到前端（reader 顯示 [object
Object]、catalog 缺圖卻無 gap 痕跡）。兩者都比「明確報錯」更危險。

本檔分兩類：
  1. 韌性回歸鎖（GREEN）：split_problems 的遞增守則等已實作的防護，加鎖防回退。
  2. 現況 baseline 鎖（GREEN，但釘住的是「有問題的現況」）：silent-corruption /
     silent-drop 的真實行為被固定下來，docstring 標明理想行為，作為修復前對照。
  3. 韌性契約鎖（GREEN，本 sweep 修補後啟用）：dogfood 揪出三個合理 LLM/OCR 輸入會 crash /
     靜默損毀的真 bug，已硬化 parser 消費端（見各 test docstring 的修補說明），啟用防回歸。

全 hermetic、synthetic block，不碰真實 mineru_data / 狀態檔。
"""
from __future__ import annotations

import re

from book_pipeline import parser


# 共用：一條無害的 \tag label regex（不影響非 equation block）
LABEL_RE = re.compile(r'\\tag\{([^}]+)\}')


# ══════════════════════════════════════════════════════════════════════════════
# 韌性契約鎖（dogfood 揭出的真 bug，消費端已硬化 → 現綠，啟用為長存回歸防護）
# 修補：expand_list_blocks 與 type=list 分支用 str(x) coerce 非字串 item；image bbox 全為
# number 才算 aspect；table_body 非 str 一律丟（防 [object Object]）。詳見 parser.py inline 註解。
# ══════════════════════════════════════════════════════════════════════════════

def test_expand_list_blocks_nonstr_items():
    """[BUG·crash] expand_list_blocks 對非字串 list_item 應 graceful，不炸整章切題。

    invariant：MinerU type=list block 的 list_items 理應全是字串，但 OCR 偶爾混入
    int / None（頁碼、空 cell 殘留）。expand_list_blocks 把每個 item 攤平成獨立
    text block 餵切題迴圈——這是 split_problems 與題目偵測的**必經路徑**。現況
    `(x or '').strip()` 對 int 直接 AttributeError（(1).strip() 不存在）→ 整章切題
    中斷、書卡 parse。正確行為：非字串 item coerce(str) 或 skip，合法字串 item
    照常攤平。本測試斷言「不拋例外」且 'real text' 仍出現在輸出。

    修復後該綠的最小改法：list comprehension 改
        items = [str(x).strip() for x in (b.get('list_items') or []) if x is not None]
    （expand_list_blocks 與 block_to_struct 內 type=list 分支兩處同病，須一併改。）
    """
    blocks = [{'type': 'list', 'text': '', 'list_items': [1, 'real text', None]}]
    out = parser.expand_list_blocks(blocks)  # 現況：AttributeError
    # 攤平後應有 text block，且合法字串題幹未被畸形 item 連坐丟棄
    texts = [b.get('text') for b in out if b.get('type') == 'text']
    assert 'real text' in texts, f'合法 item 應存活，實得 {texts}'


def test_block_to_struct_image_bad_bbox_no_crash():
    """[BUG·crash] image bbox 含 None/str 時 aspect 計算應守衛，圖仍輸出。

    invariant：bbox 是 OCR 估的座標，正常是 4 個 number；偶有 None（缺值）或 str
    （'104' 未轉型）。現況 `w = bbox[2] - bbox[0]` 對 '104'-int 或 int-None 直接
    TypeError → 炸整本解析。正確行為：bbox 非全 number 時略過 aspect（純 layout
    優化、非必要欄），圖本體（src/kind）照常輸出。本測試斷言不拋且回 {'t':'fig',...}。

    修復後該綠的最小改法：算 w/h 前先驗
        if len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox):
    或 try/except (TypeError) 包住 aspect 計算。
    """
    b = {'type': 'image', 'img_path': 'a.jpg', 'bbox': ['104', 46, 608, None]}
    out = parser.block_to_struct(b, LABEL_RE, False, False)  # 現況：TypeError
    assert out is not None and out.get('t') == 'fig', f'圖應輸出，實得 {out!r}'
    assert out.get('src') == 'a.jpg'
    # aspect 可有可無，但「有則必為 number」——絕不寫進壞值
    if 'aspect' in out:
        assert isinstance(out['aspect'], (int, float))


def test_block_to_struct_table_body_nonstr():
    """[BUG·silent-corruption] table_body 非 str（OCR 吐結構化 dict）時，html 欄不該漏壞型。

    invariant：out['html'] 是 parser 出口的型別不變式——必為 HTML 字串，前端直接
    innerHTML 注入。複雜表格偶爾被 OCR 吐成 {'rows':[...]} 結構化物件；現況
    `body_html = b.get('table_body') or ''` 後**未驗型**直接 out['html']=body_html，
    把 dict 靜默寫進去 → bake/前端拿到非字串，reader 顯示 [object Object]。肉眼到
    reader 才見、無任何報錯。正確行為：table_body 非 str → skip（回 None）或 str()。
    本測試斷言「html 欄為 str，否則該 block 整個被丟（回 None）」。

    修復後該綠的最小改法：取 body_html 後
        if not isinstance(body_html, str): return None   # 或 body_html = str(...)
    （code 分支已是 f-string 故天然 str；table 分支是唯一漏型者。）
    """
    b = {'type': 'table', 'table_body': {'rows': [1, 2]}}
    out = parser.block_to_struct(b, LABEL_RE, False, False)
    # 兩種可接受的正確結果：丟掉(None) 或 html 強制成 str；現況回 dict → 紅
    assert out is None or isinstance(out.get('html'), str), \
        f'html 出口型別不變式破壞：{out!r}'


# ══════════════════════════════════════════════════════════════════════════════
# 現況 baseline 鎖（GREEN）：釘住「有問題但確定」的行為，供修復對照。
# 這些測試**通過**，但釘的是 silent gap / silent-corruption 的現狀；docstring 標明
# 理想行為。改進時這些鎖會紅，提醒「現況已變」——這正是 baseline 鎖的用途。
# ══════════════════════════════════════════════════════════════════════════════

def test_block_to_struct_image_missing_imgpath_with_caption():
    """[baseline·silent-corruption] 有 caption/編號但無 img_path 的圖，現況被靜默丟。

    現況：`if not fname: return None`——OCR 抽到圖說 'Fig. 3.2 ...'（含 catalog 編號）
    卻沒對應檔名時，整個 fig block 連 caption 一起蒸發。catalog 因此缺這張圖、而
    _gaps.md 也抓不到（gap 報告只查題號，不查圖）→ 雙重靜默。

    理想：至少保留 caption-only placeholder（{'t':'fig','caption':...,'id':'fig-3.2'}），
    讓 catalog 與 gap 報告有痕跡可循。本測試固定現況=回 None 作改進前 baseline；若
    哪天加了 placeholder，此鎖會紅→提示「行為已改善，更新此測試」。
    """
    b = {'type': 'image', 'img_path': '', 'image_caption': ['Fig. 3.2 The apparatus']}
    out = parser.block_to_struct(b, LABEL_RE, False, False)
    assert out is None, f'現況應靜默丟（baseline）；若非 None 表行為已改：{out!r}'


def test_split_problems_hyphen_num_must_match_fallback():
    """[baseline·silent-corruption] hyphen 題號（'2-1'）+ must_match=True → 整章題目靜默蒸發。

    現況：problem_chapter_must_match 時 `int(num.split('.')[0])` 對 num='2-1' 走
    split('.')→['2-1']→int('2-1') ValueError→except m=None→該命中不開題。若全章題號
    都是 hyphen 形（P.2-1, P.2-2...），則**每一題都被否決、problems 整章清空**，且
    validate 不交叉驗 → 無痕跡。

    根因：這其實是 **audit agent 誤判**——對 hyphen 題號書（章-題用連字號而非點）
    設了 problem_chapter_must_match=True，但章號比對的 split('.')[0] 假設了點分隔。
    理想：偵測到題號用 hyphen 分隔時，must_match 應 fallback 成「不比對章號」，而非
    把整章吞掉。本測試固定現況=problems==[] 暴露靜默損毀，作修復對照 baseline。
    """
    problem_start_re = re.compile(r'^P\.(\d+-\d+)\s+')
    blocks = [
        {'type': 'text', 'text': 'P.2-1 Compute the integral'},
        {'type': 'text', 'text': 'P.2-2 Solve the ODE'},
    ]
    out = parser.split_problems(
        blocks, rules={}, ch_num=2, label_re=LABEL_RE,
        problem_start_re=problem_start_re, problem_chapter_must_match=True,
    )
    assert out == [], f'現況應靜默清空（baseline，暴露損毀）；若非空表行為已改：{out!r}'


# ══════════════════════════════════════════════════════════════════════════════
# 韌性回歸鎖（GREEN）：已實作的關鍵防護，加鎖防回退。
# ══════════════════════════════════════════════════════════════════════════════

def test_split_problems_increasing_guard_rejects_false_hits():
    """[lock·wrong-output] 題號遞增守則：≤ 已見最大的偽命中不開新題。

    invariant：章末 Problems 區題號嚴格遞增（1,2,3,...）。Problems 區後常緊接正文
    （SOCIAL ISSUES / supplement），其中的 numbered list 'N.' 會被 problem_start_re
    誤命中。守則：題號回退（≤ max_num_seen）視為「已離開題目區」的偽命中，不開新題、
    內容歸入當前最後一題 body。沒這守則，正文 numbered list 會被切成假題目污染題庫。

    本測試：題 1..5 後混入回退 '1.'（正文 numbered list），斷言：
      - 只切出 5 題（偽命中未開第 6 題）
      - 該 '1.' 行被吸進題 5 的 body（非丟棄）
    這是 split_problems 已正確實作的行為，鎖住防未來重構誤刪守則。
    """
    problem_start_re = re.compile(r'^(\d+)\.\s+')
    blocks = [
        {'type': 'text', 'text': '1. First problem'},
        {'type': 'text', 'text': '2. Second problem'},
        {'type': 'text', 'text': '3. Third problem'},
        {'type': 'text', 'text': '4. Fourth problem'},
        {'type': 'text', 'text': '5. Fifth problem'},
        # 偽命中：正文 numbered list 用 '1.' 起頭，題號回退到 1 ≤ 5
        {'type': 'text', 'text': '1. A numbered point in following prose, not a problem'},
    ]
    out = parser.split_problems(
        blocks, rules={}, ch_num=1, label_re=LABEL_RE,
        problem_start_re=problem_start_re, problem_chapter_must_match=False,
    )
    assert len(out) == 5, f'偽命中不該開新題，應 5 題，實得 {len(out)}：{[p["num"] for p in out]}'
    assert [p['num'] for p in out] == ['1', '2', '3', '4', '5']
    # 回退行被吸進最後一題 body（守則只擋「開新題」，內容仍保留不丟）
    last_md = ' '.join(blk.get('md', '') for blk in out[-1]['body'])
    assert 'numbered point' in last_md, f'偽命中行應併入題 5 body，實得 {out[-1]["body"]!r}'


def test_split_problems_increasing_guard_walk_inline():
    """[lock·wrong-output] inline 模式（整數 tuple 比較）同樣擋回退偽命中。

    walk_inline_chapter 走另一套遞增守則：用【整數 tuple】比較題號（'1.44'→(1,44)）
    而非純末段，這樣 per-section 重起（1.2.1>1.1.9）不誤殺。本測試驗非 namespace
    模式下，回退題號偽命中仍被擋，題目不被正文 numbered list 污染。鎖住此核心防護。

    本測試鎖住三條獨立 invariant（缺一不可，否則退化成假綠）：
      A. 回退偽命中不開新題（len==3）——守則存在性。
      B. 偽命中行被【吸進當前題 body】而非丟棄——與題 3 對稱（守則只擋開新題、不丟內容）。
         缺 A 之外的此條，「守則改成 drop」也會 len==3 矇混過關。
      C. tuple 比較 vs 純末段比較的【判別性】：per-section 重起 1.1.9→1.2.1，末段 1≤9
         但 tuple (1,2,1)>(1,1,9)。若有人把 max_num_seen 退回 int(...split('.')[-1])
         的純末段比較，C 會誤殺 1.2.1 → 紅；A/B 全是末段單調遞增故矇混。C 是本鎖唯一
         真正釘死 docstring 所述「tuple 而非末段」設計決策者。
    """
    # inline 模式題號形如 'N.M'（Griffiths 風格）；problem_start_re 抓 '1.44' 整串
    problem_start_re = re.compile(r'^(\d+\.\d+)\s+')
    section_re = re.compile(r'^(\d+\.\d+)\s+(\S.*)$')  # 不會命中題目（題目無 title 段亦可）
    subsection_re = re.compile(r'^(\d+\.\d+\.\d+)\s+(\S.*)$')
    blocks = [
        {'type': 'text', 'text': '1.1 First real problem in chapter 1'},
        {'type': 'text', 'text': '1.2 Second real problem'},
        {'type': 'text', 'text': '1.3 Third real problem'},
        # 偽命中：題號回退 (1,1) <= (1,3)
        {'type': 'text', 'text': '1.1 A back-referenced numbered item, not a new problem'},
    ]
    body, problems = parser.walk_inline_chapter(
        blocks, rules={}, ch_num=1, label_re=LABEL_RE,
        problem_start_re=problem_start_re, section_re=section_re,
        subsection_re=subsection_re, example_re=None,
        problem_chapter_must_match=False, namespace_by_section=False,
    )
    # A：守則存在（不開第 4 題）
    assert len(problems) == 3, f'回退偽命中不該開新題，應 3 題，實得 {[p["num"] for p in problems]}'
    assert [p['num'] for p in problems] == ['1.1', '1.2', '1.3']
    # B：偽命中行併入最後一題 body（守則只擋開新題、內容不丟；與題 3 對稱）
    last_md = ' '.join(blk.get('md', '') for blk in problems[-1]['body'])
    assert 'back-referenced' in last_md, \
        f'偽命中行應併入題 1.3 body 而非丟棄，實得 {problems[-1]["body"]!r}'

    # C：判別性子情境——per-section 重起。3 段題號 1.1.9 後接 1.2.1：
    #    末段 1 ≤ 9（純末段比較會誤殺 1.2.1），tuple (1,2,1) > (1,1,9)（正解保留）。
    #    這是唯一能讓「tuple 退化成末段」regression 變紅的 case，鎖死 docstring 設計決策。
    ps_re = re.compile(r'^(\d+(?:\.\d+)+)\s+')
    restart_blocks = [
        {'type': 'text', 'text': '1.1.9 Ninth problem of section 1.1'},
        {'type': 'text', 'text': '1.2.1 First problem of next section 1.2'},
    ]
    _body, restart_problems = parser.walk_inline_chapter(
        restart_blocks, rules={}, ch_num=1, label_re=LABEL_RE,
        problem_start_re=ps_re, section_re=section_re,
        subsection_re=subsection_re, example_re=None,
        problem_chapter_must_match=False, namespace_by_section=False,
    )
    assert [p['num'] for p in restart_problems] == ['1.1.9', '1.2.1'], (
        'per-section 重起 1.2.1 被誤殺 → 守則退化成純末段比較（應為整數 tuple），'
        f'實得 {[p["num"] for p in restart_problems]}'
    )


def test_last_chapter_backmatter_cap():
    """[lock·silent-corruption] 無附錄時 bibliography/index_start_page 也須 cap 末章。

    invariant：bib/index_start_page 是「全書 back-matter 切口」。原本只在附錄迴圈當末附錄
    cutoff 用——當 appendices:[] 時該迴圈不跑，切口整個失效，末章把 Glossary/Index/
    References 全吞進最後一題（18 本書實證：riley 3212、chaikin 1325、weinberg_qft 877…）。
    _last_chapter_backmatter_cap 在無附錄時補回末章 cap。本鎖驗五條 invariant：

      A. 無附錄 + index_start_page 落在末章內 → 回該 block idx（cap 生效）。
      B. 有附錄 → None（原路徑、零行為改變；附錄迴圈自有 cutoff）。
      C. 無 bib/index_start_page → None。
      D. stale 設定：切口落在末章 title 之前（ross index_start_page=65 型）→ None，
         fail-safe 絕不誤切好書。
      E. bib_start 優先於 index_start（與附錄迴圈 cutoff 同序）。
    """
    # page_idx 對齊 block idx：第 i 個 block 在 page i（簡化；first_block_idx_after_page
    # 回首個 page_idx>=cutoff 的 block）。末章 title@8、problems 區、back-matter@15、章尾@20。
    blocks = [{'page_idx': i} for i in range(20)]
    last = {'num': 9, 'chapter_title_block_idx': 8, 'problems_block_idx': 10,
            'next_chapter_block_idx': 20}
    chapters = [{'num': 8, 'chapter_title_block_idx': 0, 'next_chapter_block_idx': 8}, last]

    # A：無附錄、index_start_page=15（落在 problems(10) 與章尾(20) 之間）→ cap=15
    cap = parser._last_chapter_backmatter_cap(chapters, [], None, 15, blocks)
    assert cap == 15, f'A：應 cap 到 back-matter 起點 15，實得 {cap!r}'

    # B：有附錄 → 不 cap（附錄迴圈自理）
    cap = parser._last_chapter_backmatter_cap(
        chapters, [{'id': 'A', 'chapter_title_block_idx': 16}], None, 15, blocks)
    assert cap is None, f'B：有附錄應回 None（原路徑），實得 {cap!r}'

    # C：無切口設定 → None
    assert parser._last_chapter_backmatter_cap(chapters, [], None, None, blocks) is None

    # D：stale 切口落在末章 title(8) 之前（page 5）→ None，絕不誤切
    assert parser._last_chapter_backmatter_cap(chapters, [], None, 5, blocks) is None, \
        'D：切口在章前的 stale 設定必須回 None（fail-safe）'

    # E：bib_start 優先於 index_start（bib=12 落章內 → cap=12，不取 index=18）
    cap = parser._last_chapter_backmatter_cap(chapters, [], 12, 18, blocks)
    assert cap == 12, f'E：bib_start 應優先，cap 應為 12，實得 {cap!r}'


def test_expand_list_blocks_normal_passthrough():
    """[lock] expand_list_blocks 正常路徑：list_items 全字串時逐項攤平成 text block。

    這是切題迴圈不漏 (a)(b)(c) 子題的關鍵——MinerU 把連續編號題 OCR 成單一 list
    block，必須攤平成多個 text block 才能逐項命中 problem_start_re。鎖住正常行為，
    與上面的 nonstr crash bug 形成對照（修 bug 不該破壞此正常路徑）。
    """
    blocks = [{'type': 'list', 'text': '', 'list_items': ['2. alpha', '3. beta']}]
    out = parser.expand_list_blocks(blocks)
    assert len(out) == 2, f'兩 item 應攤成兩 block，實得 {len(out)}'
    assert [b['text'] for b in out] == ['2. alpha', '3. beta']
    assert all(b['type'] == 'text' for b in out)
    # list_item 不繼承 text_level（避免被誤判 heading）
    assert all('text_level' not in b for b in out)


def test_block_to_struct_table_str_body_ok():
    """[lock] table_body 為合法 HTML str 時正常輸出（與 nonstr bug 對照的正路徑）。

    確保修 table dict bug 不會誤傷正常字串表格。caption 含 'Table 1.1' 時應抽出
    id='tbl-1.1'。
    """
    b = {'type': 'table', 'table_body': '<table><tr><td>x</td></tr></table>',
         'table_caption': ['Table 1.1 Constants']}
    out = parser.block_to_struct(b, LABEL_RE, False, False)
    assert out is not None and out['t'] == 'table'
    assert isinstance(out['html'], str) and '<table>' in out['html']
    assert out.get('id') == 'tbl-1.1', f'應從 caption 抽 id，實得 {out.get("id")!r}'


if __name__ == '__main__':
    # 消費端已硬化 → 三個揭 bug 測試啟用為長存防護（crash 止血 + 型別不變式守護）。
    test_expand_list_blocks_nonstr_items()
    print('✓ 韌性：expand_list_blocks 對非字串 list_item（int/None）coerce/skip，不炸切題')
    test_block_to_struct_image_bad_bbox_no_crash()
    print('✓ 韌性：image 壞 bbox（None/str）略過 aspect、圖本體照常輸出，不炸解析')
    test_block_to_struct_table_body_nonstr()
    print('✓ 韌性：table_body 非 str（OCR 結構化 dict）一律丟，html 出口型別不變式守住')
    test_block_to_struct_image_missing_imgpath_with_caption()
    print('✓ baseline：有 caption 無 img_path 的圖被靜默丟（catalog/gap 雙重無痕，修復對照鎖）')
    test_split_problems_hyphen_num_must_match_fallback()
    print('✓ baseline：hyphen 題號 + must_match=True → 整章題目靜默蒸發（audit 誤判，修復對照鎖）')
    test_split_problems_increasing_guard_rejects_false_hits()
    print('✓ lock：split_problems 遞增守則擋正文 numbered list 偽命中（5 題、回退行併入 body）')
    test_split_problems_increasing_guard_walk_inline()
    print('✓ lock：walk_inline 整數 tuple 遞增守則同擋回退偽命中（3 題）')
    test_last_chapter_backmatter_cap()
    print('✓ lock：無附錄時 bib/index_start_page 仍 cap 末章（擋 Glossary/Index 吞噬，stale 設定 fail-safe）')
    test_expand_list_blocks_normal_passthrough()
    print('✓ lock：expand_list_blocks 正常逐項攤平（不漏子題、不繼承 text_level）')
    test_block_to_struct_table_str_body_ok()
    print('✓ lock：合法 str table_body 正常輸出 + caption 抽 tbl-id')
    print('\n全部通過 ✅')
