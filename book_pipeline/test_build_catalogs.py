"""build_catalogs 純函式單元測試（無外部依賴、全 hermetic、synthetic 輸入）：
    uv run python -m book_pipeline.test_build_catalogs

守的契約 = build_catalogs 決定 catalog id「是否產生、是否去重」的三個 leading-anchor
與正規化函式。它們是 LLM↔確定性碼 的關鍵邊界：caption/id 由上游 OCR/audit/LLM
產生（變異大），這三個函式判定該不該把它當成一個正式可索引的圖/表編號。
寬鬆一格就會：
  - 把句中引用『See Figure 1.2』誤當本 block 編號 → reader 目錄張冠李戴跳轉；
  - dash/dot 不一致 → 同一張圖出現兩條 catalog 條目（去重失效）；
  - alias 的 type/前綴不符放行 → 圖被列進表目錄（反之亦然）。
35 個 catalog_overrides + reader 目錄跳轉全靠這幾個函式的精確語義，原本零測試。
"""

from book_pipeline.build_catalogs import (
    _caption_labels,
    _canonical_catalog_id,
    _catalog_aliases,
)


def test_caption_labels_leading_anchor_only():
    """leading-anchor 守衛：只有 caption「開頭」的圖表編號才算本 block 的正式編號。

    這是 catalog id 語義純度的命脈。caption 文字常含對其他圖表的「引用」
    （'See Figure 1.2', 'as in Table 2.3'）——這些是參照，不是本 block 的編號。
    若把句中引用也抽成 id，reader 目錄會把這個 block 掛到別人的編號上，
    點目錄就跳到錯的地方（張冠李戴）。守衛靠 leading_offset：第一個 match
    的起點必須落在「去掉前導空白後的開頭」，否則整條回 []。
    """
    # 句中引用：'See ' 在前，Figure 1.2 不在 leading → 必須整條回 []，
    # 否則就是把別人的編號偷來當自己的。
    assert _caption_labels('See Figure 1.2 here') == [], '句中引用不得被當成本 block 編號'

    # 更尖的 leading 邊界：連「一個非空白字元在前」都不算 leading。
    # '(Figure 1.2)' 的左括號使 match 起點 > leading_offset → 整條回 []。
    # 這證明守衛不是只擋 'See ' 這種長前綴，而是嚴格要求編號落在去前導空白後的第 0 位。
    assert _caption_labels('(Figure 1.2) x') == [], '編號前有任何非空白字元（左括號）即非 leading，須回 []'

    # leading 正式 caption：開頭就是 Figure 1.2 → 抽出單筆。
    out = _caption_labels('Figure 1.2 The thing')
    assert out == [('figure', 'fig', '1.2', 'Figure 1.2: The thing')], f'leading caption 應抽出單筆，得 {out}'

    # 縮寫 'Fig.' 與小寫 'figure'（OCR 大小寫/縮寫變異大）仍須抽出，且 display 正規化為 'Figure'。
    assert _caption_labels('Fig. 1.2 abc') == [('figure', 'fig', '1.2', 'Figure 1.2: abc')], "'Fig.' 縮寫須抽出並正規化 display"
    assert _caption_labels('figure 1.2 abc') == [('figure', 'fig', '1.2', 'Figure 1.2: abc')], '小寫 figure 須抽出'

    # 空字串（caption 缺失）→ []，consumer 端 block.get('caption','') 永不 None，但守空無妨。
    assert _caption_labels('') == [], '空 caption 須回 []'

    # 裸章號（Figure 3，無小數段）：CAT_NUM_PATTERN 要求至少一個 [.\-] 分隔的數字段，
    # 純章號不是可索引的圖表編號（會把「第 3 章的某圖」誤建成編號 3 的假條目）→ 回 []。
    assert _caption_labels('Figure 3') == [], '裸章號（無小數）不是可索引圖表編號'

    # 多標 caption：一張抽出的圖可能合法含多個 labeled 子圖；leading 第一個成立後，
    # 其餘標也一併收（後續 alias 指同一 anchor）。順序須照在 caption 中的出現位置。
    # 此處同時釘住「非 leading 標的 display tail 取至下一個 match 起點」的切分語義——
    # 'shows it' 屬 fig 1.2、'lists it' 屬 tbl 2.3，不可串成一坨或張冠李戴。
    multi = _caption_labels('Figure 1.2 shows it. Table 2.3 lists it.')
    assert multi == [
        ('figure', 'fig', '1.2', 'Figure 1.2: shows it'),
        ('table', 'tbl', '2.3', 'Table 2.3: lists it'),
    ], f'多標應依出現順序回多筆，得 {multi}'

    # 跨型別且 Table 在前的多標：證明排序是依 caption 中出現位置（非依 fig→tbl 固定枚舉序），
    # 且第一筆即 leading（Table 落在開頭）才放行整條。tail 切分仍各自正確。
    cross = _caption_labels('Table 2.3 list and Figure 1.2 tail')
    assert cross == [
        ('table', 'tbl', '2.3', 'Table 2.3: list and'),
        ('figure', 'fig', '1.2', 'Figure 1.2: tail'),
    ], f'跨型別多標須依出現位置排序、各自切 tail，得 {cross}'

    # 前導空白不破壞 leading 判定：lstrip 後開頭即編號仍算 leading。
    ws = _caption_labels('   Figure 1.2 thing')
    assert ws == [('figure', 'fig', '1.2', 'Figure 1.2: thing')], f'前導空白後仍算 leading，得 {ws}'

    # caption 內的 dash 編號（Figure 1-2）在抽出時即正規化成 dot（1.2），
    # 確保同一圖不論 caption 用 dash 或 dot 都產生同一 id（去重前提）。
    dash = _caption_labels('Figure 1-2 caption')
    assert dash == [('figure', 'fig', '1.2', 'Figure 1.2: caption')], f'caption dash 編號應正規化為 dot，得 {dash}'

    print('✓ _caption_labels：leading-anchor 守衛 + 裸章號排除 + 多標 + dash 正規化')


