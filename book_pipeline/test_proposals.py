"""proposals 通用建議系統單測：真實 store lint/同步 + schema 詞彙守衛 + propose/resolve
round-trip + id O_EXCL 去重。

跑：uv run python -m book_pipeline.test_proposals
"""
import tempfile
from pathlib import Path

from book_pipeline import proposals as P


# ── 真實 store（committed 提案）必須恆 lint 乾淨、_index.md 與 store 同步 ──────────
def test_real_store_lints_clean():
    errs = P.lint(P.load_all())
    assert not errs, f"真實 store lint 失敗：{errs}"


def test_real_index_in_sync():
    if P.INDEX.is_file():
        assert P.INDEX.read_text(encoding="utf-8") == P.render(P.load_all()), \
            "_index.md 與 store 不同步 → 跑 `proposals render`"


# ── schema 詞彙守衛（用假紀錄打 lint，不碰真實 store）──────────────────────────
def test_lint_catches_bad_vocab():
    bad = [
        {"id": "nope", "domain": "math", "type": "macro", "status": "proposed"},        # id 格式
        {"id": "P-2026-06-17-x", "domain": "unknown", "type": "macro", "status": "proposed"},  # domain
        {"id": "P-2026-06-17-y", "domain": "math", "type": "spell", "status": "proposed"},      # type
        {"id": "P-2026-06-17-z", "domain": "math", "type": "macro", "status": "weird"},          # status
        {"id": "P-2026-06-17-a", "domain": "math", "type": "macro", "status": "accepted",
         "resolution": ""},                                                                       # 已決議缺 resolution
        {"id": "P-2026-06-17-b", "domain": "math", "type": "macro", "status": "rejected",
         "resolution": "made-up-code"},                                                           # 非受控 reject code
    ]
    errs = P.lint(bad)
    assert len(errs) >= len(bad), f"應抓到每筆問題，實得：{errs}"
    assert any("id" in e for e in errs)
    assert any("domain" in e for e in errs)
    assert any("理由代碼" in e for e in errs)


def test_lint_passes_valid_reject_codes():
    rec = {"id": "P-2026-06-17-ok", "domain": "math", "type": "macro", "status": "rejected",
           "resolution": "pseudo-macro-guard semantically-ambiguous"}
    # 注意：id 對應檔不存在 → 會有「檔名不符」一條；過濾掉只驗詞彙部分
    errs = [e for e in P.lint([rec]) if "檔名" not in e]
    assert not errs, f"合法 reject 代碼不該報錯：{errs}"


# ── propose / resolve round-trip + id O_EXCL 去重（重導 STORE 到 tmp，不污染真實）──
def _with_tmp_store(fn):
    orig_store, orig_index = P.STORE, P.INDEX
    with tempfile.TemporaryDirectory() as d:
        P.STORE = Path(d) / "proposals.d"
        P.INDEX = P.STORE / "_index.md"
        try:
            fn()
        finally:
            P.STORE, P.INDEX = orig_store, orig_index


def test_propose_resolve_roundtrip():
    def body():
        pid = P.propose(domain="math", type_="normalize-rule", title="測試規則",
                        slug="probe", detect=["\\foo"], source="unittest",
                        evidence="ev", proposal="pp", risk="rr")
        assert pid == "P-%s-probe" % P._today()
        rec = P.load_all()[0]
        assert rec["status"] == "proposed" and rec["detect"] == ["\\foo"]
        assert rec["source"] == "unittest" and rec["created"] and rec["updated"]
        assert not P.lint(P.load_all())
        P.resolve(pid, status="accepted", resolution="R9 _probe")
        rec = P.load_all()[0]
        assert rec["status"] == "accepted" and rec["resolution"] == "R9 _probe"
        assert not P.lint(P.load_all())
    _with_tmp_store(body)


def test_id_collision_bumps_suffix():
    def body():
        a = P.propose(domain="math", type_="macro", title="同名", slug="dup")
        b = P.propose(domain="math", type_="macro", title="同名", slug="dup")
        assert a != b, "同 slug 兩次應產生不同 id"
        assert b.endswith("-2"), f"第二筆應 -2 後綴：{b}"
        assert len(P.load_all()) == 2
    _with_tmp_store(body)


def test_propose_rejects_unknown_domain_and_type():
    def body():
        for kw in (dict(domain="ghost", type_="macro", title="x"),
                   dict(domain="math", type_="ghost", title="x")):
            try:
                P.propose(**kw)
                assert False, f"應 raise：{kw}"
            except ValueError:
                pass
    _with_tmp_store(body)


