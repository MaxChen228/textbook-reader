"""corpus overlay 防漂移單元測試（無外部依賴）：
    uv run python -m book_pipeline.test_corpus_overlay

守住的契約：anchor-hash 防漂移是 366 個 zh.json overlay「中譯不錯置」的地基。
`translate.overlay_anchor` 是 translate.py（寫 `a`）與 corpus.py（重算比對）的**單一真相**——
任何重構（換 sha1、改 `_norm` collapse 規則、改欄位排序、改 8-hex 截斷長度）都會讓 55247 個
已存 `a` 全部失配，整站中譯靜默退化成純英文，而 CI 仍全綠、沒人發現。本檔把 anchor 演算法
golden 值釘死，逼任何改演算法的 PR 立即 RED → 強制同步 re-anchor 全部 overlay。

同時釘住 `_patch_blocks` / `_merge_chunk_overlay` 消費端的 fail-safe 三路徑
（anchor 不符、索引越界/非 int、legacy 無 `a` 但來源欄位缺失），與正向套用、雙語並陳、
題號漂移退化。核心不變量：**寧可純英文也不錯置**。

全 hermetic：純 synthetic chunk/block，不碰真實 mineru_data、不寫任何檔。
"""

from textbooks.corpus import _patch_blocks, _merge_chunk_overlay
from book_pipeline.translate import overlay_anchor


def test_overlay_anchor_golden_and_properties():
    """anchor 演算法 golden + 不變性質。

    golden 80d3f819 = sha1('md\\x1fHello world')[:8]（雙空格被 _norm collapse 成單空格）。
    這個固定值是跨檔耦合命脈：translate.py 把它寫進每個 overlay patch 的 `a`，corpus.py
    重算後比對。任何人改 hash 函式 / _norm / 排序 / 截斷長度，此 assert 立刻 RED，
    擋下「55247 個舊 a 全失配 → 整站中譯靜默消失」這種 CI 全綠的災難。"""
    # 1) golden：固定輸入回確切值（已實測反推）
    assert overlay_anchor({'md': 'Hello  world'}) == '80d3f819'

    # 2) whitespace collapse 不變：雙空格與單空格同 anchor。
    #    _norm 用 ' '.join(text.split()) 折疊所有空白 → OCR/翻譯間距抖動不該打破比對。
    assert overlay_anchor({'md': 'Hello  world'}) == overlay_anchor({'md': 'Hello world'})
    #    換行、tab、首尾空白也一律折疊
    assert overlay_anchor({'md': ' Hello\tworld\n'}) == overlay_anchor({'md': 'Hello world'})

    # 3) 欄位排序無關：anchor 內部 sorted(keys)，dict 插入序不影響結果。
    #    table block 有 caption+footnote 兩欄，序敏感會讓部分 block 全失配。
    assert overlay_anchor({'md': 'x', 'caption': 'y'}) == overlay_anchor({'caption': 'y', 'md': 'x'})

    # 4) 多欄組合是不同 key 集合 → 與單欄不同 anchor（key 也進 hash，非只看 value）。
    assert overlay_anchor({'md': 'x', 'caption': 'y'}) != overlay_anchor({'md': 'x'})

    # 5) 回傳恆為 8 位 hex（截斷長度也是契約的一部分；改長度=全失配）。
    a = overlay_anchor({'md': 'anything'})
    assert len(a) == 8 and all(c in '0123456789abcdef' for c in a)
    print('✓ overlay_anchor golden 80d3f819 + collapse/排序無關/8-hex 釘死')


