"""validate_rules 單元測試（無 pytest）：uv run python -m book_pipeline.test_validate_rules

validate_rules.validate 是『擋畸形 LLM 產物（extract_rules.yaml）進 parser』的第一道牆。
audit-book agent 產 yaml，確定性的 parser.py 消費它；每一條 gate 都對應 parser 會踩的雷：
  - section_re 對「N.M 純編號頁眉」命中且 title 空 → catalog/section 結構靜默全爛（thomas_calculus 慘案）
  - capture group 數錯 → parser.detect_heading 的 m.group(2) / split_problems 直接 IndexError（ogata_control drift）
  - 缺 required regex / 章欄位 → parser.compile_regexes 裸 KeyError 崩（schwartz_qft/arnold_ode）
  - idx 越界 / 章號跳號 → 切片靜默丟 block、ch03.json 被重複覆蓋
  - top-level key typo（漏 s）→ 真值落 default、inline 書整章切錯（silent corruption）
這些 LLM↔確定性碼契約邊界完全零測試；本檔逐類注入違規鎖死每條 gate 邏輯。
與 artifact-guard 互補：那邊掃真實 committed 檔，這邊用 synthetic 鎖每條 gate 的判斷。

hermetic 手法：validate(slug) 只碰兩個 module 全域——
  validate_rules.DATA_DIR（讀 <slug>/extract_rules.yaml）
  validate_rules.load_unified（讀 unified content_list，回 block list）
我們把 DATA_DIR 重導到 tmp dir、用 stub 取代 load_unified 回足長 block list，
finally 全還原，絕不污染真實 mineru_data / 真實狀態。
"""

import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from book_pipeline import validate_rules


# ── 共用：完全合規的 rules 模板（仿真實 strang_linalg，已知 validate 會回 0）─────────
# 每個違規測試從這份 deep copy 出來、只改要驗的那一欄，確保「唯一變因」隔離。
def _clean_rules() -> dict:
    return {
        'slug': 'fixture_book',
        'title': 'Fixture Linear Algebra',
        'author': 'Test Author',
        'edition': '1st',
        'subject': 'Math',
        'publisher': 'Test Press',
        'language': 'en',
        'filter_types': ['header', 'page_number', 'footer'],
        'ignore_image_content': True,
        'ignore_chart_content': True,
        'body_start_page': 8,
        'appendices_start_page': 400,
        'bibliography_start_page': None,
        'index_start_page': 450,
        'inline_problems': True,  # 設 true → 跳過 inline 一致性掃描（那段非本檔焦點）
        'chapters': [
            {
                'num': 1, 'title': 'Chapter One', 'page_start': 8, 'page_end': 75,
                'chapter_title_block_idx': 10, 'problems_block_idx': None,
                'next_chapter_block_idx': 40,
            },
            {
                'num': 2, 'title': 'Chapter Two', 'page_start': 76, 'page_end': 147,
                'chapter_title_block_idx': 40, 'problems_block_idx': None,
                'next_chapter_block_idx': 80,
            },
        ],
        'appendices': [
            {'id': 'A', 'title': 'Appendix A', 'page_start': 400,
             'chapter_title_block_idx': 80},
        ],
        # \s+(.+)$ 強制 title 非空 → 不會對純編號頁眉命中（這是「正確」的 section_re）
        'section_re': r'^(\d+\.\d+)\s+(.+)$',
        'subsection_re': r'^(\S.*\S|\S)()$',  # 恰 2 group（第二個是空 group ()）
        'heading_priority': ['subsection_re', 'section_re'],
        'problem_start_re': r'^(\d+)\.\s+',  # 恰 1 group
        'problem_chapter_must_match': False,
        'problem_num_namespace_by_section': True,
        'problems_end_re': None,
        'solution_start_re': None,
        'equation_strip_dollar': True,
        'equation_label_re': r'\\tag\s*\{([0-9]+[a-z]?)\}',
        'example_start_re': None,
        'figure_caption_merge': False,
        'figure_caption_main_re': None,
        'known_missing_problems': [],
        'heading_text_level': 1,
    }


def _make_blocks(n: int) -> list[dict]:
    """回 n 個無害 block（type=text、空文字、text_level=2）。
    idx 邊界檢查靠 N=len(blocks)；inline 掃描靠每個 block 的 text，
    這裡 text 全空 → problem_start_re 不會命中，inline 段保持沉默。"""
    return [{'type': 'text', 'text': '', 'text_level': 2} for _ in range(n)]