def test_canonical_catalog_id_dash_to_dot():
    """catalog id 正規化：dash 編號 → dot，但技術 fallback id 原樣保留。

    catalog 全書去重靠 id 字串相等。OCR/audit 來源時而用 '1-2' 時而用 '1.2'
    表同一圖；若不統一，同一圖會留兩條目。_canonical_catalog_id 把
    'fig-'/'tbl-' 前綴後的編號段 dash→dot 正規化。
    但 FALLBACK_ID_RE（fig-ch03-... / app... 這種技術 anchor id）不可正規化——
    那是 reader DOM anchor 的衍生形，其中的 dash 是結構分隔，動了會破壞跳轉。
    """
    # dash 編號 → dot：去重正確性的核心。
    assert _canonical_catalog_id('tbl-1-2') == 'tbl-1.2', 'dash 編號須正規化為 dot'

    # 已是 dot：冪等不變。
    assert _canonical_catalog_id('fig-1.2') == 'fig-1.2', '已 dot 的 id 應不變（冪等）'

    # fallback 技術 id（FALLBACK_ID_RE 命中）：原樣保留，絕不誤把結構 dash 改成 dot。
    # FALLBACK_ID_RE = ^(?:fig|tbl|eq)-(?:ch\d{2}|app[^-]+)(?:-|$)，兩條分支都要守：
    assert _canonical_catalog_id('fig-ch03-body-5') == 'fig-ch03-body-5', 'ch 章節 fallback 技術 id 須原樣保留'
    # app 分支（appendix anchor）同樣不可正規化——'appA-body-1' 內的 dash 是結構分隔，
    # 動了會破壞 reader DOM anchor 跳轉。只測 ch 不測 app 會漏掉半條 FALLBACK_ID_RE。
    assert _canonical_catalog_id('tbl-appA-body-1') == 'tbl-appA-body-1', 'app 附錄 fallback 技術 id 須原樣保留'

    # 多段 dash 也全部轉 dot（如 'fig-1-2-3'）。
    assert _canonical_catalog_id('fig-1-2-3') == 'fig-1.2.3', '多段 dash 應全轉 dot'

    # en-dash（OCR 常把 hyphen 辨成 U+2013）也須一併轉 dot，否則同圖 dash/en-dash 兩條目。
    assert _canonical_catalog_id('fig-1–2') == 'fig-1.2', 'en-dash 編號須一併正規化為 dot'

    # fig- 前綴但編號段無 dash（'fig-1'）：rest='1' 無可替換 → 原樣回（冪等、不誤動）。
    assert _canonical_catalog_id('fig-1') == 'fig-1', 'fig- 前綴但無 dash 的編號須冪等不變'

    # 無 fig-/tbl- 前綴（如 eq id）：函式不碰，原樣回。eq-3-4 走 FALLBACK 否定分支與「無前綴」
    # 雙重不碰；用 eq-3-4（dash 段）確保不會被誤正規化成 eq-3.4。
    assert _canonical_catalog_id('eq-3-4') == 'eq-3-4', '非 fig/tbl 前綴 id 不被正規化'

    print('✓ _canonical_catalog_id：dash→dot + 冪等 + fallback/eq 原樣保留')


