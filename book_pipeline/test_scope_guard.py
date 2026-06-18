"""scope_guard 單元測試：uv run python -m book_pipeline.test_scope_guard

涵蓋：is_protected 路徑判定（程式碼面 vs per-slug 合法產物）、porcelain 解析、check_worker
bracket（越界 .py 捕提案 + enforce 還原、架構師既有改動 pre==post 不旗標、per-slug 產物不動、
冪等去重、observe 不還原）。全用臨時 dir + monkeypatch proposals.propose/SEEN_PATH/mode →
不碰真 store、不碰真工作樹。"""

import os
import subprocess
import tempfile

from book_pipeline import scope_guard as sg


def _git(root, *args):
    subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, check=True)


def _repo():
    d = tempfile.mkdtemp(prefix='sg_')
    _git(d, 'init', '-q')
    _git(d, 'config', 'user.email', 't@t')
    _git(d, 'config', 'user.name', 't')
    os.makedirs(os.path.join(d, 'book_pipeline', 'mineru_data', 's'), exist_ok=True)
    with open(os.path.join(d, 'book_pipeline', 'engine.py'), 'w') as f:
        f.write('ORIGINAL = 1\n')
    _git(d, 'add', '-A')
    _git(d, 'commit', '-qm', 'init')
    return d


def test_is_protected():
    P = sg.is_protected
    assert P('book_pipeline/build_catalogs.py')          # 引擎
    assert P('book_pipeline/test_parser.py')             # 測試也保護
    assert P('build/build_all.py')
    assert P('pyproject.toml')
    assert P('book_pipeline/booklists/physics.json')     # 書單 SoT
    assert P('.claude/skills/book-pipeline/SKILL.md')
    # per-slug 合法產物 → 不保護
    assert not P('book_pipeline/mineru_data/rudin/extract_rules.yaml')
    assert not P('book_pipeline/mineru_data/rudin/cover.jpg')
    assert not P('book_pipeline/catalog_overrides/rudin.json')
    assert not P('book_pipeline/metadata_schema.yaml')   # audit 合法 append subject
    assert not P('book_pipeline/mineru_data/s/parsed/book.json')
    print('✓ is_protected：程式碼面保護、per-slug 產物/schema 放行')


def test_parse_porcelain():
    rows = sg._parse_porcelain(' M book_pipeline/engine.py\n?? book_pipeline/new.py\n'
                               'R  a.py -> book_pipeline/b.py\n')
    by = {r['path']: r['code'] for r in rows}
    assert by['book_pipeline/engine.py'] == ' M'
    assert by['book_pipeline/new.py'] == '??'
    assert by['book_pipeline/b.py'] == 'R '       # rename 取目標路徑
    print('✓ _parse_porcelain：M/??/rename 目標路徑解析正確')


def test_bracket_enforce_reverts_and_captures(monkeypatch):
    d = _repo()
    cap = []
    monkeypatch.setattr(sg.proposals, 'propose',
                        lambda **kw: cap.append(kw) or f'P-test-{len(cap)}')
    monkeypatch.setattr(sg, 'SEEN_PATH', os.path.join(d, '.seen.json'))
    monkeypatch.setattr(sg, '_mode', lambda: 'enforce')

    pre = sg.snapshot(root=d)                                   # worker spawn 前快照
    eng = os.path.join(d, 'book_pipeline', 'engine.py')
    with open(eng, 'w') as f:                                   # worker 越界：改引擎
        f.write('ORIGINAL = 1\nHACKED = 999\n')
    rules = os.path.join(d, 'book_pipeline', 'mineru_data', 's', 'extract_rules.yaml')
    with open(rules, 'w') as f:                                 # worker 合法：per-slug 產物
        f.write('slug: s\n')

    handled = sg.check_worker(pre, root=d, verb='audit', slug='s')
    paths = {h['path'] for h in handled}
    assert paths == {'book_pipeline/engine.py'}, handled        # 只抓越界、不抓合法產物
    assert handled[0]['reverted'] is True
    assert open(eng).read() == 'ORIGINAL = 1\n'                 # 引擎已還原
    assert open(rules).read() == 'slug: s\n'                    # 合法產物不動
    assert len(cap) == 1 and cap[0]['domain'] == 'engine' and cap[0]['type_'] == 'patch'
    assert cap[0]['slug'] == 's' and 'HACKED = 999' in cap[0]['proposal']  # 歸屬 + diff 留痕
    # 冪等：還原後同一 worker 視窗的新 bracket（diff 已消）→ 無新越界
    assert sg.check_worker(sg.snapshot(root=d), root=d, verb='audit', slug='s') == []
    print('✓ check_worker(enforce)：越界引擎還原 + 捕 engine/patch 提案(歸屬+diff) + 合法產物不動 + 冪等')


