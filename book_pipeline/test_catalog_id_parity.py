"""跨模組 anchor 一致性測試：uv run python -m book_pipeline.test_catalog_id_parity

守住 CLAUDE.md 明文契約：
  textbooks.corpus._ensure_catalog_ids 補的 block['id']（= reader 渲染出的 DOM anchor）
  必須與 book_pipeline.build_catalogs._scan_chunk 算出的 catalog entry['anchor'] 逐一相等。

為何高價值（silent-corruption）：
  reader 點目錄（catalog）跳轉靠 `#slug/kind/key` 捲到 DOM 元素 id=entry['anchor']；
  而那顆 DOM 元素的 id 是 corpus 在 `_load_chunk` 時 `_ensure_catalog_ids` 補進 block['id'] 後
  由前端 renderBlock 寫上去的。兩套 anchor 由**兩個獨立模組、兩段並行實作**算出：
    - corpus 端：_fallback_media_id / _leading_media_id / _dedupe_media_id（消費端，跑在 web 進程）
    - build 端：_anchor_id / _leading_caption / _scan_chunk 的去重迴圈（生產 catalogs.json）
  任一邊改規則而另一邊沒跟上，anchor 就漂移 → catalog 指向一個 DOM 裡不存在的 id →
  「點目錄跳不到圖」。這是靜默損毀：catalogs.json 自身合法、reader 自身不報錯，
  只有使用者點下去才發現跳轉失效。目前零交叉一致性守護，此檔補上。

涵蓋情境（同一 chunk 檔內並存，逼出去重交互作用 + 跨 source 邊界）：
  ① fig 有 caption 編號（'Figure 1.2 ...'）→ 語義 anchor fig-1.2
  ② fig 無 caption fallback（抽不出編號）→ 退化 anchor fig-<stem>-<idx>
  ③ eq 有 label → anchor eq-<stem>-<label>
  ④ p block 帶 leading-media（md 開頭 'Figure 1.2 ...'）→ 與 ① 撞號，吃去重後綴
  ⑤ 同編號去重（再來一個 'Figure 1.2'）→ 連鎖 --N 後綴
  ⑥ problem body 內又一個 'Figure 1.2' → **跨 body→problem source 邊界**仍接續 --N 鏈
     （seen 計數器在兩端皆 per-chunk、橫跨 body/problems/solutions，不是 per-source 重置）
  ⑦ problem body 內無 caption fig → fallback 帶 source 標記 fig-<stem>-prob0-<idx>
     （`source != 'body'` 分支：corpus._fallback_media_id ↔ build._fallback_anchor 的 `{source}-{idx}` 形式必須同字）
  ⑧ solution 內無 label eq → fallback eq-<stem>-sol0-<idx>（同 source 分支，另一型別）

為何 ⑥⑦⑧ 是最高含金量：原五情境全在 body，`source != 'body'` 這條 docstring 明示的
  「兩端並行實作的分歧點」與「跨 source 的 seen 計數同步」**完全未觸及**。題目／解答區的圖
  在真實書裡很常見；若兩端對 source 標記或跨 source 去重不同步，使用者「在題目裡點圖跳不到」
  正是典型靜默損毀——catalogs.json 合法、reader 不報錯，只有點下去才壞。

hermetic：synthetic chunk 寫進 tmp/<slug>/parsed/ch01.json，同步把
  corpus.DATA_DIR 與 build_catalogs.DATA_DIR 重導到 tmp，finally 還原。絕不碰真實 mineru_data。
"""
import json
import os
import shutil
import tempfile
from pathlib import Path

from book_pipeline import build_catalogs as bc
from textbooks import corpus

SLUG = 'parity_fixture'
STEM = 'ch01'