def test_catalog_aliases_type_prefix_consistency():
    """LLM 直餵的 catalog_aliases 高信任輸入守衛：type 與 id 前綴須一致。

    catalog_aliases 由 LLM 產出、直接進目錄。若 type='table' 卻 id='fig-1.2'
    （前綴矛盾），放行就會把一張圖列進「表目錄」。守衛要求
    figure↔fig-、table↔tbl- 嚴格對應，否則丟棄該 alias。另需擋掉非 list 容器、
    缺 id 的殘缺 alias。
    """
    # 前綴/type 矛盾（id 是 fig-，type 卻說 table）→ 過濾掉，回 []。
    mismatch = _catalog_aliases({'catalog_aliases': [{'id': 'fig-1.2', 'type': 'table'}]}, 'anc', 'figure')
    assert mismatch == [], '前綴與 type 不符的 alias 須被過濾（防圖混入表目錄）'

    # catalog_aliases 不是 list（LLM 給了 dict）→ 容器型別不符直接回 []。
    not_list = _catalog_aliases({'catalog_aliases': {'id': 'fig-1.2'}}, 'anc', 'figure')
    assert not_list == [], 'catalog_aliases 非 list 時須回 []'

    # catalog_aliases 缺鍵（block 根本沒這欄）→ raw = [] → 回 []，不得 KeyError。
    assert _catalog_aliases({}, 'anc', 'figure') == [], '缺 catalog_aliases 欄須回 []（不炸）'
    # 值為 None（LLM 顯式給 null）→ `or []` 兜底 → 回 []。
    assert _catalog_aliases({'catalog_aliases': None}, 'anc', 'figure') == [], 'catalog_aliases=None 須回 []'

    # list 內元素不是 dict（LLM 給了裸字串 'fig-1.2'）→ 該元素跳過，回 []。
    # 不可對 str 呼叫 .get 而 AttributeError。
    not_dict = _catalog_aliases({'catalog_aliases': ['fig-1.2']}, 'anc', 'figure')
    assert not_dict == [], 'alias 元素非 dict（裸字串）須被跳過'

    # 缺 id 的 alias → 跳過（無 id 無法當索引鍵）。
    no_id = _catalog_aliases({'catalog_aliases': [{'type': 'figure'}]}, 'anc', 'figure')
    assert no_id == [], '缺 id 的 alias 須被跳過'

    # id 只有空白 → strip 後為空 → 視同缺 id 跳過。
    ws_id = _catalog_aliases({'catalog_aliases': [{'id': '   ', 'type': 'figure'}]}, 'anc', 'figure')
    assert ws_id == [], '空白 id 須被當缺 id 跳過'

    # type 既非 figure 也非 table（LLM 給 'equation'）→ 不在白名單 → 過濾。
    # catalog 只有圖/表兩種目錄，equation 不該混入。
    bad_type = _catalog_aliases({'catalog_aliases': [{'id': 'fig-1.2', 'type': 'equation'}]}, 'anc', 'figure')
    assert bad_type == [], "type 非 figure/table（如 'equation'）須被過濾"

    # id 前綴既非 fig- 也非 tbl-（'eq-1.2'）即使 type=figure 也過濾——
    # 前綴是 reader 目錄分流的鍵，eq 前綴無法掛進圖/表目錄。
    bad_prefix = _catalog_aliases({'catalog_aliases': [{'id': 'eq-1.2', 'type': 'figure'}]}, 'anc', 'figure')
    assert bad_prefix == [], "id 前綴非 fig-/tbl-（如 eq-）須被過濾"

    # 合法 alias（fig- 配 figure）→ 收入，且關鍵欄位正確。
    legal = _catalog_aliases({'catalog_aliases': [{'id': 'fig-1.2', 'type': 'figure', 'caption': 'My Cap'}]}, 'anc', 'figure')
    assert len(legal) == 1, f'合法 alias 應被收入，得 {legal}'
    a = legal[0]
    assert a['id'] == 'fig-1.2', 'alias id 應保留'
    assert a['type'] == 'figure', 'alias type 應保留'
    assert a['anchor'] == 'anc', 'alias 須沿用傳入 anchor（指向同一 reader DOM 目標）'
    assert a['catalog_alias'] is True, 'alias 須標記 catalog_alias=True'
    assert a['caption'] == 'My Cap', '提供 caption 時須保留（不被 id 覆蓋）'

    # alias id 用 dash 編號（'fig-1-2'）→ 進來先經 _canonical_catalog_id 轉 dot，
    # 再做前綴檢查（轉後仍是 fig- 開頭）→ 收入且 id 已正規化為 'fig-1.2'。
    # 這釘住「正規化發生在前綴守衛之前」的順序，是 alias 與主目錄去重對齊的前提。
    dash_alias = _catalog_aliases({'catalog_aliases': [{'id': 'fig-1-2', 'type': 'figure'}]}, 'anc', 'figure')
    assert len(dash_alias) == 1 and dash_alias[0]['id'] == 'fig-1.2', f'alias dash id 須正規化為 dot 後收入，得 {dash_alias}'

    # 缺 type 時 default_type 接管，仍須與 id 前綴一致才放行：
    # default='figure' 配 id='tbl-1.2'（前綴 tbl）→ 矛盾，過濾。
    default_mismatch = _catalog_aliases({'catalog_aliases': [{'id': 'tbl-1.2'}]}, 'anc', 'figure')
    assert default_mismatch == [], 'alias 缺 type 時 default_type 須仍與 id 前綴一致，否則過濾'

    # 缺 type 但 default 與前綴一致（default='table' 配 id='tbl-1.2'）→ 收入並取 default type。
    default_ok = _catalog_aliases({'catalog_aliases': [{'id': 'tbl-1.2'}]}, 'anc', 'table')
    assert len(default_ok) == 1 and default_ok[0]['type'] == 'table', 'default_type 與前綴一致時應收入並取 default type'

    print('✓ _catalog_aliases：type/前綴一致性 + 非 list/缺 id 守衛 + default_type 接管')


if __name__ == '__main__':
    test_caption_labels_leading_anchor_only()
    test_canonical_catalog_id_dash_to_dot()
    test_catalog_aliases_type_prefix_consistency()
    print('\n全部通過 ✅')
