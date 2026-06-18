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
import os
from collections import defaultdict

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


# ── sweep batch：JSONL 容錯 / render 門控+retry / 雙機 base / per-book 落地 ────
#
# 為何重要：batch 把「想 new tex」外包給 LLM，安全全靠本機 render 門控（壞的不落地、進
# retry）。LLM 輸出不可信（漏條/亂回/markdown）→ _parse_jsonl 必須容錯，_process_pool 必須
# 漏條與 render-fail 都丟回 retry，cmd_batch 必須每書只落地一次。全 hermetic、不打真 API。

def _finding_t(tex, display=False):
    return {"tex": tex, "display": display, "occ": 1, "category": "x", "err": "e",
            "targets": [{"chunk": "ch01", "selector": "body[0]", "field": "tex"}]}


def test_parse_jsonl_tolerant():
    txt = ('```json\n{"i":0,"tex":"a"}\n garbage line\n{"i":1,"tex":"b"}\n{"bad":1}\n'
           '{"i":2}\n{"i":3,"unrecoverable":true}\n```')
    # markdown 圍欄/雜訊/缺 tex 無 unrec(i2)/無 i(bad) 全跳過；fix 兩條 + unrecoverable 一條
    assert math_sweep._parse_jsonl(txt) == {0: {"tex": "a"}, 1: {"tex": "b"}, 3: {"unrec": True}}


