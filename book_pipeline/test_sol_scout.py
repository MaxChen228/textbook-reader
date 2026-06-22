"""sol_scout 測試（效率修：殺 sol_extract 60min flail）。

核心契約＝判讀正確：引擎 `extract_sol_chapters` 章錨現認任意 text_level（chapter_level 預設 null），故
① 章標在 lvl2/header 但帶數字章號 → 現可錨（✓，不再 pending）
② 有文字但 0 個數字章號（純標題章/羅馬數字）→ 判 _pending（無 int 章號）
③ 可錨但遠少於主書章數 → ✓ 但帶 shortfall 警告（dry-run 確認）
④ 可錨且數量充足 → 純 ✓

驗 _verdict 純函式（判讀核心）+ _bucket/_CH_NUMBERED（分桶/章號偵測）+ scout 合成資料端到端。
全 hermetic：scout 端到端用 tmp DATA_DIR，絕不碰真實資料。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from book_pipeline import sol_scout as S


# ── _CH_NUMBERED：章號偵測 ──────────────────────────────────────────────────────
def test_ch_numbered_matches_and_rejects():
    m = lambda t: bool(S._CH_NUMBERED.match(t))
    assert m('Chapter 1 Set Theory and Logic')
    assert m('7 The Path Integral')
    assert m('30 Spontaneous Symmetry Breaking')
    assert m('4 Specific Heat of Solids')
    # 帶句點裸章號（kittel 式）必須命中——負前瞻只擋 N.M、不擋 N.（review block 回歸守門）
    assert m('Chapter 1.')
    assert m('1.')
    assert m('10.')
    # 排除：N.M section（非章）、純標題無號
    assert not m('1.2 Review of Compiler Structure')
    assert not m('1.5 Apple')
    assert not m('Limits and Continuity')
    assert not m('Before Calculus')


# ── _verdict：判讀核心（四型）─────────────────────────────────────────────────────
def test_verdict_off_level_now_anchorable():
    """章標在 lvl2/header 但帶數字章號（munkres/srednicki 型）→ chapter_level 可配後 ✓ 可錨（不再 pending）。"""
    v = S._verdict(eng_anchors=25, text_total=300, main_ch=23)
    blob = '\n'.join(v)
    assert '✓' in blob and '_pending' not in blob
    assert '任意層級' in blob                            # 明告不分層級皆可錨


def test_verdict_titles_without_number_is_pending():
    """有文字 block 但 0 個數字章號（純標題章/羅馬數字）→ _pending（無 int 章號）。"""
    v = S._verdict(eng_anchors=0, text_total=19, main_ch=16)
    blob = '\n'.join(v)
    assert '_pending' in blob and ('純標題' in blob or '數字章號' in blob or '羅馬' in blob)


def test_verdict_no_text_is_pending():
    v = S._verdict(eng_anchors=0, text_total=0, main_ch=None)
    blob = '\n'.join(v)
    assert '_pending' in blob and 'source-quality' in blob


def test_verdict_anchored_sufficient_is_ok_no_shortfall():
    v = S._verdict(eng_anchors=12, text_total=120, main_ch=12)
    blob = '\n'.join(v)
    assert blob.startswith('✓') or '✓' in v[0]
    assert '遠少於' not in blob                          # 充足 → 無 shortfall 警告


def test_verdict_anchored_but_shortfall_warns():
    """可錨數遠少於主書章數（部分章無數字章號/源頭漏章）→ ✓ + shortfall 警告。"""
    v = S._verdict(eng_anchors=3, text_total=350, main_ch=97)
    blob = '\n'.join(v)
    assert '✓' in blob and '遠少於' in blob


# ── _bucket：分桶 + 帶章號計數 ──────────────────────────────────────────────────
def test_bucket_classifies_levels_and_counts_numbered():
    blocks = [
        {'type': 'text', 'text_level': 1, 'text': 'Chapter 1 Foo'},   # lvl1 numbered
        {'type': 'text', 'text_level': 1, 'text': 'Limits'},          # lvl1 plain
        {'type': 'text', 'text_level': 2, 'text': '2 Bar'},           # lvl2 numbered
        {'type': 'header', 'text': '3 Baz'},                          # header numbered
        {'type': 'text', 'text_level': 1, 'text': ''},                # 空 → 略過
        {'type': 'equation', 'text': '$$x$$'},                        # 非 text/header → 略過
    ]
    b = S._bucket(blocks)
    assert b['text_level==1']['total'] == 2 and b['text_level==1']['numbered'] == 1
    assert b['text_level==2']['numbered'] == 1
    assert b['header']['numbered'] == 1


# ── scout 端到端（hermetic tmp DATA_DIR）─────────────────────────────────────────
def _write_sol(root: str, sol_slug: str, blocks: list[dict], main_chapters: int | None = None):
    d = Path(root) / sol_slug / 'unified'; d.mkdir(parents=True)
    (d / 'content_list.json').write_text(json.dumps(blocks))
    if main_chapters is not None:
        main = S.re.sub(r'_sol$', '', sol_slug)
        pd = Path(root) / main / 'parsed'; pd.mkdir(parents=True)
        (pd / 'book.json').write_text(json.dumps({'chapters': [{} for _ in range(main_chapters)]}))


def test_scout_end_to_end_off_level(capsys):
    with tempfile.TemporaryDirectory() as root:
        # lvl1 純標題、真章標在 lvl2 帶數字 → chapter_level 可配後應判 ✓ 可錨（不再 pending）
        blocks = [{'type': 'text', 'text_level': 1, 'text': 'Some Title'}]
        blocks += [{'type': 'text', 'text_level': 2, 'text': f'{i} Chapter {i}'} for i in range(1, 6)]
        _write_sol(root, 'foo_sol', blocks, main_chapters=5)
        old = S.DATA_DIR; S.DATA_DIR = Path(root)
        try:
            S.scout('foo_sol')
        finally:
            S.DATA_DIR = old
        out = capsys.readouterr().out
        assert 'foo_sol' in out and '主書=foo' in out
        assert '✓' in out and '引擎可錨' in out            # off-level 帶數字 → 現可錨
        assert '`_pending: true`' not in out              # 不下 _pending 動作（標題提及選項不算）


def test_scout_end_to_end_no_numbered_is_pending(capsys):
    with tempfile.TemporaryDirectory() as root:
        # 全純標題、無數字章號 → 仍判 _pending（無 int 章號）
        blocks = [{'type': 'text', 'text_level': 1, 'text': t}
                  for t in ('Limits and Continuity', 'Before Calculus', 'Derivatives')]
        _write_sol(root, 'bar_sol', blocks, main_chapters=3)
        old = S.DATA_DIR; S.DATA_DIR = Path(root)
        try:
            S.scout('bar_sol')
        finally:
            S.DATA_DIR = old
        out = capsys.readouterr().out
        assert '`_pending: true`' in out                  # 無數字章號 → 下 _pending 動作


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-q']))
