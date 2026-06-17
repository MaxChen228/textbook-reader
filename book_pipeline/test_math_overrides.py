"""apply_math_overrides 的 guard / 冪等 / skip-drift + locator→selector 橋接。

跑：uv run python -m book_pipeline.test_math_overrides
用真實 DATA_DIR 下的一次性 slug 當 fixture（_apply_* 經 _chunk_path 讀磁碟），每測先 _setup 重置、跑完即清。
covers：eq/inline 各結果碼、title/caption 分支（anchor key= field 的關鍵路徑）、all 替換（occ>1）、
old==new、error-path（缺 expect / 不支援 field/action）、apply_overrides 的 build_catalogs gate、locator 橋接。
"""
import json
import shutil

from book_pipeline import apply_math_overrides as amo
from book_pipeline.math_validate import locator_to_target
from book_pipeline.translate import overlay_anchor

SLUG = "__test_math_overrides__"
PARSED = amo.DATA_DIR / SLUG / "parsed"
OV_FILE = amo.OVERRIDE_DIR / f"{SLUG}.json"


def _setup() -> None:
    PARSED.mkdir(parents=True, exist_ok=True)
    (PARSED / "ch01.json").write_text(json.dumps({
        "title": r"Ch1 $\AA$ unit",
        "body": [
            {"t": "eq", "tex": r"a^{x}^{y}"},                     # body[0] 壞 eq
            {"t": "p", "md": r"see $5\AA$ here"},                 # body[1] inline 偽符號
            {"t": "p", "md": "plain text no math"},               # body[2] 無數學
            {"t": "p", "md": r"twice $\bad$ and $\bad$ again"},   # body[3] 同欄同式 ×2
            {"t": "fig", "caption": r"fig with $\AA$ here"},      # body[4] caption 分支
        ],
    }, ensure_ascii=False), encoding="utf-8")


def _teardown() -> None:
    shutil.rmtree(amo.DATA_DIR / SLUG, ignore_errors=True)
    OV_FILE.unlink(missing_ok=True)


def _block(idx: int) -> dict:
    return json.loads((PARSED / "ch01.json").read_text(encoding="utf-8"))["body"][idx]


def _field(name: str):
    return json.loads((PARSED / "ch01.json").read_text(encoding="utf-8")).get(name)


def _call(fn, ov) -> str:
    return fn(SLUG, ov, PARSED / "_override_backups" / "t", set())


def test_fix_eq_tex():
    _setup()
    assert _call(amo._apply_fix_eq_tex, {"chunk": "ch01", "selector": "body[0]",
                 "expect": r"a^{x}^{y}", "new": r"a^{x y}"}) == "applied"
    assert _block(0)["tex"] == r"a^{x y}"
    # noop（再套已等於 new）/ skip-drift（expect 對不上）/ 非 eq / 越界
    assert _call(amo._apply_fix_eq_tex, {"chunk": "ch01", "selector": "body[0]",
                 "expect": r"a^{x}^{y}", "new": r"a^{x y}"}) == "noop"
    assert _call(amo._apply_fix_eq_tex, {"chunk": "ch01", "selector": "body[0]",
                 "expect": r"WRONG", "new": r"zzz"}) == "skip-drift"
    assert _block(0)["tex"] == r"a^{x y}"
    assert _call(amo._apply_fix_eq_tex, {"chunk": "ch01", "selector": "body[1]",
                 "expect": "x", "new": "y"}) == "skip-drift"
    assert _call(amo._apply_fix_eq_tex, {"chunk": "ch01", "selector": "body[99]",
                 "expect": "x", "new": "y"}) == "skip-drift"
    # 缺 expect → raise（spec 錯，非 drift）
    try:
        _call(amo._apply_fix_eq_tex, {"chunk": "ch01", "selector": "body[0]", "new": "z"})
        assert False, "缺 expect 應 raise"
    except ValueError:
        pass