def test_select_and_resolve_many_batch():
    def body():
        a = P.propose(domain="engine", type_="patch", title="p1", slug="p1", source="scope_guard")
        b = P.propose(domain="engine", type_="patch", title="p2", slug="p2", source="scope_guard")
        c = P.propose(domain="math", type_="macro", title="m1", slug="m1", source="agent")
        # select_proposed 對稱於 list 的過濾，且只命中 proposed
        assert set(P.select_proposed(domain="engine", type_="patch", source="scope_guard")) == {a, b}
        staged, errs = P.resolve_many([a, b], status="superseded", resolution="已落地主線")
        assert not errs and len(staged) == 2
        st = {r["id"]: r["status"] for r in P.load_all()}
        assert st[a] == st[b] == "superseded" and st[c] == "proposed", "只動目標、不碰 c"
        assert P.select_proposed(domain="engine", type_="patch") == [], "已決議者不再入選"
        assert not P.lint(P.load_all())
    _with_tmp_store(body)


def test_resolve_many_is_transactional():
    def body():
        a = P.propose(domain="math", type_="macro", title="m1", slug="m1")
        b = P.propose(domain="math", type_="macro", title="m2", slug="m2")
        # 含不存在 id → 全批不寫（a 不得被改）
        _, errs = P.resolve_many([a, "P-2026-01-01-ghost"], status="superseded", resolution="x")
        assert errs and any("找不到" in e for e in errs)
        assert all(r["status"] == "proposed" for r in P.load_all()), "事務失敗不得落盤任何一條"
        # 非法 rejected code → 全批不寫
        _, errs2 = P.resolve_many([a, b], status="rejected", resolution="made-up-code")
        assert errs2 and any("理由代碼" in e for e in errs2)
        assert all(r["status"] == "proposed" for r in P.load_all())
        # dry-run（apply=False）只驗不寫
        staged, errs3 = P.resolve_many([a, b], status="rejected", resolution="out-of-scope", apply=False)
        assert not errs3 and len(staged) == 2
        assert all(r["status"] == "proposed" for r in P.load_all()), "dry-run 不得落盤"
        # 真正落盤
        _, errs4 = P.resolve_many([a, b], status="rejected", resolution="out-of-scope")
        assert not errs4 and all(r["status"] == "rejected" for r in P.load_all())
    _with_tmp_store(body)


# ── Pillar 1+2：parked 生命週期 / slug 持久化 / verified_at / stale ───────────────
def test_slug_persisted():
    def body():
        with_slug = P.propose(domain="math", type_="macro", title="x", slug="probe-slug")
        no_slug = P.propose(domain="math", type_="macro", title="無 slug 標題")
        by_id = {r["id"]: r for r in P.load_all()}
        assert by_id[with_slug]["slug"] == "probe-slug", "slug 須持久化進 JSON"
        assert by_id[no_slug]["slug"] is None, "無傳 slug → 欄為 None（不臆測）"
    _with_tmp_store(body)


def test_park_roundtrip():
    def body():
        pid = P.propose(domain="sol", type_="source-quality", title="解答源爛", slug="bk")
        staged, errs = P.park_many([pid], unblock_kind="re-source", unblock_target="找完整版")
        assert not errs and len(staged) == 1
        rec = P.load_all()[0]
        assert rec["status"] == "parked"
        assert rec["unblock"] == {"kind": "re-source", "target": "找完整版"}
        assert not rec.get("resolution"), "parked 不需 resolution"
        assert not P.lint(P.load_all()), "parked + unblock 應 lint 乾淨"
        assert P.select_proposed() == [], "parked 不再被 select_proposed 命中"
    _with_tmp_store(body)


def test_park_only_proposed_and_transactional():
    def body():
        a = P.propose(domain="sol", type_="source-quality", title="a", slug="a")
        b = P.propose(domain="sol", type_="source-quality", title="b", slug="b")
        P.resolve(a, status="superseded", resolution="已修")
        # 批次含已決議 a → 全批不寫（b 不得被 park）
        _, errs = P.park_many([a, b], unblock_kind="re-source", unblock_target="t")
        assert errs and any("只能 park proposed" in e for e in errs)
        assert P.load_all()  # b 仍 proposed
        st = {r["id"]: r["status"] for r in P.load_all()}
        assert st[a] == "superseded" and st[b] == "proposed", "事務失敗不得落盤任何一條"
    _with_tmp_store(body)