def _run(rules: dict, n_blocks: int = 100, blocks: list[dict] | None = None):
    """把 rules dump 成 tmp/<slug>/extract_rules.yaml，stub load_unified，
    跑 validate 並擷取 stdout。回 (rc, output_text)。全程 hermetic、finally 還原全域。

    blocks 給定時直接用該 list 當 unified content（N=len(blocks)）——專供需要
    「真的注入一個 problem_start_re 命中 block」的 inline-consistency 正向測試；
    否則回 n_blocks 個無害空 block（idx 邊界用，inline 段保持沉默）。"""
    import yaml

    slug = rules.get('slug') or 'fixture_book'
    tmpdir = Path(tempfile.mkdtemp(prefix='vr_test_'))
    (tmpdir / slug).mkdir(parents=True, exist_ok=True)
    (tmpdir / slug / 'extract_rules.yaml').write_text(
        yaml.safe_dump(rules, allow_unicode=True, sort_keys=False)
    )

    stub = blocks if blocks is not None else _make_blocks(n_blocks)
    orig_data_dir = validate_rules.DATA_DIR
    orig_load = validate_rules.load_unified
    try:
        validate_rules.DATA_DIR = tmpdir
        validate_rules.load_unified = lambda s: stub
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = validate_rules.validate(slug)
        return rc, buf.getvalue()
    finally:
        validate_rules.DATA_DIR = orig_data_dir
        validate_rules.load_unified = orig_load


def _run_raw_yaml(yaml_text: str, slug: str = 'fixture_book', n_blocks: int = 100):
    """直接寫原始 yaml 文字（給需要繞過 dict→yaml 正規化的 case，如 typo key 名）。"""
    tmpdir = Path(tempfile.mkdtemp(prefix='vr_test_'))
    (tmpdir / slug).mkdir(parents=True, exist_ok=True)
    (tmpdir / slug / 'extract_rules.yaml').write_text(yaml_text)

    orig_data_dir = validate_rules.DATA_DIR
    orig_load = validate_rules.load_unified
    try:
        validate_rules.DATA_DIR = tmpdir
        validate_rules.load_unified = lambda s: _make_blocks(n_blocks)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = validate_rules.validate(slug)
        return rc, buf.getvalue()
    finally:
        validate_rules.DATA_DIR = orig_data_dir
        validate_rules.load_unified = orig_load


# ── 自驗：先確認模板乾淨（否則所有「只改一欄」的隔離前提就破了）──────────────────────
def test_clean_template_passes_baseline():
    rc, out = _run(_clean_rules())
    assert rc == 0, f'乾淨模板應 rc==0，否則隔離前提破。實得 rc={rc}\n{out}'
    print('✓ baseline：完全合規模板 → rc==0（隔離前提成立）')


# ── 1. section_re 純編號空 title（最高價值單條：thomas_calculus 級靜默災難）──────────
def test_section_re_empty_title_detected():
    """LLM 若把 section_re 寫成 '^(\\d+\\.\\d+)\\s*(.*)$'，\\s*(.*)$ 允許 title 空，
    則「14.1」這種純編號頁眉 block 會被當成一個無標題 section → catalog 長出空標題章節、
    後續 section 結構整片錯位（thomas_calculus 慘案的根因）。
    validate 用探針樣本 ('1.1','1.1 ','4.1','4.1 ','10.5 ') 涵蓋含/不含尾空白，
    任一命中且 group(2) strip 後為空 → 報錯。這是 silent-corruption 最隱蔽的一條。"""
    r = _clean_rules()
    r['section_re'] = r'^(\d+\.\d+)\s*(.*)$'  # \s* + (.*)$：致命的「允許空 title」寫法
    rc, out = _run(r)
    assert rc == 1, f'空 title section_re 應被擋，實得 rc={rc}\n{out}'
    assert '純編號 block' in out, f'訊息應點名「純編號 block」，實得：\n{out}'
    assert '命中且' in out and 'title 為空' in out, f'訊息應說「命中且 title 為空」：\n{out}'
    print('✓ section_re 允許空 title（純編號頁眉污染）→ rc==1 + 點名純編號/空 title')