# 八情境 chunk。block 順序刻意讓 ①④⑤⑥ 四個 fig-1.2 撞號穿插（中間隔著 fallback fig 與 eq），
# 逼出 corpus.seen 與 build_catalogs.seen 兩套去重計數器在「跨 block 型別」**且跨 body/problem/solution
# source 邊界**下仍同步遞增。problems/solutions 區另逼出 `source != 'body'` 的 fallback 分支。
FIXTURE_CHUNK = {
    'body': [
        {'t': 'fig', 'caption': 'Figure 1.2 A simple circuit', 'src': 'a.jpg'},  # ① 語義 fig-1.2
        {'t': 'fig', 'src': 'b.jpg'},                                            # ② 無 caption → fallback
        {'t': 'eq', 'tex': 'x = 1', 'label': '1.5'},                            # ③ eq 有 label
        {'t': 'p', 'md': 'Figure 1.2 explanatory paragraph'},                    # ④ leading-media 撞 ①
        {'t': 'fig', 'caption': 'Figure 1.2 the same number again'},             # ⑤ 同編號再撞
    ],
    'problems': [
        {
            'num': '1',
            'body': [
                {'t': 'fig', 'caption': 'Figure 1.2 inside a problem', 'src': 'p.jpg'},  # ⑥ 跨 source 仍撞 ①
                {'t': 'fig', 'src': 'q.jpg'},                                            # ⑦ 無 caption → prob fallback
            ],
            'solution': [
                {'t': 'eq', 'tex': 'y = 2'},                                             # ⑧ 無 label → sol fallback
            ],
        },
    ],
}

# 黃金值（golden）：除了「兩端逐一相等」外，再釘死實際字串。
# 這擋掉一種隱形退化：兩套實作「同步漂移」到另一個彼此相等但語義已變的方案
# （例如都改成 fig-ch01-2.x），逐一比對仍會綠但 anchor 規則其實已被改壞。
# 釘死字串 → 任一規則變更（含善意重構）都必須有意識地更新本表，留下審查點。
#
# 注意 ⑥ 的 fig-1.2--3：body 已用掉 fig-1.2 / --1 / --2，problem body 的第四個撞號者
#   續鏈成 --3 → 證明 seen 計數器**橫跨 body→problem source 邊界不重置**。
# ⑦ fig-ch01-prob0-1：source='prob0'、idx=1（problem body 第二個 block）→ `prob0-1` 形式。
# ⑧ eq-ch01-sol0-0：source='sol0'、idx=0 → `sol0-0` 形式。兩者皆走 `source != 'body'` 分支。
EXPECTED_ANCHORS = [
    'fig-1.2', 'fig-ch01-1', 'eq-ch01-1.5', 'fig-1.2--1', 'fig-1.2--2',   # body ①②③④⑤
    'fig-1.2--3', 'fig-ch01-prob0-1', 'eq-ch01-sol0-0',                   # problem/solution ⑥⑦⑧
]


def _build_book(slug: str) -> dict:
    """build_catalogs.build_catalogs 需要 book.json 列出 chapters。
    這裡只給 ch01 一章，num=1 → stem ch01，與 FIXTURE_CHUNK 落點對齊。"""
    return {
        'slug': slug,
        'title': 'Parity Fixture',
        'chapters': [{'num': 1, 'title': 'Ch1'}],
        'appendices': [],
    }


def _setup_tmp() -> tuple[Path, Path, Path]:
    """建 tmp/<slug>/parsed/{book.json, ch01.json}，回 (tmp_root, orig_corpus_dir, orig_bc_dir)。"""
    tmp = Path(tempfile.mkdtemp(prefix='catalog_parity_'))
    parsed = tmp / SLUG / 'parsed'
    parsed.mkdir(parents=True)
    (parsed / f'{STEM}.json').write_text(
        json.dumps(FIXTURE_CHUNK, ensure_ascii=False), encoding='utf-8')
    (parsed / 'book.json').write_text(
        json.dumps(_build_book(SLUG), ensure_ascii=False), encoding='utf-8')
    orig_corpus = corpus.DATA_DIR
    orig_bc = bc.DATA_DIR
    corpus.DATA_DIR = tmp
    bc.DATA_DIR = tmp
    return tmp, orig_corpus, orig_bc