def test_park_requires_valid_kind():
    def body():
        pid = P.propose(domain="sol", type_="source-quality", title="x", slug="x")
        _, errs = P.park_many([pid], unblock_kind="bogus-kind", unblock_target="t")
        assert errs and any("unblock kind" in e for e in errs)
        assert P.load_all()[0]["status"] == "proposed", "非法 kind 不得落盤"
    _with_tmp_store(body)


def test_lint_parked_needs_unblock():
    # parked 無 unblock → 報錯；unblock.kind 非法 → 報錯（過濾檔名不符雜訊）
    no_ub = {"id": "P-2026-06-23-a", "domain": "sol", "type": "source-quality", "status": "parked"}
    bad_kind = {"id": "P-2026-06-23-b", "domain": "sol", "type": "source-quality", "status": "parked",
                "unblock": {"kind": "nope", "target": "t"}}
    errs = [e for e in P.lint([no_ub, bad_kind]) if "檔名" not in e]
    assert any("unblock.kind" in e and "須附" in e for e in errs), errs
    assert any("不在" in e for e in errs), errs


def test_verified_at_optional_and_structure():
    ok = {"id": "P-2026-06-23-c", "domain": "math", "type": "macro", "status": "proposed",
          "verified_at": {"sha": "abc1234", "date": "2026-06-23T00:00:00+00:00"}}
    bad = {"id": "P-2026-06-23-d", "domain": "math", "type": "macro", "status": "proposed",
           "verified_at": {"date": "no-sha"}}  # 缺 sha
    ok_errs = [e for e in P.lint([ok]) if "檔名" not in e]
    bad_errs = [e for e in P.lint([bad]) if "檔名" not in e]
    assert not ok_errs, f"合法 verified_at 不該報錯：{ok_errs}"
    assert any("verified_at" in e for e in bad_errs), bad_errs


def test_render_order_and_parked_stats():
    recs = [
        {"id": "P-2026-06-23-s", "domain": "sol", "type": "source-quality", "status": "superseded",
         "resolution": "r", "title": "S"},
        {"id": "P-2026-06-23-k", "domain": "sol", "type": "source-quality", "status": "parked",
         "unblock": {"kind": "re-source", "target": "t"}, "title": "K"},
        {"id": "P-2026-06-23-p", "domain": "sol", "type": "source-quality", "status": "proposed",
         "title": "P"},
    ]
    out = P.render(recs)
    # 排序：proposed(P) < parked(K) < superseded(S)
    ip, ik, is_ = out.index("— P"), out.index("— K"), out.index("— S")
    assert ip < ik < is_, f"order 應 proposed<parked<terminal：{ip},{ik},{is_}"
    assert "proposed=1 parked=1" in out, "統計列須分開計 proposed/parked"
    assert "解鎖條件：re-source → t" in out, "parked 須顯示 unblock"


def test_stale_candidates_pure():
    def since_fn(sha, paths):
        if sha == "gone":
            return None              # sha 不可達
        return ["abc123"] if (sha == "old" and paths) else []
    recs = [
        {"id": "P-1", "domain": "sol", "verified_at": {"sha": "old"}},     # stale（sol paths 非空）
        {"id": "P-2", "domain": "sol", "verified_at": {"sha": "cur"}},     # 未 stale
        {"id": "P-3", "domain": "sol"},                                     # 無 verified_at → 不納入
        {"id": "P-4", "domain": "crawl", "verified_at": {"sha": "old"}},   # crawl paths=[] → 未 stale
        {"id": "P-5", "domain": "engine", "verified_at": {"sha": "gone"}}, # 不可達 → 納入(touched=None)
    ]
    cands = P.stale_candidates(recs, since_fn)
    ids = {rec["id"]: touched for rec, touched in cands}
    assert set(ids) == {"P-1", "P-5"}, f"只 P-1(stale)+P-5(不可達)：{set(ids)}"
    assert ids["P-1"] == ["abc123"] and ids["P-5"] is None


