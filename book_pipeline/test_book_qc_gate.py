"""部署前書況 gate 的狀態標記測試：mark/read/clear round-trip + 空殼收尾。

monkeypatch STATE_PATH/STATE_LOCK 至 tmp，絕不碰真 pipeline_state.json（daemon 常駐寫）。
"""
import pytest

from book_pipeline import pipeline_queue as q


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(q, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(q, "STATE_LOCK", str(tmp_path / "state.lock"))
    return tmp_path


def test_mark_read_clear_roundtrip(tmp_state):
    assert q.book_qc_review("foo") is None
    q.mark_book_qc("foo", ["companion", "partial_source(starts@9)"])
    m = q.book_qc_review("foo")
    assert m["review"] is True
    assert m["reasons"] == ["companion", "partial_source(starts@9)"]
    assert "at" in m
    q.clear_book_qc("foo")
    assert q.book_qc_review("foo") is None


def test_clear_removes_empty_shell(tmp_state):
    # 純 book_qc 標記的 slug，clear 後不留 {slug:{}}
    q.mark_book_qc("bar", ["companion"])
    q.clear_book_qc("bar")
    assert "bar" not in q._load_state()


def test_clear_preserves_other_keys(tmp_state):
    # slug 另有 deployed_at 等鍵時，clear 只移除 book_qc、保留其餘
    q.mark_deployed("baz")
    q.mark_book_qc("baz", ["title_mismatch(0%)"])
    q.clear_book_qc("baz")
    s = q._load_state()
    assert "baz" in s and "deployed_at" in s["baz"] and "book_qc" not in s["baz"]


def test_clear_noop_when_unmarked(tmp_state):
    # 未標記時 clear 不應建立空殼（避免每次成功部署都改寫 state）
    q.clear_book_qc("never_marked")
    assert "never_marked" not in q._load_state()


def test_blocking_marker_terminates_deploy_scheduling(tmp_state):
    # assess_full 的短路依據：book_qc_review 標記存在 → 上層回 'R 書況' 終止
    q.mark_book_qc("wrongbook", ["companion"])
    state = q._load_state()
    bq = q.book_qc_review("wrongbook", state)
    assert bq and bq.get("review")
