"""sol_scout 測試（效率修：殺 sol_extract 60min flail）。

核心契約＝判讀正確：引擎 `extract_sol_chapters` 只認 lvl1 章錨，故
① 章標在 lvl2/header（eng=0, off>0）→ 判 harness-gap _pending、明告勿迭代 chapter_re
② lvl1 有文字但無數字章號（純標題章）→ 判 _pending
③ lvl1 可錨但遠少於主書章數 → ✓ 但帶 shortfall 警告（dry-run 確認）
④ lvl1 可錨且數量充足 → 純 ✓

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
def test_verdict_anchors_off_level_is_pending():
    """eng=0 但 off>0（munkres/srednicki/simon 型：章標在 lvl2/header）→ harness-gap _pending、勿迭代。"""
    v = S._verdict(eng_anchors=0, off=25, lvl1_total=22, main_ch=23)
    blob = '\n'.join(v)
    assert '_pending' in blob and 'harness-gap' in blob
    assert '勿' in blob and 'chapter_re' in blob       # 明告勿耗時迭代 chapter_re


def test_verdict_titles_without_number_is_pending():
    """eng=0, off=0, lvl1 有文字（純標題章，無數字章號）→ _pending。"""
    v = S._verdict(eng_anchors=0, off=0, lvl1_total=19, main_ch=16)
    blob = '\n'.join(v)
    assert '_pending' in blob and ('純標題' in blob or '無數字章號' in blob)


def test_verdict_no_lvl1_text_is_pending():
    v = S._verdict(eng_anchors=0, off=0, lvl1_total=0, main_ch=None)
    assert '_pending' in '\n'.join(v)


def test_verdict_anchored_sufficient_is_ok_no_shortfall():
    v = S._verdict(eng_anchors=12, off=0, lvl1_total=12, main_ch=12)
    blob = '\n'.join(v)
    assert blob.startswith('✓') or '✓' in v[0]
    assert '遠少於' not in blob                          # 充足 → 無 shortfall 警告


def test_verdict_anchored_but_shortfall_warns():
    """eng=2 但 main_ch=97（srednicki 型：少數 lvl1 錨、真章標在他處）→ ✓ + shortfall 警告。"""
    v = S._verdict(eng_anchors=2, off=91, lvl1_total=3, main_ch=97)
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
        # lvl1 全純標題、真章標在 lvl2 → 應判 _pending
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
        assert '_pending' in out                          # off-level → 判 _pending


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-q']))