# ── Pillar 3：verify 原始資料驗證通道（雙軸不變量 / trichotomy / 路由）─────────────
def test_verify_math_trichotomy():
    ctx = P.VerifyCtx()
    ctx._math = [{"tex": "\\Nu x", "total_occ": 5, "books": [{"slug": "bk", "occ": 5}]}]
    live = P._verify_math({"id": "P-1", "domain": "math", "detect": ["\\Nu"]}, ctx)
    assert live.verdict == "live" and live.can_auto_disposition and live.live_metric == 5
    res = P._verify_math({"id": "P-2", "domain": "math", "detect": ["\\Mu"]}, ctx)
    assert res.verdict == "resolved" and res.can_auto_disposition and res.live_metric == 0
    none = P._verify_math({"id": "P-3", "domain": "math", "detect": []}, ctx)
    assert none.verdict == "inconclusive" and none.can_auto_disposition is False


def test_verify_sol_always_inconclusive_even_at_100pct(monkeypatch):
    """鐵律不變量：配對率 100% 也絕不變 resolved／絕不進 live_metric 當結論。"""
    from book_pipeline import sol_extract as se
    monkeypatch.setattr(se, "edition_block_reason", lambda s: None)
    monkeypatch.setattr(se, "load_sol_rules_safe", lambda s: ({"ok": 1}, None))
    monkeypatch.setattr(se, "extract_sol_chapters", lambda s, r: {1: {"1.1": ["b"]}})
    monkeypatch.setattr(se, "merge_into_main", lambda m, d, dry_run=True: {
        "chapters": 1, "problems_total": 10, "problems_with_sol": 10,
        "sol_unmatched": 0, "per_ch": [(1, 10, 10, [])]})
    vr = P._verify_sol({"id": "P-x", "domain": "sol", "slug": "foo_sol"}, P.VerifyCtx())
    assert vr.verdict == "inconclusive" and vr.can_auto_disposition is False
    assert vr.live_metric is None, "100% 配對率也不得當結論"
    assert "100%" in vr.note and "僅輔助" in vr.note


def test_verify_sol_pending_reports_reason(monkeypatch):
    from book_pipeline import sol_extract as se
    monkeypatch.setattr(se, "edition_block_reason", lambda s: None)
    monkeypatch.setattr(se, "load_sol_rules_safe", lambda s: (None, "標記 _pending"))
    vr = P._verify_sol({"id": "P-x", "domain": "sol", "slug": "foo_sol"}, P.VerifyCtx())
    assert vr.verdict == "inconclusive" and "_pending" in vr.note


def test_verify_crawl_always_inconclusive(monkeypatch):
    from book_pipeline import editions as ed, booklists as bl
    monkeypatch.setattr(ed, "load", lambda s: {"version": {"value": "3", "matches_pref": True}})
    monkeypatch.setattr(ed, "dims", lambda s, e, r, h: {
        "eligible": True, "link": True, "version": True, "sol_alignment": True})
    monkeypatch.setattr(bl, "have_slugs", lambda: set())
    monkeypatch.setattr(bl, "load_resolution", lambda: {})
    monkeypatch.setattr(bl, "status_of", lambda s, h, r, edition=None: "OWNED")
    vr = P._verify_crawl({"id": "P-x", "domain": "crawl", "slug": "neamen"}, P.VerifyCtx())
    assert vr.verdict == "inconclusive" and vr.can_auto_disposition is False and vr.live_metric is None


def test_verify_catalog_routing():
    ctx = P.VerifyCtx()
    ctx._slug_of = lambda rec: None  # 強制無法定址路徑（不讀 disk）
    ctx._catalog["bk0"] = {"critical": 0, "findings": []}
    ctx._catalog["bk3"] = {"critical": 3, "findings": []}
    r0 = P._verify_catalog({"id": "P-1", "domain": "catalog", "slug": "bk0"}, ctx)
    assert r0.verdict == "resolved" and r0.can_auto_disposition and r0.live_metric == 0
    r3 = P._verify_catalog({"id": "P-2", "domain": "catalog", "slug": "bk3"}, ctx)
    assert r3.verdict == "live" and r3.live_metric == 3
    rn = P._verify_catalog({"id": "P-3", "domain": "catalog", "slug": None}, ctx)
    assert rn.verdict == "inconclusive" and rn.can_auto_disposition is False


def test_verify_fn_dispatch_and_unknown():
    assert P._verify_fn("math") is P._verify_math
    assert P._verify_fn("sol") is P._verify_sol
    assert P._verify_fn("nope") is P._verify_unknown
    vr = P._verify_unknown({"id": "P-x", "domain": "nope"}, P.VerifyCtx())
    assert vr.verdict == "inconclusive" and vr.can_auto_disposition is False