# ── 2. capture group 數（對應 parser m.group(2) IndexError；ogata_control 實證 drift）──
def test_group_count_violations():
    """parser.detect_heading 取 m.group(2) 當 title、split_problems 取 group(1) 當題號。
    LLM drift 改了 group 數（多/少一個括號）→ parser 直接 IndexError 崩。
    validate 強制 section_re/subsection_re 恰 2 group、problem_start_re/example_start_re 恰 1 group。"""
    # (a) section_re 只 1 group：'^(\d+)\.' → 缺 title group，parser m.group(2) 會 IndexError
    r = _clean_rules()
    r['section_re'] = r'^(\d+)\.'
    rc, out = _run(r)
    assert rc == 1, f'section_re 1 group 應被擋，實得 rc={rc}'
    assert 'section_re' in out and '2 個 capture group' in out, \
        f'訊息應說 section_re 須恰好 2 capture group：\n{out}'
    print('✓ section_re 只 1 group → rc==1 + 「須恰好 2 capture group」')

    # (b) problem_start_re 2 group：split_problems 期待單一題號 group
    r = _clean_rules()
    r['problem_start_re'] = r'^(\d+)\.(\d+)\s+'  # 2 group，違反「恰 1」
    rc, out = _run(r)
    assert rc == 1, f'problem_start_re 2 group 應被擋，實得 rc={rc}'
    assert 'problem_start_re' in out and '1 個 capture group' in out, \
        f'訊息應說 problem_start_re 須恰好 1 capture group：\n{out}'
    print('✓ problem_start_re 2 group → rc==1 + 「須恰好 1 capture group」')

    # (c) example_start_re 2 group：同 problem_start，期待單一 group
    r = _clean_rules()
    r['example_start_re'] = r'^Example\s+(\d+)\.(\d+)'  # 2 group
    rc, out = _run(r)
    assert rc == 1, f'example_start_re 2 group 應被擋，實得 rc={rc}'
    assert 'example_start_re' in out and '1 個 capture group' in out, \
        f'訊息應說 example_start_re 須恰好 1 capture group：\n{out}'
    print('✓ example_start_re 2 group → rc==1 + 「須恰好 1 capture group」')


# ── 3. 缺 required top-level key / 缺章欄位 / heading_text_level 非法 ──────────────────
def test_missing_required_and_chapter_keys():
    """parser.compile_regexes 直接 R['section_re'] / R['equation_label_re']，缺 key → 裸 KeyError
    （schwartz_qft/arnold_ode 已實證 committed 缺 key）。缺章欄（problems_block_idx /
    next_chapter_block_idx）→ 切片用 None 崩或整章題目丟失。heading_text_level 非 ≥1 整數 → MinerU
    heading 選層錯亂。三類都該被 validate 擋下。"""
    # (a) 缺 top-level section_re 與 equation_label_re
    r = _clean_rules()
    del r['section_re']
    del r['equation_label_re']
    rc, out = _run(r)
    assert rc == 1, f'缺 required key 應被擋，實得 rc={rc}'
    assert '缺 top-level key: section_re' in out, f'應點名缺 section_re：\n{out}'
    assert '缺 top-level key: equation_label_re' in out, f'應點名缺 equation_label_re：\n{out}'
    print('✓ 缺 top-level section_re/equation_label_re → rc==1 + 逐一點名')

    # (b) 某 chapter 缺 problems_block_idx / next_chapter_block_idx
    r = _clean_rules()
    del r['chapters'][0]['problems_block_idx']
    del r['chapters'][0]['next_chapter_block_idx']
    rc, out = _run(r)
    assert rc == 1, f'缺章欄應被擋，實得 rc={rc}'
    assert 'chapter[0] 缺 problems_block_idx' in out, f'應點名 chapter[0] 缺 problems_block_idx：\n{out}'
    assert 'chapter[0] 缺 next_chapter_block_idx' in out, \
        f'應點名 chapter[0] 缺 next_chapter_block_idx：\n{out}'
    print('✓ chapter[0] 缺 problems_block_idx/next_chapter_block_idx → rc==1 + 逐一點名')

    # (c) heading_text_level=[2, null] — None 不是 ≥1 整數
    r = _clean_rules()
    r['heading_text_level'] = [2, None]
    rc, out = _run(r)
    assert rc == 1, f'heading_text_level 含 null 應被擋，實得 rc={rc}'
    assert 'heading_text_level' in out and '整數' in out, \
        f'訊息應說 heading_text_level 須為 ≥1 整數：\n{out}'
    print('✓ heading_text_level=[2,null] → rc==1 + 「須為 ≥1 整數」')