def test_fix_inline_math():
    _setup()
    md = _block(1)["md"]
    anchor = overlay_anchor({"md": md})
    assert _call(amo._apply_fix_inline_math, {"chunk": "ch01", "selector": "body[1]",
                 "field": "md", "anchor": anchor, "old": r"$5\AA$", "new": r"$5\,\text{Å}$"}) == "applied"
    assert r"$5\,\text{Å}$" in _block(1)["md"]
    # noop（old 不在、new 在）
    assert _call(amo._apply_fix_inline_math, {"chunk": "ch01", "selector": "body[1]",
                 "field": "md", "old": r"$5\AA$", "new": r"$5\,\text{Å}$"}) == "noop"
    # anchor 漂移 → skip-drift（不動）
    before = _block(2)["md"]
    assert _call(amo._apply_fix_inline_math, {"chunk": "ch01", "selector": "body[2]",
                 "field": "md", "anchor": "deadbeef", "old": "plain", "new": "X"}) == "skip-drift"
    assert _block(2)["md"] == before
    # old 找不到且 new 也不在 → skip-drift；old==new → noop
    assert _call(amo._apply_fix_inline_math, {"chunk": "ch01", "selector": "body[2]",
                 "field": "md", "old": "NOPE", "new": "X"}) == "skip-drift"
    assert _call(amo._apply_fix_inline_math, {"chunk": "ch01", "selector": "body[2]",
                 "field": "md", "old": "plain", "new": "plain"}) == "noop"
    # 不支援 field → raise
    try:
        _call(amo._apply_fix_inline_math, {"chunk": "ch01", "selector": "body[2]", "field": "tex",
              "old": "a", "new": "b"})
        assert False, "field=tex 應 raise"
    except ValueError:
        pass


def test_title_and_caption_branches():
    """anchor key 必須 = field（title→'title'、caption→'caption'）；填錯 key 必 skip-drift。
    這正是 SOP 曾教錯的路徑，鎖死 code 行為防回歸。"""
    _setup()
    # title：selector='title'，anchor key 必須是 'title'
    t = _field("title")
    assert _call(amo._apply_fix_inline_math, {"chunk": "ch01", "selector": "title", "field": "title",
                 "anchor": overlay_anchor({"title": t}), "old": r"$\AA$", "new": r"$\text{Å}$"}) == "applied"
    assert r"$\text{Å}$" in _field("title")
    # 用錯 key（'md'）算 anchor → 對 title 失配 → skip-drift（證明 code 要求 key==field）
    _setup()
    t = _field("title")
    assert _call(amo._apply_fix_inline_math, {"chunk": "ch01", "selector": "title", "field": "title",
                 "anchor": overlay_anchor({"md": t}), "old": r"$\AA$", "new": r"$\text{Å}$"}) == "skip-drift"
    assert _field("title") == t  # 未動
    # caption 分支
    _setup()
    cap = _block(4)["caption"]
    assert _call(amo._apply_fix_inline_math, {"chunk": "ch01", "selector": "body[4]", "field": "caption",
                 "anchor": overlay_anchor({"caption": cap}), "old": r"$\AA$", "new": r"$\text{Å}$"}) == "applied"
    assert r"$\text{Å}$" in _block(4)["caption"]


def test_all_replace_occ_gt_1():
    """同欄同式 ×2：預設只換首處；all=true 一次換光（解 occ>1 清不掉的洞）。"""
    _setup()
    assert _call(amo._apply_fix_inline_math, {"chunk": "ch01", "selector": "body[3]", "field": "md",
                 "old": r"$\bad$", "new": r"$good$"}) == "applied"
    assert _block(3)["md"].count(r"$good$") == 1 and _block(3)["md"].count(r"$\bad$") == 1  # 只換首處
    _setup()
    assert _call(amo._apply_fix_inline_math, {"chunk": "ch01", "selector": "body[3]", "field": "md",
                 "old": r"$\bad$", "new": r"$good$", "all": True}) == "applied"
    assert _block(3)["md"].count(r"$good$") == 2 and r"$\bad$" not in _block(3)["md"]  # 全換