def test_stamp_verified_and_default_readonly():
    def body():
        pid = P.propose(domain="math", type_="macro", title="x", slug="x")
        assert "verified_at" not in P.load_all()[0], "propose 預設不蓋 verified_at"
        P.stamp_verified(pid, sha="abc1234", date="2026-06-23T00:00:00+00:00")
        rec = P.load_all()[0]
        assert rec["verified_at"] == {"sha": "abc1234", "date": "2026-06-23T00:00:00+00:00"}
        assert rec["status"] == "proposed", "stamp 不改 status（唯讀於決策態）"
        assert not P.lint(P.load_all())
    _with_tmp_store(body)


def test_verify_engine_caption_routing_status_agnostic():
    """已決議的 caption tooling-gap 提案 verify 路由須穩定（不隨 status 漂移走錯 smoke 分支）。"""
    ctx = P.VerifyCtx()
    ctx._slug_of = lambda rec: "bk"
    ctx._catalog["bk"] = {"critical": 0, "findings": []}
    base = {"id": "P-x", "domain": "engine", "type": "tooling-gap",
            "title": "figure caption 缺", "evidence": "empty_caption"}
    for st in ("proposed", "superseded", "accepted"):
        vr = P._verify_engine({**base, "status": st}, ctx)
        assert vr.metric_label == "catalog_critical", f"status={st} 應委派 catalog 而非 smoke"
        assert vr.verdict == "resolved"


# ── Pillar 4：frontier 生命週期聚合（零缺口三桶 + stale 跨切）─────────────────────
def test_frontier_view_buckets_and_stale():
    recs = [
        {"id": "P-1", "domain": "sol", "status": "proposed"},
        {"id": "P-2", "domain": "sol", "status": "parked"},
        {"id": "P-3", "domain": "math", "status": "superseded"},
        {"id": "P-4", "domain": "math", "status": "accepted"},
        {"id": "P-5", "domain": "engine", "status": "proposed"},
    ]
    fv = P.frontier_view(recs, stale_ids={"P-1", "P-3"})
    c = fv["counts"]
    # 零缺口：actionable + parked + terminal == total
    assert c["actionable"] + c["parked"] + c["terminal"] == fv["total"] == 5
    assert c["actionable"] == 2 and c["parked"] == 1 and c["terminal"] == 2
    # stale 只算未終態（P-3 superseded 雖在 stale_ids 也不計）
    assert fv["stale"] == ["P-1"] and c["stale"] == 1
    assert fv["by_domain"]["sol"] == {"actionable": 1, "parked": 1, "terminal": 0}


if __name__ == "__main__":
    test_real_store_lints_clean();              print("✓ 真實 store lint 乾淨")
    test_real_index_in_sync();                  print("✓ _index.md 與 store 同步")
    test_lint_catches_bad_vocab();              print("✓ lint 抓 domain/type/status/id/reject-code")
    test_lint_passes_valid_reject_codes();      print("✓ 合法 reject 代碼通過")
    test_propose_resolve_roundtrip();           print("✓ propose/resolve round-trip + 時戳")
    test_id_collision_bumps_suffix();           print("✓ id O_EXCL 去重（-2 後綴）")
    test_propose_rejects_unknown_domain_and_type(); print("✓ 未知 domain/type 拒收")
    test_select_and_resolve_many_batch();       print("✓ 批次選取 + resolve_many（只動目標、不碰已決議）")
    test_resolve_many_is_transactional();       print("✓ resolve_many 事務性 all-or-nothing + dry-run")
    test_slug_persisted();                      print("✓ slug 持久化")
    test_park_roundtrip();                      print("✓ park round-trip（parked+unblock）")
    test_park_only_proposed_and_transactional(); print("✓ park 只 proposed + 事務性")
    test_park_requires_valid_kind();            print("✓ park 須合法 kind")
    test_lint_parked_needs_unblock();           print("✓ lint：parked 須 unblock")
    test_verified_at_optional_and_structure();  print("✓ verified_at 可選 + 結構驗")
    test_render_order_and_parked_stats();       print("✓ render order + parked 統計")
    test_stale_candidates_pure();               print("✓ stale_candidates 純函式")
    test_frontier_view_buckets_and_stale();     print("✓ frontier 零缺口三桶 + stale 跨切")
    import pytest as _pt                          # monkeypatch fixture 測試走 pytest
    _pt.main([__file__, "-q", "-k", "verify or stamp"])
    print("✓ verify 雙軸/trichotomy/路由 + stamp（見上 pytest）")
    print("\n全部通過 ✅")
