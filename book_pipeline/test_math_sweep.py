"""math_sweep 待辦清單契約測試：uv run python -m book_pipeline.test_math_sweep

守的真相（Phase 1，list 工具）：

1. _gid(slug, tex)：全域穩定 id 綁 tex 內容。這是 list↔fix 唯一橋——agent 從 list 抄
   gid、fix 用 gid 反查 finding。若 gid 隨 findings 順序漂移（例如改用 list index），
   report 一重生（fix 後單書重驗會重生）整批 gid 失效、agent 拿舊 gid 全 miss。本測試
   釘死「同 (slug,tex) → 同 gid、跨 tex 不撞、格式 <slug>:<hex8>」。

2. iter_todo / collect_todo：把全 corpus _math_report.json findings 攤平成待辦，且
   book/category/limit 過濾正確。攤平漏書或濾錯 → agent 看不到該修的、或重複做。

全 hermetic：monkeypatch read_report / iter_reports 餵假 report，零真實資料、零 node。
"""

from book_pipeline import math_sweep


# ── _gid：穩定 id 契約 ────────────────────────────────────────────────────
def test_gid_stable_and_unique():
    tex = r"\frac{a}{b}"
    # 同 (slug, tex) → 同 gid（冪等、可重現）
    assert math_sweep._gid("strang", tex) == math_sweep._gid("strang", tex)
    # 格式 <slug>:<8 hex>
    gid = math_sweep._gid("strang", tex)
    slug, _, h = gid.partition(":")
    assert slug == "strang"
    assert len(h) == 8 and all(c in "0123456789abcdef" for c in h), gid
    # 跨 tex 不撞
    assert math_sweep._gid("strang", tex) != math_sweep._gid("strang", r"\frac{b}{a}")
    # 跨 slug 不撞（同 tex 不同書 → 不同 gid，fix 才不會套錯書）
    assert math_sweep._gid("strang", tex) != math_sweep._gid("axler", tex)
    # 空 tex 不炸
    assert math_sweep._gid("x", "").startswith("x:")


# ── 假 corpus：兩書、含 skipped 與 category 多樣 ──────────────────────────
_FAKE = {
    "bookA": {
        "status": "fail",
        "findings": [
            {"tex": r"a^{x}^{y}", "display": False, "occ": 3,
             "category": "double_script", "err": "Double superscript",
             "targets": [{"chunk": "ch01", "selector": "body[1]", "field": "tex"}]},
            {"tex": r"\Nu", "display": False, "occ": 1,
             "category": "undefined_macro", "err": r"Undefined control sequence \Nu",
             "targets": [{"chunk": "ch02", "selector": "body[2]", "field": "md"},
                         {"chunk": "ch03", "selector": "body[5]", "field": "md"}]},
        ],
    },
    "bookB": {
        "status": "fail",
        "findings": [
            {"tex": r"\left( x", "display": True, "occ": 2,
             "category": "left_right", "err": "missing \\right",
             "targets": [{"chunk": "ch01", "selector": "body[9]", "field": "tex"}]},
        ],
    },
    "skipme": {"status": "skipped", "findings": []},
}


def _patch(monkeypatch):
    monkeypatch.setattr(math_sweep, "read_report", lambda s: _FAKE.get(s))
    monkeypatch.setattr(
        math_sweep, "iter_reports",
        lambda: ((s, r) for s, r in _FAKE.items() if r.get("status") != "skipped"),
    )


def test_collect_todo_flattens_all(monkeypatch):
    _patch(monkeypatch)
    rows = math_sweep.collect_todo()
    # 3 條 finding 跨 2 書（skipped 不算）
    assert len(rows) == 3
    slugs = {r["slug"] for r in rows}
    assert slugs == {"bookA", "bookB"}
    # 每列含 agent 決策必需欄位
    r0 = rows[0]
    assert set(r0) >= {"gid", "slug", "category", "display", "occ", "targets", "err", "tex"}
    # targets 是「條數」非列表
    nu = next(r for r in rows if r["tex"] == r"\Nu")
    assert nu["targets"] == 2
    # gid 與 _gid 一致
    assert nu["gid"] == math_sweep._gid("bookA", r"\Nu")


def test_collect_todo_book_filter(monkeypatch):
    _patch(monkeypatch)
    rows = math_sweep.collect_todo(book="bookB")
    assert len(rows) == 1 and rows[0]["slug"] == "bookB"
    # skipped 書 → 空
    assert math_sweep.collect_todo(book="skipme") == []


def test_collect_todo_category_and_limit(monkeypatch):
    _patch(monkeypatch)
    assert [r["category"] for r in math_sweep.collect_todo(category="left_right")] == ["left_right"]
    assert len(math_sweep.collect_todo(limit=2)) == 2


# ── minimal pytest-less runner（對齊 book_pipeline 其他 test 的 __main__ 慣例）──
def _run():
    import inspect

    class _MP:
        def __init__(self): self._undo = []
        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def undo(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)
            self._undo.clear()

    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    passed = 0
    for name, fn in fns:
        mp = _MP()
        try:
            if "monkeypatch" in inspect.signature(fn).parameters:
                fn(mp)
            else:
                fn()
            passed += 1
            print(f"  ✅ {name}")
        finally:
            mp.undo()
    print(f"\nmath_sweep：{passed}/{len(fns)} 通過 ✅")
    return 0 if passed == len(fns) else 1


if __name__ == "__main__":
    raise SystemExit(_run())