# ── 4. idx 邊界 + 章號連續 ──────────────────────────────────────────────────────────
def test_idx_bounds_and_chapter_numbering():
    """idx 是 parser 切 block 用的座標：越界 → 切片靜默丟 block；problems_block_idx 不在
    (cti,nci) → 抓錯區段；章號跳號 → f-string 章編號 format / ch{n}.json 覆蓋錯亂。
    全屬 silent-corruption（切錯不報、ch03.json 被覆蓋）。"""
    N = 100  # 與 _run 預設 n_blocks 一致

    # (a) chapter_title_block_idx 越界（>=N，上界）
    r = _clean_rules()
    r['chapters'][0]['chapter_title_block_idx'] = N  # 等於 N → 出界 [0,N)
    r['chapters'][0]['next_chapter_block_idx'] = None  # 避免連帶觸發 cti>=nci 模糊判定
    rc, out = _run(r, n_blocks=N)
    assert rc == 1, f'cti 越界應被擋，實得 rc={rc}'
    assert f'chapter_title_block_idx={N} 超出 [0,{N})' in out, \
        f'訊息應精準說 cti={N} 超出 [0,N)：\n{out}'
    print('✓ chapter_title_block_idx >= N → rc==1 + 「超出 [0,N)」')

    # (a2) chapter_title_block_idx 負值（下界）——上界守了不代表下界守，N.M 兩端都要鎖。
    #      cti<0 → parser 用負索引從尾端切 block，靜默抓錯整章標題（比越界崩更陰）。
    r = _clean_rules()
    r['chapters'][0]['chapter_title_block_idx'] = -1
    r['chapters'][0]['next_chapter_block_idx'] = None
    rc, out = _run(r, n_blocks=N)
    assert rc == 1, f'cti 負值應被擋，實得 rc={rc}'
    assert f'chapter_title_block_idx=-1 超出 [0,{N})' in out, \
        f'訊息應點名 cti=-1 超出下界：\n{out}'
    print('✓ chapter_title_block_idx < 0 → rc==1 + 「超出 [0,N)」（下界也鎖）')

    # (a3) next_chapter_block_idx 越界（>N，上界）。合法區間是 (0,N]——nci 可等於 N（章尾恰到末
    #      block 後），但 >N → 切片右界出界、靜默吞掉尾端整段 block。只測 cti 不測 nci 會漏這條。
    r = _clean_rules()
    r['chapters'][1]['next_chapter_block_idx'] = N + 1
    rc, out = _run(r, n_blocks=N)
    assert rc == 1, f'nci 越界應被擋，實得 rc={rc}'
    assert f'next_chapter_block_idx={N + 1} 超出 (0,{N}]' in out, \
        f'訊息應點名 nci 超出 (0,N]：\n{out}'
    print('✓ next_chapter_block_idx > N → rc==1 + 「超出 (0,N]」')

    # (a4) cti >= nci（區間退化/反序）。cti=nci 時 (cti,nci) 為空集 → 整章題目掃描範圍歸零、
    #      靜默丟光。這條與 cti/nci 各自越界正交，是「兩端皆在界內但順序錯」的獨立守衛。
    r = _clean_rules()
    r['chapters'][0]['chapter_title_block_idx'] = 40
    r['chapters'][0]['next_chapter_block_idx'] = 40  # cti == nci
    r['chapters'][0]['problems_block_idx'] = None
    rc, out = _run(r, n_blocks=N)
    assert rc == 1, f'cti>=nci 應被擋，實得 rc={rc}'
    assert 'chapter_title_block_idx(40) >= next_chapter_block_idx(40)' in out, \
        f'訊息應點名 cti>=nci：\n{out}'
    print('✓ cti == nci（區間退化）→ rc==1 + 「cti >= nci」')

    # (b) problems_block_idx 不在 (cti, nci)
    r = _clean_rules()
    r['chapters'][0]['chapter_title_block_idx'] = 10
    r['chapters'][0]['next_chapter_block_idx'] = 40
    r['chapters'][0]['problems_block_idx'] = 5  # < cti，不在 (10,40)
    rc, out = _run(r, n_blocks=N)
    assert rc == 1, f'pbi 不在區間應被擋，實得 rc={rc}'
    assert 'problems_block_idx' in out and '不在' in out, \
        f'訊息應說 problems_block_idx 不在 (cti,nci)：\n{out}'
    print('✓ problems_block_idx 不在 (cti,nci) → rc==1 + 「不在 (cti,nci)」')

    # (c) 章號跳號 [1,2,4]
    r = _clean_rules()
    r['chapters'].append({
        'num': 4, 'title': 'Chapter Four', 'page_start': 200, 'page_end': 250,
        'chapter_title_block_idx': 80, 'problems_block_idx': None,
        'next_chapter_block_idx': 90,
    })  # 原本 [1,2] → 變 [1,2,4]，缺 3
    rc, out = _run(r, n_blocks=N)
    assert rc == 1, f'章號跳號應被擋，實得 rc={rc}'
    assert '章號跳號' in out, f'訊息應說章號跳號：\n{out}'
    print('✓ 章號跳號 [1,2,4] → rc==1 + 「章號跳號」')

    # (d) num 為字串 '3' — 章號連續比對用整數 range，字串 num 應被連續守衛抓到
    r = _clean_rules()
    r['chapters'][1]['num'] = '2'  # 字串而非 int → nums=[1,'2'] != range(1,3)
    rc, out = _run(r, n_blocks=N)
    assert rc == 1, f'字串 num 應被擋（破壞整數連續性），實得 rc={rc}'
    assert '章號跳號' in out, f'字串 num 應被章號連續守衛抓到：\n{out}'
    print('✓ num 為字串 → rc==1 + 章號連續守衛抓到')