def test_bracket_ignores_preexisting_architect_edit(monkeypatch):
    """核心安全：架構師在 worker spawn 前就改了引擎（pre 已含），worker 沒碰 → pre==post → 不旗標。"""
    d = _repo()
    cap = []
    monkeypatch.setattr(sg.proposals, 'propose', lambda **kw: cap.append(kw) or 'P-x')
    monkeypatch.setattr(sg, 'SEEN_PATH', os.path.join(d, '.seen.json'))
    monkeypatch.setattr(sg, '_mode', lambda: 'enforce')

    eng = os.path.join(d, 'book_pipeline', 'engine.py')
    with open(eng, 'w') as f:                                   # 架構師既有未提交開發
        f.write('ORIGINAL = 1\nARCHITECT_WIP = 1\n')
    pre = sg.snapshot(root=d)                                   # spawn 快照（含架構師改動）
    # worker 跑完什麼引擎都沒碰
    handled = sg.check_worker(pre, root=d, verb='audit', slug='s')
    assert handled == [] and cap == []                          # 不旗標架構師開發
    assert 'ARCHITECT_WIP = 1' in open(eng).read()             # 架構師改動原封不動
    print('✓ check_worker：架構師既有改動（pre==post）永不旗標、永不還原')


def test_bracket_observe_no_revert(monkeypatch):
    d = _repo()
    cap = []
    monkeypatch.setattr(sg.proposals, 'propose', lambda **kw: cap.append(kw) or 'P-x')
    monkeypatch.setattr(sg, 'SEEN_PATH', os.path.join(d, '.seen.json'))
    monkeypatch.setattr(sg, '_mode', lambda: 'observe')
    pre = sg.snapshot(root=d)
    eng = os.path.join(d, 'book_pipeline', 'engine.py')
    with open(eng, 'w') as f:
        f.write('ORIGINAL = 1\nHACKED = 1\n')
    handled = sg.check_worker(pre, root=d, verb='audit', slug='s')
    assert handled[0]['reverted'] is False
    assert 'HACKED = 1' in open(eng).read()                     # observe 未還原（架構師裁決）
    assert len(cap) == 1                                        # 仍捕提案 + surface
    print('✓ check_worker(observe)：不還原但仍捕提案 surface（永不與架構師互踩）')


if __name__ == '__main__':
    # 極簡 monkeypatch shim（免 pytest 依賴，與 repo 其他 test_*.py 同風格自跑）
    class _MP:
        def __init__(self): self._undo = []
        def setattr(self, obj, name, val):
            old = getattr(obj, name); self._undo.append((obj, name, old))
            setattr(obj, name, val)
        def undo(self):
            for obj, name, old in reversed(self._undo): setattr(obj, name, old)

    test_is_protected()
    test_parse_porcelain()
    for fn in (test_bracket_enforce_reverts_and_captures,
               test_bracket_ignores_preexisting_architect_edit,
               test_bracket_observe_no_revert):
        mp = _MP()
        try:
            fn(mp)
        finally:
            mp.undo()
    print('\n全部通過 ✅')