def _corpus_block_ids() -> list[tuple[str, str]]:
    """跑 corpus 消費路徑，回 [(block_t, assigned_id), ...]（document order）。

    用真實的 _ensure_catalog_ids（in-place mutate），再讀回 block['id']。
    只收「會渲染成 DOM anchor」的 block：fig/table/eq 一定有 id；
    p block 僅在 leading-media 命中時被補 id，未命中則無 id（不會渲染成媒體 anchor）。

    遍歷順序**必須對齊** build_catalogs._scan_chunk：body → 每個 problem 的 body → 該 problem 的
    solution（見 build_catalogs._scan_chunk 的 _walk_blocks 呼叫順序）。順序錯位 → 逐一比對失真。
    """
    chunk = json.loads(json.dumps(FIXTURE_CHUNK))  # 深拷貝，勿動 module 常數
    corpus._ensure_catalog_ids(chunk, STEM)
    out: list[tuple[str, str]] = []

    def _collect(blocks: list[dict]) -> None:
        for b in blocks:
            bid = (b.get('id') or '').strip()
            if bid:
                out.append((b['t'], bid))

    _collect(chunk.get('body', []))
    for prob in chunk.get('problems', []):
        _collect(prob.get('body', []))
        _collect(prob.get('solution', []))
    return out


def _build_entry_anchors() -> list[tuple[str, str]]:
    """跑 build_catalogs 生產路徑，回 [(entry_type, anchor), ...]（document order）。

    過濾 catalog_alias 條目：alias 是「同一張圖含多個 label」的衍生條目，
    共用同一 anchor、在 corpus 端沒有對應的獨立 block，故不納入逐一對齊。
    （本 fixture 每個 caption 僅單一 label，本就不產 alias；過濾是防禦性、語義清楚。）
    """
    entries = bc._scan_chunk(SLUG, STEM)
    return [(e['type'], e['anchor']) for e in entries if not e.get('catalog_alias')]