# ── 5. 未知 top-level key + typo（typo 讓真值落 default → 靜默改 parser 行為）─────────
def test_unknown_toplevel_key_and_typo():
    """SCHEMA_KEYS 白名單 + 「未知 key」報錯的價值在於『接住 typo』：
    LLM 把 inline_problems 拼成 inline_problem（漏 s）→ 真正的 inline_problems 落 false default、
    inline 書整章切錯（silent corruption）。validate 把 typo'd key 當「未知 key」報出來，
    迫使人類發現拼錯。這條同時守『無關的雜 key（series）』與『致命 typo』。"""
    # (a) 純粹多餘 key 'series'
    r = _clean_rules()
    r['series'] = 'Some Series'
    rc, out = _run(r)
    assert rc == 1, f"未知 key 'series' 應被擋，實得 rc={rc}"
    assert '未知 top-level key' in out and 'series' in out, \
        f"訊息應列出未知 key 'series'：\n{out}"
    print("✓ 多餘 key 'series' → rc==1 + 列出未知 key")

    # (b) typo：inline_problem（漏 s）。真值 inline_problems 缺 → 落 False default。
    #     真實災難：false default 讓 inline 書走非 inline 路徑、整章切錯。
    r = _clean_rules()
    del r['inline_problems']
    r['inline_problem'] = True  # typo
    rc, out = _run(r)
    assert rc == 1, f'typo inline_problem 應被當未知 key 報出，實得 rc={rc}'
    assert '未知 top-level key' in out and 'inline_problem' in out, \
        f"typo'd key 應被列為未知 key（接住拼錯）：\n{out}"
    print("✓ typo 'inline_problem'（漏 s）→ rc==1 + 被當未知 key 報出（接住 typo）")

    # (c) typo：problems_block_index（正確是 ..._idx）放 top-level。它非 SCHEMA_KEYS → 未知 key
    r = _clean_rules()
    r['problems_block_index'] = 30
    rc, out = _run(r)
    assert rc == 1, f'typo problems_block_index 應被擋，實得 rc={rc}'
    assert '未知 top-level key' in out and 'problems_block_index' in out, \
        f"typo'd key problems_block_index 應被列為未知 key：\n{out}"
    print("✓ typo 'problems_block_index' → rc==1 + 被當未知 key 報出")


