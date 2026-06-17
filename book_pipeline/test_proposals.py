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


if __name__ == "__main__":
    test_real_store_lints_clean();              print("✓ 真實 store lint 乾淨")
    test_real_index_in_sync();                  print("✓ _index.md 與 store 同步")
    test_lint_catches_bad_vocab();              print("✓ lint 抓 domain/type/status/id/reject-code")
    test_lint_passes_valid_reject_codes();      print("✓ 合法 reject 代碼通過")
    test_propose_resolve_roundtrip();           print("✓ propose/resolve round-trip + 時戳")
    test_id_collision_bumps_suffix();           print("✓ id O_EXCL 去重（-2 後綴）")
    test_propose_rejects_unknown_domain_and_type(); print("✓ 未知 domain/type 拒收")
    print("\n全部通過 ✅")