def test_apply_overrides_build_catalogs_gate():
    """apply_overrides：有 applied → build_catalogs 跑一次；全 noop → 不跑（monkeypatch 隔離，不依賴真 catalog 結構）。"""
    _setup()
    calls = []
    orig = amo.build_catalogs
    amo.build_catalogs = lambda s: calls.append(s)
    try:
        amo.OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
        OV_FILE.write_text(json.dumps({"overrides": [
            {"action": "fix_eq_tex", "chunk": "ch01", "selector": "body[0]",
             "expect": r"a^{x}^{y}", "new": r"a^{xy}"}]}), encoding="utf-8")
        stats = amo.apply_overrides(SLUG)
        assert stats.get("fix_eq_tex:applied") == 1 and calls == [SLUG], (stats, calls)
        calls.clear()
        stats2 = amo.apply_overrides(SLUG)  # 重跑 → noop
        assert stats2.get("fix_eq_tex:noop") == 1 and calls == [], (stats2, calls)
        # 不支援 action → raise
        OV_FILE.write_text(json.dumps({"overrides": [{"action": "bogus"}]}), encoding="utf-8")
        try:
            amo.apply_overrides(SLUG)
            assert False, "bogus action 應 raise"
        except ValueError:
            pass
    finally:
        amo.build_catalogs = orig


def test_locator_to_target():
    assert locator_to_target("ch03:body[7]") == {"chunk": "ch03", "selector": "body[7]"}
    assert locator_to_target("ch03:problem[5].body[2]") == {"chunk": "ch03", "selector": "problem:5:body[2]"}
    assert locator_to_target("ch03:problem[5].solution[1]") == {"chunk": "ch03", "selector": "problem:5:solution[1]"}
    assert locator_to_target("app01:title") == {"chunk": "app01", "selector": "title"}


def _finding(tex, targets, display=False):
    return {"category": "double_script", "detail": None, "err": "e",
            "tex": tex, "display": display, "occ": 1, "locators": [], "targets": targets}


def test_finding_to_override_eq_self_contained():
    # fix_eq_tex：完全自足，expect=finding.tex、new=裸 new、無 anchor
    f = _finding(r"a^{x}^{y}", [{"chunk": "ch01", "selector": "body[0]", "field": "tex"}], display=True)
    ov = amo.finding_to_override("bk", f, r"a^{x y}")
    assert ov["action"] == "fix_eq_tex"
    assert ov["chunk"] == "ch01" and ov["selector"] == "body[0]"
    assert ov["expect"] == r"a^{x}^{y}" and ov["new"] == r"a^{x y}"
    assert "anchor" not in ov and "old" not in ov
    assert ov["id"].startswith("bk-ch01-body-0-")


def test_finding_to_override_inline_with_fieldvalue():
    # fix_inline_math + field_value：用 _math_regions 精確取 old（含原樣定界）、new 同定界、附 anchor
    fv = r"see $5\AA$ here"
    f = _finding(r"5\AA", [{"chunk": "ch01", "selector": "body[1]", "field": "md"}])
    ov = amo.finding_to_override("bk", f, r"5\text{Å}", field_value=fv)
    assert ov["action"] == "fix_inline_math" and ov["field"] == "md"
    assert ov["old"] == r"$5\AA$" and ov["new"] == r"$5\text{Å}$"
    assert ov["anchor"] == overlay_anchor({"md": fv})


