"""sol_extract 版本對齊閘單元測試：uv run python -m book_pipeline.test_sol_extract

edition_block_reason：讀 sol 的 editions.sol_alignment（書單管理 skill 由 LLM 親判寫入）；
aligned 明確 False→擋、True/缺/None→放行（fail-open）。**不自己比對版本字串**（鐵律）。
main 的版本閘：不對齊→return 3、開 proposal、不碰主書檔（gate 在 load_sol_rules 前）。
用 tmpdir 重指 editions.EDITIONS_DIR，不碰 repo；teardown 還原。"""

import json
import tempfile

from book_pipeline import editions as ed
from book_pipeline import sol_extract as sx

_ORIG_DIR = ed.EDITIONS_DIR


def teardown_function(function):
    ed.EDITIONS_DIR = _ORIG_DIR


def _isolate():
    ed.EDITIONS_DIR = tempfile.mkdtemp(prefix='soledition_')


def test_block_when_llm_judged_misaligned():
    _isolate()
    ed.save('halliday_sol', {'sol_alignment': {'aligned': False, 'parent_version': '11th',
                                               'sol_version': '10th', 'basis': 'LLM 親判'}})
    reason = sx.edition_block_reason('halliday_sol')
    assert reason and '10th' in reason and '11th' in reason
    print('✓ edition_block_reason：LLM 親判 aligned=False → 擋（題號錯位）')


def test_passes_when_aligned():
    _isolate()
    ed.save('griffiths_sol', {'sol_alignment': {'aligned': True, 'parent_version': '3rd',
                                                'sol_version': '3rd'}})
    assert sx.edition_block_reason('griffiths_sol') is None
    print('✓ edition_block_reason：aligned=True → 放行')


def test_failopen_when_unknown():
    _isolate()
    assert sx.edition_block_reason('never_checked_sol') is None            # 無 editions 檔（未查證）
    ed.save('partial_sol', {'version': {'label': '2nd'}})
    assert sx.edition_block_reason('partial_sol') is None                  # 查了連結未判對齊
    ed.save('pending_sol', {'sol_alignment': {'aligned': None}})
    assert sx.edition_block_reason('pending_sol') is None                  # aligned 未判定
    print('✓ edition_block_reason：未查證/未判對齊/aligned=None 一律 fail-open 放行（不擋好書）')


def test_main_blocks_merge_on_misalignment(monkeypatch):
    """版本不對齊 → main return 3、開 proposal、gate 在 load_sol_rules 前故不碰主書 mineru_data。"""
    _isolate()
    ed.save('z_sol', {'sol_alignment': {'aligned': False, 'parent_version': '2nd', 'sol_version': '1st'}})
    proposed = []
    monkeypatch.setattr(sx, '_open_edition_mismatch_proposal',
                        lambda m, s, r: proposed.append((m, s, r)))
    rc = sx.main('z_main', 'z_sol', dry_run=False)
    assert rc == 3 and len(proposed) == 1
    print('✓ main：版本不對齊 → return 3、開 proposal、不 merge（gate 先於讀主書檔）')


# ── 章錨層級 chapter_level（解鎖 lvl2 章標解答書 = harness-gap 主修）─────────────
def test_chapter_level_anchors(tmp_path, monkeypatch):
    """chapter_level=None（預設）認任意 text_level 的章標 → 解鎖 lvl2 解答書；設 int 則只認該層級。"""
    blocks = [
        {'type': 'text', 'text_level': 2, 'text': 'Chapter 1'},   # 章標落在 lvl2（舊引擎抓不到）
        {'type': 'text', 'text': 'Problem 1.1'},
        {'type': 'text', 'text': 'ans a'},
        {'type': 'text', 'text_level': 2, 'text': 'Chapter 2'},
        {'type': 'text', 'text': 'Problem 2.1'},
        {'type': 'text', 'text': 'ans b'},
    ]
    d = tmp_path / 'foo_sol' / 'unified'
    d.mkdir(parents=True)
    (d / 'content_list.json').write_text(json.dumps(blocks))
    monkeypatch.setattr(sx, 'DATA_DIR', tmp_path)
    rules = sx.load_sol_rules('foo_sol')          # 無 yaml → DEFAULTS（chapter_level=None）
    assert rules['chapter_level'] is None
    got = sx.extract_sol_chapters('foo_sol', rules)
    assert set(got) == {1, 2} and '1.1' in got[1] and '2.1' in got[2], '任意層級應抓到 2 個 lvl2 章'
    # chapter_level=1 → lvl2 章標被濾掉 → 0 章（驗證舊行為仍可顯式還原）
    assert sx.extract_sol_chapters('foo_sol', {**rules, 'chapter_level': 1}) == {}
    print('✓ chapter_level：None 認任意層級（解鎖 lvl2）、int 限定層級')


def test_chapter_level_invalid_rejected(tmp_path, monkeypatch):
    """chapter_level 非 int/None（如字串）→ load_sol_rules SystemExit。"""
    d = tmp_path / 'bar_sol'
    d.mkdir(parents=True)
    (d / 'sol_rules.yaml').write_text("chapter_level: 'two'\n")
    monkeypatch.setattr(sx, 'DATA_DIR', tmp_path)
    try:
        sx.load_sol_rules('bar_sol')
        assert False, '應 SystemExit'
    except SystemExit:
        pass
    print('✓ chapter_level 非 int/null → 拒收')


if __name__ == '__main__':
    test_block_when_llm_judged_misaligned()
    test_passes_when_aligned()
    test_failopen_when_unknown()
    print('（test_main_blocks_merge_on_misalignment 需 pytest monkeypatch，於 pytest 跑）')
    print('\n全部通過 ✅')