def test_anchor_parity_caption_fallback_eq_dedup():
    """corpus 補的 block id 與 build_catalogs 算的 entry anchor：逐一、含 --N 去重後綴、
    含 body/problem/solution 跨 source 邊界與 source 標記 fallback，皆相等。"""
    tmp, orig_corpus, orig_bc = _setup_tmp()
    try:
        corpus_ids = _corpus_block_ids()
        build_anchors = _build_entry_anchors()

        # 1) 數量一致：每個會渲染 DOM anchor 的 block 都該有恰一個 catalog entry 對應。
        assert len(corpus_ids) == len(build_anchors), (
            f'block→entry 對應數不符：corpus={corpus_ids} build={build_anchors}')

        # 2) 逐一相等（核心契約）：position-wise 兩端 anchor 必須字字相同。
        #    type 也比對，順手抓「同一 block 被兩端歸成不同類別」這種更隱蔽的漂移。
        #    這裡用**兩個獨立來源**交叉驗 build 的 type 欄：
        #      (a) corpus 端原始 block t（fig/table/eq/p；p=leading-media，語義必為 figure/table）
        #          ——這是與 anchor 字串完全無關的獨立訊號，不會因 cid==banchor 而恆真。
        #      (b) anchor 前綴（fig-/tbl-/eq-）的慣例。兩者都須與 build type 對齊。
        prefix_type = {'fig-': 'figure', 'tbl-': 'table', 'eq-': 'equation'}

        def _anchor_type(anchor: str) -> str:
            for pre, typ in prefix_type.items():
                if anchor.startswith(pre):
                    return typ
            raise AssertionError(f'非預期 anchor 前綴：{anchor!r}')

        def _block_t_type(ct: str, anchor: str) -> str:
            """由 corpus 原始 block t 推 build 語義 type（與 anchor 字串無關的獨立訊號）。
            p（leading-media）本身沒有 fig/tbl 之分，其語義由 _leading_media_id 寫出的前綴決定，
            故 p 退回看 anchor 前綴；fig/table/eq 則直接映射、完全不看 anchor。"""
            if ct == 'fig':
                return 'figure'
            if ct == 'table':
                return 'table'
            if ct == 'eq':
                return 'equation'
            if ct == 'p':  # leading-media：只可能是 fig-/tbl- 前綴
                assert anchor.startswith(('fig-', 'tbl-')), (
                    f'p block 的 leading-media anchor 非 fig/tbl 前綴：{anchor!r}')
                return 'figure' if anchor.startswith('fig-') else 'table'
            raise AssertionError(f'非預期 corpus block t：{ct!r}')

        for i, ((ct, cid), (bt, banchor)) in enumerate(zip(corpus_ids, build_anchors)):
            assert cid == banchor, (
                f'block#{i} anchor 漂移：corpus={cid!r} vs build_catalogs={banchor!r}\n'
                f'  → reader 會渲染 id={cid!r}，但 catalog 指向 {banchor!r} → 點目錄跳不到')
            # (a) 獨立訊號：corpus 原始 block t → build type
            assert _block_t_type(ct, cid) == bt, (
                f'block#{i} 型別漂移：corpus block t={ct!r}→{_block_t_type(ct, cid)!r} '
                f'vs build type={bt!r}（anchor {cid!r}）')
            # (b) 慣例訊號：anchor 前綴 → build type
            assert _anchor_type(cid) == bt, (
                f'block#{i} 前綴/型別不一致：anchor {cid!r}→{_anchor_type(cid)!r} '
                f'vs build type={bt!r}')

        # 3) 黃金值釘死：擋「兩端同步漂移到另一相等方案」的隱形退化。
        assert [a for _, a in build_anchors] == EXPECTED_ANCHORS, (
            f'anchor 規則已變：實得 {[a for _, a in build_anchors]}，'
            f'預期 {EXPECTED_ANCHORS}（若為刻意改規則，請同步更新本表並確認兩端皆改）')

        # 4) 去重後綴明確驗證：**四個**撞號 fig-1.2 連鎖（含跨 body→problem source 邊界的 --3），
        #    必須是 fig-1.2 / --1 / --2 / --3，且 corpus 端與 build 端逐字相同。
        #    --3 來自 problem body（不同 source），證明 seen 計數器 per-chunk 橫跨 source 不重置——
        #    若哪邊把 seen 改成 per-source 重置，這裡會變 fig-1.2--1（碰撞！）→ 立刻變紅。
        EXPECTED_CLASH = ['fig-1.2', 'fig-1.2--1', 'fig-1.2--2', 'fig-1.2--3']
        clashing = [a for _, a in build_anchors if a.startswith('fig-1.2')]
        assert clashing == EXPECTED_CLASH, (
            f'同編號去重後綴漂移（含跨 source --3）：{clashing}')
        corpus_clashing = [i for _, i in corpus_ids if i.startswith('fig-1.2')]
        assert corpus_clashing == clashing, (
            f'corpus 去重後綴與 build 不一致：{corpus_clashing} vs {clashing}')

        # 5) `source != 'body'` fallback 分支明確驗證（之前完全未觸及的分歧點）：
        #    problem/solution 區的無語義媒體必須帶 source 標記，且兩端同字。
        build_set = {a for _, a in build_anchors}
        corpus_set = {i for _, i in corpus_ids}
        for tagged in ('fig-ch01-prob0-1', 'eq-ch01-sol0-0'):
            assert tagged in build_set, (
                f'build 端缺 source 標記 fallback：{tagged}（`source != "body"` 分支壞了？）')
            assert tagged in corpus_set, (
                f'corpus 端缺 source 標記 fallback：{tagged}（兩端 source 標記分歧 → 題目內點圖跳不到）')

        print('✓ caption/fallback/eq/leading-media/去重 + 跨 source 邊界 八情境 anchor 跨模組逐一相等')
        print(f'  golden anchors = {EXPECTED_ANCHORS}')
    finally:
        corpus.DATA_DIR = orig_corpus
        bc.DATA_DIR = orig_bc
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    test_anchor_parity_caption_fallback_eq_dedup()
    print('\n全部通過 ✅')
