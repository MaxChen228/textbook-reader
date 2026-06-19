"""commit_artifacts 的純邏輯 + 行為契約測試。

跑：uv run python -m book_pipeline.test_artifact_autocommit

不碰真 repo 的 git：純函式（_artifact_slugs/_artifact_commit_msg）直接驗；
commit_artifacts 的「curated 白名單、no-op、fail-open」契約以暫存 git repo 隔離驗。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from book_pipeline import pipeline_tick as PT


# ── 1. _artifact_slugs：路徑 → 書 slug（去重排序、共用索引不列） ──────────────────
def test_artifact_slugs_extracts_per_book_and_skips_shared():
    files = [
        'book_pipeline/math_overrides/axler_linalg.json',
        'book_pipeline/catalog_overrides/brown_lemay_central_science.json',
        'book_pipeline/mineru_data/shankar_qm/parsed/ch1.zh.json',
        'book_pipeline/mineru_data/shankar_qm/extract_rules.yaml',   # 同書去重
        'book_pipeline/mineru_data/shankar_qm/cover.jpg',
        'book_pipeline/slug_map.json',            # 共用索引 → 不列
        'book_pipeline/proposals.d/_index.md',    # 共用 → 不列
        'book_pipeline/metadata_schema.yaml',     # 共用 → 不列
    ]
    assert PT._artifact_slugs(files) == [
        'axler_linalg', 'brown_lemay_central_science', 'shankar_qm']


def test_artifact_slugs_empty_and_shared_only():
    assert PT._artifact_slugs([]) == []
    # 全是共用索引 → 空 slug 清單（訊息會落到「索引」）
    assert PT._artifact_slugs(['book_pipeline/slug_map.json',
                               'book_pipeline/proposals.d/_index.md']) == []


# ── 2. _artifact_commit_msg：≤5 全列、>5 收 (+N)、空 slug → 「索引」 ──────────────
def test_commit_msg_lists_slugs_and_count():
    files = ['book_pipeline/math_overrides/a.json', 'book_pipeline/math_overrides/b.json']
    subj, body = PT._artifact_commit_msg(files)
    assert 'a, b' in subj and '2 檔' in subj
    assert 'commit_artifacts' in body


def test_commit_msg_truncates_over_five():
    files = [f'book_pipeline/math_overrides/s{i}.json' for i in range(8)]
    subj, _ = PT._artifact_commit_msg(files)
    assert '(+3)' in subj and '8 檔' in subj   # 列前 5、其餘收成 (+3)


def test_commit_msg_shared_only_says_index():
    subj, _ = PT._artifact_commit_msg(['book_pipeline/slug_map.json'])
    assert '索引' in subj and '1 檔' in subj


# ── 3. commit_artifacts 行為契約（暫存 git repo 隔離） ──────────────────────────
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def _init_repo(repo: Path) -> None:
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.email', 'test@test')
    _git(repo, 'config', 'user.name', 'test')
    (repo / 'book_pipeline').mkdir()
    (repo / '.gitignore').write_text('book_pipeline/mineru_data/*/raw/\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'init')


def _run_commit_artifacts(repo: Path) -> None:
    """以暫存 repo 當 ROOT 跑 commit_artifacts。還原 ROOT 絕不污染。"""
    orig = PT.ROOT
    PT.ROOT = str(repo)
    try:
        PT.commit_artifacts()
    finally:
        PT.ROOT = orig


def test_commit_artifacts_commits_whitelist_and_noop_when_clean():
    if not PT.AUTOCOMMIT:
        return  # 旗標關閉時本契約不適用
    with tempfile.TemporaryDirectory(prefix='autocommit_') as d:
        repo = Path(d)
        _init_repo(repo)
        mo = repo / 'book_pipeline' / 'math_overrides'
        mo.mkdir()
        (mo / 'axler_linalg.json').write_text('{}')
        # (a) 有貴重成果 → commit 一筆
        _run_commit_artifacts(repo)
        head = _git(repo, 'log', '-1', '--format=%s')
        assert 'axler_linalg' in head, f'未 commit 成果：{head}'
        assert _git(repo, 'status', '--short').strip() == '', '工作區未乾淨'
        # (b) 再跑一次無變更 → no-op，不產空 commit
        n_before = len(_git(repo, 'log', '--format=%h').splitlines())
        _run_commit_artifacts(repo)
        n_after = len(_git(repo, 'log', '--format=%h').splitlines())
        assert n_after == n_before, 'clean 時不該產空 commit'


def test_commit_artifacts_ignores_nonwhitelist_dirty():
    """白名單外的髒檔（例如 dev/、根目錄雜物）絕不被 auto-commit 帶走。"""
    if not PT.AUTOCOMMIT:
        return
    with tempfile.TemporaryDirectory(prefix='autocommit_') as d:
        repo = Path(d)
        _init_repo(repo)
        (repo / 'stray.txt').write_text('not an artifact')        # 白名單外
        (repo / 'book_pipeline' / 'random.py').write_text('x = 1')  # book_pipeline 下但非白名單路徑
        _run_commit_artifacts(repo)
        # 無任何白名單成果 → 不該有新 commit；stray 仍 untracked
        assert 'stray.txt' in _git(repo, 'status', '--short')
        assert _git(repo, 'log', '-1', '--format=%s').strip() == 'init'


def test_commit_artifacts_fail_open_on_bad_root():
    """git 出錯（ROOT 非 repo）絕不丟例外 → pipeline 不被連坐。"""
    with tempfile.TemporaryDirectory(prefix='autocommit_nogit_') as d:
        PT_orig = PT.ROOT
        PT.ROOT = d  # 非 git repo
        try:
            PT.commit_artifacts()  # 不該 raise
        finally:
            PT.ROOT = PT_orig


if __name__ == '__main__':
    test_artifact_slugs_extracts_per_book_and_skips_shared()
    test_artifact_slugs_empty_and_shared_only()
    test_commit_msg_lists_slugs_and_count()
    test_commit_msg_truncates_over_five()
    test_commit_msg_shared_only_says_index()
    test_commit_artifacts_commits_whitelist_and_noop_when_clean()
    test_commit_artifacts_ignores_nonwhitelist_dirty()
    test_commit_artifacts_fail_open_on_bad_root()
    print('\n全部通過 ✅')