# ── 6. filter_types 白名單 + 乾淨本通過（正反配對：證明 gate 精確、不過嚴）────────────
def test_filter_types_whitelist_and_clean_passes():
    """filter_types 是 parser 丟棄 block 的型別清單，超出 ALLOWED_FILTER 的值代表 LLM 幻覺型別、
    要嘛漏濾要嘛濾錯。但 gate 必須精確：若太嚴，合法 audit 產物被擋 → 阻 deploy。
    故正反配對——白名單外值 rc==1；完全合規本 rc==0。"""
    # 反例：filter_types 含白名單外的 'magic'
    r = _clean_rules()
    r['filter_types'] = ['header', 'magic']  # 'magic' 不在 ALLOWED_FILTER
    rc, out = _run(r)
    assert rc == 1, f"filter_types 含 'magic' 應被擋，實得 rc={rc}"
    assert '含未知值' in out, f"訊息應說 filter_types 含未知值：\n{out}"
    print("✓ filter_types 含白名單外值 'magic' → rc==1 + 「含未知值」")

    # 正例（控制組）：完全合規本必須 rc==0，否則 gate 過嚴會擋掉合法 audit 產物
    rc, out = _run(_clean_rules())
    assert rc == 0, f'完全合規 rules 應 rc==0（gate 不可過嚴），實得 rc={rc}\n{out}'
    assert '✅' in out, f'乾淨本應印 ✅ 摘要：\n{out}'
    print('✓ 完全合規 rules（含 chapters/合法 regex/heading_priority）→ rc==0')


# ── 7. heading_priority 必須恰為 [subsection_re, section_re]（parser heading 派發順序契約）──
def test_heading_priority_must_be_exact():
    """parser 依 heading_priority 決定先試 subsection 還是 section 命中。順序顛倒 →
    N.M section 被 subsection_re（通常更寬）先吃掉，整個 heading 層級對調、章節樹崩。
    乾淨模板把它設成唯一合法值 [subsection_re, section_re]，但那只是『正例』；
    這裡注入顛倒/缺項/含雜值三種畸形，鎖死『恰為此 list』的硬等值守衛——
    否則模組把守衛從 != 改成 in（放寬）也沒人會發現。"""
    for bad in (
        ['section_re', 'subsection_re'],     # 顛倒
        ['subsection_re'],                   # 缺一項
        ['subsection_re', 'section_re', 'x'],  # 多雜值
        [],                                  # 空
    ):
        r = _clean_rules()
        r['heading_priority'] = bad
        rc, out = _run(r)
        assert rc == 1, f'heading_priority={bad!r} 應被擋，實得 rc={rc}\n{out}'
        assert 'heading_priority 必須是 [subsection_re, section_re]' in out, \
            f'訊息應點名 heading_priority 契約（bad={bad!r}）：\n{out}'
    print('✓ heading_priority 非 [subsection_re, section_re]（顛倒/缺/雜/空）→ 全 rc==1')


# ── 8. regex 無法編譯（LLM 吐壞 pattern → parser.compile_regexes re.compile 直接炸）─────────
def test_uncompilable_regex_detected():
    """audit-book agent 偶爾吐語法壞掉的 regex（未閉合字元集/括號）。若沒這道牆，
    parser.compile_regexes 的 re.compile 會在 ingest 中途 raise，整本書卡死。
    必填 regex（section_re）與選填 regex（example_start_re）都要驗——選填那組是
    `if R.get(k):` 路徑（None 略過、有值才編），畸形值同樣要被接住。"""
    # (a) 必填 section_re 未閉合字元集
    r = _clean_rules()
    r['section_re'] = r'^(\d+\.\d+)[a-z'  # 未閉合 [
    rc, out = _run(r)
    assert rc == 1, f'壞 section_re 應被擋，實得 rc={rc}'
    assert 'section_re 編譯失敗' in out, f'訊息應說 section_re 編譯失敗：\n{out}'
    print('✓ section_re 無法編譯 → rc==1 + 「section_re 編譯失敗」')

    # (b) 選填 example_start_re 未閉合括號（走 R.get(k) 為真才編的分支）
    r = _clean_rules()
    r['example_start_re'] = r'^Example\s+(\d+'  # 未閉合 (
    rc, out = _run(r)
    assert rc == 1, f'壞 example_start_re 應被擋，實得 rc={rc}'
    assert 'example_start_re 編譯失敗' in out, f'訊息應說 example_start_re 編譯失敗：\n{out}'
    print('✓ example_start_re 無法編譯（選填分支）→ rc==1 + 「example_start_re 編譯失敗」')