def test_patch_blocks_anchor_mismatch_keeps_english():
    """anchor 不符 → 退化純英文，不錯置（防漂移核心）。

    parser 重跑會打亂 body 索引，使舊 overlay 的 `i` 指到錯 block。對策：patch 帶 `a`，
    corpus 用「當前 block 的來源欄位」重算 anchor，不符就整條 skip。這裡 a='deadbeef' 故意
    不等於重算值 → 中譯**不得**寫入，原文 md 必須原封不動。這條 fail-safe 若被重構破壞，
    後果是中文掛錯段（甚至中文先於英文顯示），且靜默無聲。"""
    inp = [{'t': 'p', 'md': 'English'}]
    out = _patch_blocks(inp, [{'i': 0, 'a': 'deadbeef', 'md': '中譯'}])
    assert out[0]['md'] == 'English', 'anchor 不符必須保留原文'
    assert 'md_zh' not in out[0], '非雙語模式不該冒出 _zh 欄'
    # 輸入 block 必須原封未動：_patch_blocks 做 [dict(b) for b in blocks] 後只改副本。
    # 若退化成原地改 blocks（漏 copy），corpus 的 mtime cache 會把被污染的 dict 回存，
    # 下次同 slug 不同 lang 讀到髒資料 → 跨語言污染。釘住「不寫中譯時連副本欄位都不新增」+
    # 「輸入 dict identity 與內容皆不變」兩條，比舊的恆真 `is not None` 強。
    assert inp == [{'t': 'p', 'md': 'English'}], 'mismatch 不得原地污染輸入 block'
    assert out[0] is not inp[0], '回傳須是 dict 副本、非輸入同一物件'
    print('✓ anchor 不符 → 保留純英文、不錯置（不寫中譯、不污染輸入）')


def test_patch_blocks_anchor_match_applies():
    """anchor 符 → 套用中譯（正向對照）。

    與 mismatch 案配對才能證明防護**精確**：anchor 對得上時必須套用。若 fail-safe 過度拒絕
    （連正確的也擋），整章同樣退化純英文，也是 bug。這裡 a 用 overlay_anchor 真算，模擬
    translate.py 寫入流程。"""
    a = overlay_anchor({'md': 'Hello world'})
    out = _patch_blocks([{'t': 'p', 'md': 'Hello world'}],
                        [{'i': 0, 'a': a, 'md': '你好'}])
    assert out[0]['md'] == '你好', 'anchor 符必須套用中譯'
    print('✓ anchor 符 → 套用中譯（fail-safe 不過度拒絕）')


def test_patch_blocks_oob_and_nonint_i_skip():
    """三條 skip 路徑：索引越界、非 int 的 i、legacy 無 a 但來源欄位缺失。

    全部須 silently skip、不崩潰、不污染原 block。這三路徑目前零守護測試，重構極易破：
      - 越界：isinstance(idx,int) and 0<=idx<len 守衛（i=5 對 1-block body）。
      - 非 int i：LLM/JSON 可能把 i 寫成字串 '0' → isinstance(idx,int) 擋掉，否則 list[str] 崩。
      - legacy 無 a：舊 overlay 沒 `a`，至少要求目標 block 真有對應「非空 str 來源欄位」，
        擋掉「md patch 掛到只有 tex 的 eq block」這種錯置。"""
    base = [{'t': 'p', 'md': 'X'}]

    # 越界：i=5 但 body 只有 1 block
    out = _patch_blocks([dict(base[0])], [{'i': 5, 'md': '中譯'}])
    assert out == [{'t': 'p', 'md': 'X'}], '越界 patch 必須整條丟'

    # 負索引：這是最陰險的一條。Python list[-1] 完全合法，若守衛只寫 `idx >= len(out)`
    # 漏了 `idx < 0`，i=-1 會把中譯靜默掛到 body 最後一個 block（錯置且無聲）。
    # 用 2-block body + i=-1，斷言兩個 block 都原文不變，逼守衛必含 `idx < 0`。
    out = _patch_blocks([{'t': 'p', 'md': 'A'}, {'t': 'p', 'md': 'B'}], [{'i': -1, 'md': '中譯'}])
    assert out == [{'t': 'p', 'md': 'A'}, {'t': 'p', 'md': 'B'}], '負索引必須擋掉、不得掛到末 block'

    # 非 int 的 i（字串 '0'）：不得當索引用（list['0'] 會 TypeError 崩潰）
    out = _patch_blocks([dict(base[0])], [{'i': '0', 'md': '中譯'}])
    assert out == [{'t': 'p', 'md': 'X'}], '字串 i 必須被 int 守衛擋掉'

    # float i（1.0）：JSON round-trip 可能把整數變浮點；list[1.0] 也 TypeError。
    out = _patch_blocks([dict(base[0])], [{'i': 1.0, 'md': '中譯'}])
    assert out == [{'t': 'p', 'md': 'X'}], 'float i 必須被 int 守衛擋掉'

    # i 缺失 / 為 None：patch.get('i') → None，不得當索引
    out = _patch_blocks([dict(base[0])], [{'i': None, 'md': '中譯'}])
    assert out == [{'t': 'p', 'md': 'X'}], 'i=None 必須被守衛擋掉'

    # legacy（無 a）：目標 eq block 缺 md 欄 → md patch 不該掛上去
    out = _patch_blocks([{'t': 'eq', 'tex': 'x'}], [{'i': 0, 'md': '中譯'}])
    assert out == [{'t': 'eq', 'tex': 'x'}], 'legacy 無 a 且來源缺欄 → skip（擋錯置）'

    # legacy（無 a）：來源欄位存在但為空白字串 → 同樣 skip（.strip() 守衛）
    out = _patch_blocks([{'t': 'p', 'md': '   '}], [{'i': 0, 'md': '中譯'}])
    assert out[0]['md'] == '   ', 'legacy 無 a 且來源為空白 → skip'
    print('✓ 越界 / 非int-i / legacy 缺欄 三路徑皆 silently skip 不崩潰')


