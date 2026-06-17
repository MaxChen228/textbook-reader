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

import argparse
import contextlib
import io
import json as _json

from book_pipeline import math_sweep


# ── _gid：穩定 id 契約 ────────────────────────────────────────────────────
def test_gid_stable_and_unique():
    tex = r"\frac{a}{b}"
    # 同 (slug, tex, display) → 同 gid（冪等、可重現）
    assert math_sweep._gid("strang", tex, False) == math_sweep._gid("strang", tex, False)
    # 格式 <slug>:<8 hex>
    gid = math_sweep._gid("strang", tex, False)
    slug, _, h = gid.partition(":")
    assert slug == "strang"
    assert len(h) == 8 and all(c in "0123456789abcdef" for c in h), gid
    # 跨 tex 不撞
    assert math_sweep._gid("strang", tex, False) != math_sweep._gid("strang", r"\frac{b}{a}", False)
    # 跨 slug 不撞（同 tex 不同書 → 不同 gid，fix 才不會套錯書）
    assert math_sweep._gid("strang", tex, False) != math_sweep._gid("axler", tex, False)
    # 跨 display 不撞：同書同 tex 但 inline vs display 是兩條 finding，fix 反查必須唯一
    assert math_sweep._gid("strang", tex, False) != math_sweep._gid("strang", tex, True)
    # 空 tex 不炸
    assert math_sweep._gid("x", "", False).startswith("x:")


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
    # gid 與 _gid 一致（含 display 維度）
    assert nu["gid"] == math_sweep._gid("bookA", r"\Nu", False)


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
    # limit=0 須回 0 條（非 falsy-當無限制）
    assert math_sweep.collect_todo(limit=0) == []


# ── cmd_fix orchestration：守住「render 不過即擋下、不落地」這條安全契約 ──────
#
# 為何重要：fix 用「單條 render 驗證」取代原 30min 全 corpus gate——這是逐條 override
# 的整個安全基礎。若 render 失敗仍往下寫 override，就會把語意錯/編譯壞的 tex 落進
# parsed，比舊架構更糟。本組測試 hermetic patch 掉 run_render/override/validate，只驗
# cmd_fix 的控制流：壞 new → 一步都不落地；好 new → 併入+apply+重驗+正確回報待辦消長。

_FINDING = {
    "tex": r"a^{x}^{y}", "display": False, "occ": 1, "category": "double_script",
    "err": "Double superscript",
    "targets": [{"chunk": "ch01", "selector": "body[1]", "field": "tex"}],
}


def _run_fix(monkeypatch, *, finding, render_ok, after_findings, new=r"a^{x y}"):
    spy: list[tuple] = []
    rep_before = {"status": "fail", "findings": [finding] if finding else []}
    monkeypatch.setattr(math_sweep, "read_report", lambda s: rep_before)
    monkeypatch.setattr(
        math_sweep, "run_render",
        lambda items: {0: {"i": 0, "ok": render_ok, "err": "" if render_ok else "Missing }"}},
    )
    monkeypatch.setattr(
        math_sweep, "finding_to_overrides",
        lambda slug, f, n: (spy.append(("fto", slug, n)), [{"id": "ov1"}, {"id": "ov2"}])[1],
    )
    monkeypatch.setattr(
        math_sweep, "merge_overrides",
        lambda slug, ovs: (spy.append(("merge", slug, len(ovs))), {"added": len(ovs), "replaced": 0})[1],
    )
    monkeypatch.setattr(
        math_sweep, "apply_overrides",
        lambda slug: (spy.append(("apply", slug)), {"fix_eq_tex:applied": 1})[1],
    )
    rep_after = {"status": "fail" if after_findings else "pass",
                 "findings": after_findings, "stats": {"bad_unique": len(after_findings)}}
    monkeypatch.setattr(math_sweep, "validate_book", lambda s: rep_after)
    monkeypatch.setattr(math_sweep, "write_report", lambda s, r: spy.append(("write", s)))

    gid = (math_sweep._gid("bookA", finding["tex"], finding["display"])
           if finding else "bookA:deadbeef")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = math_sweep.cmd_fix(argparse.Namespace(gid=gid, new=new))
    return rc, _json.loads(buf.getvalue()), spy


def test_fix_bad_new_blocked_no_landing(monkeypatch):
    # new 渲染失敗 → rc=1、stage=render，且 override/apply/validate 一步都沒呼叫
    rc, out, spy = _run_fix(monkeypatch, finding=_FINDING, render_ok=False, after_findings=[_FINDING])
    assert rc == 1 and out["ok"] is False and out["stage"] == "render"
    assert spy == [], f"render 不過卻有落地動作：{spy}"


def test_fix_success_lands_and_clears(monkeypatch):
    # new 渲染過 + 重驗後該 gid 消失 → rc=0、ok、待辦歸零；全鏈路依序執行
    rc, out, spy = _run_fix(monkeypatch, finding=_FINDING, render_ok=True, after_findings=[])
    assert rc == 0 and out["ok"] is True
    assert out["book_remaining"] == 0 and out["overrides"] == {"added": 2, "replaced": 0}
    kinds = [s[0] for s in spy]
    assert kinds == ["fto", "merge", "apply", "write"], kinds


def test_fix_still_residual_warns(monkeypatch):
    # override 寫了但重驗該 gid 仍在（如 skip-drift）→ rc=1 + warn，誠實回報未清掉
    rc, out, _ = _run_fix(monkeypatch, finding=_FINDING, render_ok=True, after_findings=[_FINDING])
    assert rc == 1 and out["ok"] is False and "warn" in out


def test_fix_unknown_gid(monkeypatch):
    rc, out, spy = _run_fix(monkeypatch, finding=None, render_ok=True, after_findings=[])
    assert rc == 1 and out["ok"] is False and "查無" in out["error"]
    assert spy == []


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