# ── 9. known_missing_problems schema（list of {chapter:int, nums:[str]}）──────────────────
def test_known_missing_problems_schema():
    """known_missing_problems 是「已知書中缺漏題號」白名單，parser 拿它壓 QC 缺題告警。
    若 chapter 非 int / nums 非 list，parser 後續用 int 比對章號、迭代 nums 會型別崩。
    逐欄位驗：chapter='1'（字串）、nums='a'（字串非 list）、整條非 dict 三種畸形都要被擋。"""
    # (a) chapter 非 int + nums 非 list（同一條同時違兩欄 → 兩行錯）
    r = _clean_rules()
    r['known_missing_problems'] = [{'chapter': '1', 'nums': 'a'}]
    rc, out = _run(r)
    assert rc == 1, f'kmp 型別錯應被擋，實得 rc={rc}'
    assert 'known_missing_problems[0].chapter 不是 int' in out, \
        f'應點名 chapter 不是 int：\n{out}'
    assert 'known_missing_problems[0].nums 不是 list' in out, \
        f'應點名 nums 不是 list：\n{out}'
    print('✓ known_missing_problems chapter 非 int / nums 非 list → rc==1 + 逐欄點名')

    # (b) 整條非 dict（LLM 寫成裸字串）
    r = _clean_rules()
    r['known_missing_problems'] = ['ch1 missing 5']
    rc, out = _run(r)
    assert rc == 1, f'kmp 非 dict 應被擋，實得 rc={rc}'
    assert 'known_missing_problems[0] 不是 dict' in out, \
        f'應點名 [0] 不是 dict：\n{out}'
    print('✓ known_missing_problems 整條非 dict → rc==1 + 「不是 dict」')


# ── 10. appendix idx 不合法 + inline_problems 非 bool ──────────────────────────────────
def test_appendix_idx_and_inline_bool():
    """附錄也靠 chapter_title_block_idx 定位切點，越界/None → 切片靜默抓錯整段附錄。
    inline_problems 非 bool（LLM 寫 'yes'）→ `not inline` 對字串恆 False，inline 一致性
    掃描被整段跳過、該報的 inline 漏配靜默放行。兩條都是被現有測試忽略的真實守衛。"""
    # (a) appendix chapter_title_block_idx 越界
    r = _clean_rules()
    r['appendices'][0]['chapter_title_block_idx'] = 999  # >> N=100
    rc, out = _run(r)
    assert rc == 1, f'appendix idx 越界應被擋，實得 rc={rc}'
    assert 'appendix[0] chapter_title_block_idx 不合法' in out, \
        f'應點名 appendix[0] idx 不合法：\n{out}'
    print('✓ appendix chapter_title_block_idx 越界 → rc==1 + 「不合法」')

    # (a2) appendix chapter_title_block_idx 缺（None）——同條守衛接住 None
    r = _clean_rules()
    del r['appendices'][0]['chapter_title_block_idx']
    rc, out = _run(r)
    assert rc == 1, f'appendix idx 缺應被擋，實得 rc={rc}'
    assert 'appendix[0] chapter_title_block_idx 不合法' in out, \
        f'應點名 appendix[0] idx 不合法（None）：\n{out}'
    print('✓ appendix chapter_title_block_idx 缺（None）→ rc==1 + 「不合法」')

    # (b) inline_problems 非 bool（字串 'yes'）
    r = _clean_rules()
    r['inline_problems'] = 'yes'
    rc, out = _run(r)
    assert rc == 1, f"inline_problems='yes' 應被擋，實得 rc={rc}"
    assert 'inline_problems 必須是 bool' in out, \
        f'應點名 inline_problems 必須是 bool：\n{out}'
    print("✓ inline_problems 非 bool（'yes'）→ rc==1 + 「必須是 bool」")