def test_patch_blocks_bilingual_writes_zh_field():
    """雙語模式：寫 <field>_zh、保留原文（並陳顯示地基）。

    bi 模式 corpus 把中譯寫進 md_zh 而非覆蓋 md，前端並陳英文+中文。若錯置（覆蓋原文或
    寫錯欄名）並陳就崩。這裡確認原文 md 保留、md_zh 帶中譯。"""
    a = overlay_anchor({'md': 'Hello world'})
    out = _patch_blocks([{'t': 'p', 'md': 'Hello world'}],
                        [{'i': 0, 'a': a, 'md': '你好'}],
                        bilingual=True)
    assert out[0]['md'] == 'Hello world', '雙語模式原文必須保留'
    assert out[0]['md_zh'] == '你好', '雙語中譯寫入 md_zh'

    # 雙語模式下 anchor 不符也必須 fail-safe：不得冒出 md_zh。
    # 缺這條，「防漂移只在 zh 模式生效、bi 模式照掛錯置中文」的 regression 會綠燈過關——
    # 並陳視圖會把錯段的中譯顯示在某英文 block 旁，比純 zh 模式更顯眼地誤導讀者。
    out_bad = _patch_blocks([{'t': 'p', 'md': 'Hello world'}],
                            [{'i': 0, 'a': 'deadbeef', 'md': '你好'}],
                            bilingual=True)
    assert out_bad[0]['md'] == 'Hello world', '雙語 + anchor 不符：原文保留'
    assert 'md_zh' not in out_bad[0], '雙語 + anchor 不符：不得寫 md_zh（fail-safe 在 bi 模式也成立）'
    print('✓ bilingual → 寫 md_zh 並保留原文；anchor 不符 bi 模式也 fail-safe')