def test_finding_to_override_inline_fallback_no_anchor():
    # 無 field_value → best-effort $tex$ 重建、不附 anchor（apply 端 old 對不上只 skip-drift）
    f = _finding(r"\bad", [{"chunk": "ch01", "selector": "body[3]", "field": "md"}], display=False)
    ov = amo.finding_to_override("bk", f, r"\good")
    assert ov["old"] == r"$\bad$" and ov["new"] == r"$\good$" and "anchor" not in ov
    fd = _finding(r"\bad", [{"chunk": "ch01", "selector": "body[3]", "field": "md"}], display=True)
    assert amo.finding_to_override("bk", fd, r"\good")["old"] == r"$$\bad$$"


def test_finding_to_override_no_targets_raises():
    try:
        amo.finding_to_override("bk", _finding("x", []), "y")
        assert False, "無 targets 應 raise"
    except ValueError:
        pass


def test_finding_to_override_empty_tex_raises():
    # 空 tex → fallback 會產 old="$$" 誤改任何含 $$ 的欄 → 必 raise
    tgt = [{"chunk": "ch01", "selector": "body[0]", "field": "md"}]
    for bad in ("", "   ", None):
        try:
            amo.finding_to_override("bk", _finding(bad, tgt), "y")
            assert False, f"空 tex({bad!r}) 應 raise"
        except ValueError:
            pass


def test_finding_to_override_roundtrip_apply():
    # 端到端契約：finding → override → apply_overrides 實際修好（eq + inline 各一）
    _setup()
    orig = amo.build_catalogs
    amo.build_catalogs = lambda slug: None   # 隔離 build_catalogs（無 book.json fixture，比照 gate 測試）
    try:
        f_eq = _finding(r"a^{x}^{y}", [{"chunk": "ch01", "selector": "body[0]", "field": "tex"}], True)
        md_fv = _block(1)["md"]                                # "see $5\AA$ here"
        f_in = _finding(r"5\AA", [{"chunk": "ch01", "selector": "body[1]", "field": "md"}])
        ovs = [amo.finding_to_override(SLUG, f_eq, r"a^{x y}"),
               amo.finding_to_override(SLUG, f_in, r"5\text{Å}", field_value=md_fv)]
        OV_FILE.write_text(json.dumps({"overrides": ovs}, ensure_ascii=False), encoding="utf-8")
        stats = amo.apply_overrides(SLUG)
        assert stats.get("fix_eq_tex:applied") == 1 and stats.get("fix_inline_math:applied") == 1, stats
        assert _block(0)["tex"] == r"a^{x y}"
        assert _block(1)["md"] == r"see $5\text{Å}$ here"
    finally:
        amo.build_catalogs = orig
        _teardown()


if __name__ == "__main__":
    try:
        test_fix_eq_tex();                    print("✓ fix_eq_tex：applied/noop/skip-drift/非eq/越界/缺expect")
        test_fix_inline_math();               print("✓ fix_inline_math：applied/noop/anchor漂移/old缺/old==new/壞field")
        test_title_and_caption_branches();    print("✓ title/caption 分支 + anchor key==field（SOP P1 回歸鎖）")
        test_all_replace_occ_gt_1();          print("✓ all 替換（occ>1 同欄同式）")
        test_apply_overrides_build_catalogs_gate(); print("✓ apply_overrides：build_catalogs gate + 不支援 action raise")
        test_locator_to_target();             print("✓ locator→selector 橋接")
        test_finding_to_override_eq_self_contained(); print("✓ finding→override：eq 自足")
        test_finding_to_override_inline_with_fieldvalue(); print("✓ finding→override：inline 精確 old/anchor")
        test_finding_to_override_inline_fallback_no_anchor(); print("✓ finding→override：inline fallback 無 anchor")
        test_finding_to_override_no_targets_raises(); print("✓ finding→override：無 targets raise")
        test_finding_to_override_empty_tex_raises(); print("✓ finding→override：空 tex raise（防誤改）")
        test_finding_to_override_roundtrip_apply(); print("✓ finding→override→apply round-trip 修復 eq+inline")
    finally:
        _teardown()
    print("\n全部通過 ✅")