# ── 11. inline 一致性正向 gate（最語意化的守衛：pbi=null 章內藏題卻沒標 inline）────────────
def test_inline_consistency_gate_fires_and_clean_silent():
    """這是 dogfood 反覆提到「inline 書整章靜默切錯」的那道防線，卻零正向測試。
    語意：某章 problems_block_idx=null（宣稱沒有獨立題目區）但 inline_problems=false，
    若該章 (cti,nci) 內竟有 block 命中 problem_start_re → 題目散在正文裡卻沒開 inline 模式，
    parser 會整章漏抓題目。本測試**真的注入一個命中 block**，斷言 gate 觸發；
    再對照組：同樣布局但 inline_problems=true → gate 沉默（rc 由其他欄決定，不該因此條紅）。

    對照組驗的是『gate 精確、不誤殺合法 inline 書』——這是正向 gate 最容易寫歪成
    『永遠不觸發』或『永遠觸發』的兩種假測試，正反配對才鎖得住。"""
    # 在 chapter[0] 的 (cti=10, nci=40) 區間內塞一個命中 '^(\d+)\.\s+' 的 block。
    blocks = _make_blocks(100)
    blocks[20] = {'type': 'text', 'text': '1. Find the derivative of f.', 'text_level': 2}

    # (a) inline_problems=false + 章內藏題 → gate 必觸發
    r = _clean_rules()
    r['inline_problems'] = False
    # chapter[0].problems_block_idx 已是 None（宣稱無獨立題區）→ 觸發掃描
    rc, out = _run(r, blocks=blocks)
    assert rc == 1, f'inline 不一致應被擋，實得 rc={rc}\n{out}'
    assert 'pbi=null 但章內有 problem_start_re 命中' in out, \
        f'訊息應點名 inline 一致性違規：\n{out}'
    print('✓ inline_problems=false 但 pbi=null 章內藏題 → rc==1 + inline 一致性報警')

    # (b) 對照組：同布局但 inline_problems=true → 此 gate 不該觸發（合法 inline 書放行）
    r = _clean_rules()
    r['inline_problems'] = True
    rc, out = _run(r, blocks=blocks)
    assert rc == 0, f'合法 inline 書（inline=true）應 rc==0，gate 不可誤殺：\n{out}'
    assert 'pbi=null 但章內有 problem_start_re 命中' not in out, \
        f'inline=true 時不該報 inline 一致性違規：\n{out}'
    assert '✅' in out, f'合法 inline 書應印 ✅：\n{out}'
    print('✓ 對照組 inline_problems=true（同布局）→ rc==0，gate 不誤殺合法 inline 書')


# ── 12. 載入前置守衛：檔不存在 / YAML 壞 / 非 mapping（validate 早退三閘）──────────────────
def test_load_guards_missing_malformed_nonmapping():
    """validate 最前面三道早退閘：檔不存在、YAML 解析失敗、根非 mapping。
    這三條若失守，後續 R['...'] / R.get() 會在 None 或 list 上爆 TypeError，
    錯誤訊息變成難解的 traceback 而非明確 gate 報告。逐一驗早退 + 訊息。"""
    import yaml as _yaml

    # (a) extract_rules.yaml 不存在（建了 <slug> dir 但不寫檔）
    tmpdir = Path(tempfile.mkdtemp(prefix='vr_test_'))
    (tmpdir / 'fixture_book').mkdir(parents=True, exist_ok=True)
    orig_dd, orig_lu = validate_rules.DATA_DIR, validate_rules.load_unified
    try:
        validate_rules.DATA_DIR = tmpdir
        validate_rules.load_unified = lambda s: _make_blocks(100)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = validate_rules.validate('fixture_book')
        assert rc == 1, f'缺檔應 rc==1，實得 {rc}'
        assert '缺 extract_rules.yaml' in buf.getvalue(), \
            f'應報缺 extract_rules.yaml：\n{buf.getvalue()}'
    finally:
        validate_rules.DATA_DIR, validate_rules.load_unified = orig_dd, orig_lu
    print('✓ 缺 extract_rules.yaml → rc==1 + 「缺 extract_rules.yaml」')

    # (b) YAML 語法壞（無法 safe_load）
    rc, out = _run_raw_yaml('slug: fixture_book\n: : : bad\n  - broken')
    assert rc == 1, f'壞 YAML 應 rc==1，實得 {rc}'
    assert 'YAML 載入失敗' in out, f'應報 YAML 載入失敗：\n{out}'
    print('✓ YAML 語法壞 → rc==1 + 「YAML 載入失敗」')

    # (c) 根非 mapping（頂層是 list）
    rc, out = _run_raw_yaml('- a\n- b\n')
    assert rc == 1, f'非 mapping 應 rc==1，實得 {rc}'
    assert '不是 mapping' in out, f'應報不是 mapping：\n{out}'
    print('✓ 頂層非 mapping（list）→ rc==1 + 「不是 mapping」')


if __name__ == '__main__':
    test_clean_template_passes_baseline()
    test_section_re_empty_title_detected()
    test_group_count_violations()
    test_missing_required_and_chapter_keys()
    test_idx_bounds_and_chapter_numbering()
    test_unknown_toplevel_key_and_typo()
    test_filter_types_whitelist_and_clean_passes()
    test_heading_priority_must_be_exact()
    test_uncompilable_regex_detected()
    test_known_missing_problems_schema()
    test_appendix_idx_and_inline_bool()
    test_inline_consistency_gate_fires_and_clean_silent()
    test_load_guards_missing_malformed_nonmapping()
    print('\n全部通過 ✅')