def test_merge_chunk_overlay_problem_num_drift_skips():
    """_merge_chunk_overlay 兩層防漂移：題號 num + 雙守衛 body 存在。

    題目層多一道防線：overlay problem 靠 `num` 對齊 chunk problem（prob_map `in` 守衛）。
    題號漂移（'1.5' 對不上現有 '1.1'）時舊 overlay 應安靜被忽略、原 problem 不變
    （而非掛到別題）。另測 title-only chunk（無 body 鍵）遇到帶 body 的 overlay：
    `if 'body' in overlay and chunk.get('body')` 雙守衛 → 整段 body overlay 丟、不崩潰。"""
    # 題號漂移：overlay num '1.5' 對不上 chunk num '1.1'，即使 anchor 算對也不該套用
    ba = overlay_anchor({'md': 'E'})
    chunk = {'problems': [{'num': '1.1', 'body': [{'t': 'p', 'md': 'E'}]}]}
    overlay = {'problems': [{'num': '1.5', 'body': [{'i': 0, 'a': ba, 'md': '中'}]}]}
    out = _merge_chunk_overlay(chunk, overlay)
    assert out['problems'][0]['body'][0]['md'] == 'E', '題號漂移 → 原 problem 不變'
    assert out['problems'][0]['num'] == '1.1'

    # 題號相符時正向套用（對照，證明 num 守衛不過度拒絕）
    chunk2 = {'problems': [{'num': '1.1', 'body': [{'t': 'p', 'md': 'E'}]}]}
    overlay2 = {'problems': [{'num': '1.1', 'body': [{'i': 0, 'a': ba, 'md': '中'}]}]}
    out2 = _merge_chunk_overlay(chunk2, overlay2)
    assert out2['problems'][0]['body'][0]['md'] == '中', '題號相符 → 套用中譯'

    # title-only chunk（無 'body' 鍵）遇帶 body 的 overlay → 雙守衛擋下，不崩潰
    chunk3 = {'title': 'T'}
    overlay3 = {'body': [{'i': 0, 'md': '中'}]}
    out3 = _merge_chunk_overlay(chunk3, overlay3)
    assert 'body' not in out3, '無 body 的 chunk 不該被 overlay body 憑空塞入'
    assert out3['title'] == 'T', 'title-only chunk 不崩潰'

    # chunk.problems 為空 list（falsy）遇帶 problems 的 overlay：`chunk.get('problems')` 守衛
    # 須擋下，不得把 overlay 的整題憑空塞進來（否則目錄會多出原書沒有的幽靈題）。
    chunk4 = {'problems': []}
    overlay4 = {'problems': [{'num': '1.1', 'body': [{'i': 0, 'md': '中'}]}]}
    out4 = _merge_chunk_overlay(chunk4, overlay4)
    assert out4['problems'] == [], '空 problems list 不該被 overlay problems 撐起'

    # title overlay 的 zh / bi 兩態契約：zh 就地替換、bi 寫 title_zh 並保留原 title。
    # 缺這條，「bi 模式誤覆蓋原 title」這種並陳破壞會無人察覺。
    assert _merge_chunk_overlay({'title': 'Old'}, {'title': '新'})['title'] == '新', 'zh: title 就地替換'
    bi_t = _merge_chunk_overlay({'title': 'Old'}, {'title': '新'}, bilingual=True)
    assert bi_t['title'] == 'Old' and bi_t['title_zh'] == '新', 'bi: 保留原 title、寫 title_zh'

    # 輸入 chunk 必須原封未動（淺拷貝 dict(chunk) + 對 body/problems 各自重建新 list）。
    # 漂移防護建立在「overlay 套用是純函式」上：若原地改 chunk，mtime cache 會回存髒 dict，
    # 下次讀同章不同 lang 會見到上次 merge 的殘留。釘住正向套用後原 chunk 的 md 仍是英文原文。
    orig = {'body': [{'t': 'p', 'md': 'E'}],
            'problems': [{'num': '1.1', 'body': [{'t': 'p', 'md': 'E'}]}]}
    ov = {'body': [{'i': 0, 'a': ba, 'md': '中'}],
          'problems': [{'num': '1.1', 'body': [{'i': 0, 'a': ba, 'md': '中'}]}]}
    _merge_chunk_overlay(orig, ov)
    assert orig['body'][0]['md'] == 'E', 'merge 不得原地污染輸入 chunk body'
    assert orig['problems'][0]['body'][0]['md'] == 'E', 'merge 不得原地污染輸入 problem body'
    print('✓ 題號漂移忽略 + title-only/空 problems 守衛 + zh/bi title + 輸入純函式不污染')


if __name__ == '__main__':
    test_overlay_anchor_golden_and_properties()
    test_patch_blocks_anchor_mismatch_keeps_english()
    test_patch_blocks_anchor_match_applies()
    test_patch_blocks_oob_and_nonint_i_skip()
    test_patch_blocks_bilingual_writes_zh_field()
    test_merge_chunk_overlay_problem_num_drift_skips()
    print('\n全部通過 ✅')