def test_batched():
    assert list(math_sweep._batched([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]


def test_ccnexus_base_env_and_host(monkeypatch):
    old = os.environ.pop("CCNEXUS_BASE_URL", None)
    try:
        os.environ["CCNEXUS_BASE_URL"] = "http://x:1"          # env 覆寫優先
        assert math_sweep._ccnexus_base() == "http://x:1"
        del os.environ["CCNEXUS_BASE_URL"]
        monkeypatch.setattr(math_sweep.socket, "gethostname", lambda: "chenliangyusAir.local")
        assert "127.0.0.1" in math_sweep._ccnexus_base()       # felix → 本機
        monkeypatch.setattr(math_sweep.socket, "gethostname", lambda: "MacBook-Air-7")
        assert "100.118.39.104" in math_sweep._ccnexus_base()  # oscar → felix IP
    finally:
        if old is not None:
            os.environ["CCNEXUS_BASE_URL"] = old
        else:
            os.environ.pop("CCNEXUS_BASE_URL", None)


def _one(grp, monkeypatch):
    return math_sweep._run_one_batch(grp, 0, model="m", base="b", auth="a", pool_name="short", rnd=0)


def test_run_one_batch_gates_and_retries(monkeypatch):
    grp = [("g0", "bookA", _finding_t("BAD0")),
           ("g1", "bookA", _finding_t("OK1")),
           ("g2", "bookB", _finding_t("MISS2"))]
    # 模型：i0 回壞 tex（render fail）、i1 回好 tex、i2 漏回。批次 render → 須回 per-item verdict。
    monkeypatch.setattr(math_sweep, "_call_llm",
                        lambda payload, **k: '{"i":0,"tex":"BADNEW"}\n{"i":1,"tex":"GOODNEW"}')
    monkeypatch.setattr(math_sweep, "run_render",
                        lambda items: {it["i"]: {"ok": it["s"] == "GOODNEW"} for it in items})
    monkeypatch.setattr(math_sweep, "finding_to_overrides", lambda s, f, n: [{"id": s + "-ov"}])
    res = _one(grp, monkeypatch)
    assert res["accepts"] == [("bookA", "g1", "GOODNEW", [{"id": "bookA-ov"}])]  # 只 i1 落地
    assert {x[0] for x in res["retry"]} == {"g0", "g2"}                          # render-fail + 漏回
    assert res["unrec"] == []


def test_run_one_batch_llm_failure_retries_all(monkeypatch):
    grp = [("g0", "bookA", _finding_t("X")), ("g1", "bookA", _finding_t("Y"))]
    def boom(*a, **k):
        raise RuntimeError("conn reset")
    monkeypatch.setattr(math_sweep, "_call_llm", boom)
    res = _one(grp, monkeypatch)
    assert res["state"] == "error" and res["retry"] == grp and not res["accepts"]  # 整批重試零落地


def test_run_one_batch_unrecoverable_exits_retry(monkeypatch):
    # 模型誠實宣告 unrecoverable → 進 unrec 終態、**不重試**（退出無限重試迴圈）
    grp = [("g0", "bookA", _finding_t("NOISE"))]
    monkeypatch.setattr(math_sweep, "_call_llm", lambda payload, **k: '{"i":0,"unrecoverable":true}')
    monkeypatch.setattr(math_sweep, "run_render", lambda items: {})
    res = _one(grp, monkeypatch)
    assert res["unrec"] == [("bookA", 1)] and not res["retry"] and not res["accepts"]
    assert res["verdicts"][0]["outcome"] == "unrecoverable"


# ── 語意守門：render 過但空殼/原語不可落地（render gate 之上的第二道閘）─────────
def test_semantic_reason_blocks_empty_and_primitive():
    sr = math_sweep.semantic_reason
    assert sr(r"$\mathrm{~~} $") == "empty_shell"              # 純 nbsp 空白
    assert sr("$$ $$") == "empty_shell"                       # 空 display
    assert sr("") == "empty_shell"                            # 空字串
    assert sr(r"$\mathbf{}\mathbf{}\mathbf{}$") == "empty_shell"  # 一排空盒
    assert sr(r"${\let\mathbf\relax \mathbf{}\mathbf{}}$") == "tex_primitive"  # \let 中和
    assert sr(r"$\def\x{}\x$") == "tex_primitive"


def test_semantic_reason_passes_legit_short_formulas():
    sr = math_sweep.semantic_reason
    for ok in (r"$N_{2}$", r"$\nu_{2}$", r"$\sqrt{2}$", r"$\alpha = 1$", r"$\alpha \in K$",
               r"$\partial U$", r"$\delta L$", r"\mu\text{A}", r"$T|_{\mathrm{null}(T)^\perp}$",
               r"$\chi _ { 2 }$", r"$\omega_{\mu}^{a}{}_{b}$",
               r"\begin{array}{c c c c} a & b & c & d \\ \end{array}"):  # 表格欄位規格不誤殺
        assert sr(ok) is None, ok


def test_semantic_reason_passes_whitelist_outside_symbols():
    # 黑名單翻轉護欄：合法但不在舊白名單的符號（剝光會誤判空殼）必須放行。
    # 實證 bug：brown_lemay `\complement{\upharpoonright}` 被誤判 empty_shell 永久退回。
    sr = math_sweep.semantic_reason
    for ok in (r"$\complement{\upharpoonright}$", r"$\nexists x$", r"$A \boxtimes B$",
               r"$\hbar\Game$", r"$\varnothing$", r"$a \multimap b$",
               r"$\mathbb{Z}_{\geq 0}$"):    # mathbb 是格式但 Z 是內容 → 放行
        assert sr(ok) is None, ok


def test_semantic_reason_still_blocks_empty_after_flip():
    # 黑名單翻轉不得放鬆空殼/原語偵測（含純間距/phantom 等無內容 wrapper）。
    sr = math_sweep.semantic_reason
    assert sr(r"$\mathrm{~~}$") == "empty_shell"
    assert sr(r"$\mathbf{}\mathbf{}\mathbf{}$") == "empty_shell"
    assert sr(r"$\phantom{}\hphantom{}$") == "empty_shell"   # 空 phantom → 黑名單刪命令+空引數 → 空
    assert sr(r"$\quad\qquad\,$") == "empty_shell"           # 純間距無內容
    assert sr(r"${\let\mathbf\relax \mathbf{}}$") == "tex_primitive"


def test_run_one_batch_semantic_gate_blocks_renderable_empty(monkeypatch):
    # 模型回「能 render 但語意空洞」的空殼 → render_ok=True 卻必須擋下不落地、回流重試
    grp = [("g0", "bookA", _finding_t("DESTROYED_OCR"))]
    monkeypatch.setattr(math_sweep, "_call_llm", lambda payload, **k: r'{"i":0,"tex":"\\mathrm{~~} "}')
    monkeypatch.setattr(math_sweep, "run_render", lambda items: {it["i"]: {"ok": True} for it in items})
    monkeypatch.setattr(math_sweep, "finding_to_overrides",
                        lambda s, f, n: (_ for _ in ()).throw(AssertionError("空殼不該落地")))
    res = _one(grp, monkeypatch)
    assert not res["accepts"]                                 # 零落地
    assert {x[0] for x in res["retry"]} == {"g0"}             # 回流重試
    assert res["verdicts"][0]["outcome"] == "semantic_fail"


def _batch_ns(**kw):
    base = dict(n=40, rounds=2, model="m", book=None, category=None, limit=None, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_cmd_batch_end_to_end(monkeypatch):
    todo = [("bookA", _finding_t("A")), ("bookA", _finding_t("B")), ("bookB", _finding_t("C"))]
    monkeypatch.setattr(math_sweep, "iter_todo", lambda **k: iter(todo))
    monkeypatch.setattr(math_sweep, "_ccnexus_base", lambda: "http://x")
    monkeypatch.setattr(math_sweep, "_ccnexus_auth", lambda: "auth")
    monkeypatch.setattr(
        math_sweep, "_call_llm",
        lambda payload, **k: "\n".join('{"i":%d,"tex":"NEW%d"}' % (x["i"], x["i"]) for x in payload))
    monkeypatch.setattr(math_sweep, "run_render", lambda items: {it["i"]: {"ok": True} for it in items})
    monkeypatch.setattr(math_sweep, "finding_to_overrides", lambda s, f, n: [{"id": s}])
    landed = []
    monkeypatch.setattr(math_sweep, "merge_overrides",
                        lambda s, ovs: (landed.append(("merge", s, len(ovs))),
                                        {"added": len(ovs), "replaced": 0})[1])
    monkeypatch.setattr(math_sweep, "apply_overrides", lambda s: (landed.append(("apply", s)), {})[1])
    monkeypatch.setattr(math_sweep, "validate_book",
                        lambda s: {"status": "pass", "findings": [], "stats": {"bad_unique": 0}})
    monkeypatch.setattr(math_sweep, "write_report", lambda s, r: None)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = math_sweep.cmd_batch(_batch_ns())
    out = _json.loads(buf.getvalue())
    assert rc == 0 and out["accepted"] == 3 and out["books_touched"] == 2 and out["still_failing"] == 0
    assert out["remaining_by_book"] == {"bookA": 0, "bookB": 0}
    # 每書 merge+apply 各一次（per-book 落地、非每條）
    assert sorted(landed) == sorted(
        [("merge", "bookA", 2), ("apply", "bookA"), ("merge", "bookB", 1), ("apply", "bookB")])


def test_cmd_batch_dry_run_no_api(monkeypatch):
    todo = [("bookA", _finding_t("short")), ("bookA", _finding_t("x" * 500))]
    monkeypatch.setattr(math_sweep, "iter_todo", lambda **k: iter(todo))
    monkeypatch.setattr(math_sweep, "_ccnexus_base", lambda: "http://x")
    called = []
    monkeypatch.setattr(math_sweep, "_call_llm", lambda *a, **k: called.append(1))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        math_sweep.cmd_batch(_batch_ns(dry_run=True))
    out = _json.loads(buf.getvalue())
    assert out["dry_run"] and out["total"] == 2 and out["short"] == 1 and out["long"] == 1
    assert called == []                                        # dry-run 絕不打 LLM


def test_cmd_batch_node_unavailable(monkeypatch):
    # node 缺 → render 守門失效 → graceful 中止（rc=1），絕不打 LLM、不落地
    monkeypatch.setattr(math_sweep, "iter_todo", lambda **k: iter([("bookA", _finding_t("A"))]))
    monkeypatch.setattr(math_sweep, "node_available", lambda: False)
    called = []
    monkeypatch.setattr(math_sweep, "_call_llm", lambda *a, **k: called.append(1))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = math_sweep.cmd_batch(_batch_ns())
    out = _json.loads(buf.getvalue())
    assert rc == 1 and out["ok"] is False and "node" in out["error"] and called == []


def test_run_one_batch_render_exception_retries(monkeypatch):
    # render_check.js 偶發 raise（整批 spawn 掛）→ 全候選進 retry、零落地（不裸炸整批）
    grp = [("g0", "bookA", _finding_t("X"))]
    monkeypatch.setattr(math_sweep, "_call_llm", lambda payload, **k: '{"i":0,"tex":"NEW"}')
    def boom(items):
        raise RuntimeError("render_check crash")
    monkeypatch.setattr(math_sweep, "run_render", boom)
    monkeypatch.setattr(math_sweep, "finding_to_overrides", lambda s, f, n: [{"id": "x"}])
    res = _one(grp, monkeypatch)
    assert {x[0] for x in res["retry"]} == {"g0"} and not res["accepts"]


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
