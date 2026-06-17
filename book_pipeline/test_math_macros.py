"""math_macros.json 健全性 + reader codegen 一致性。

跑：uv run python -m book_pipeline.test_math_macros
"""
import json

from build import gen_macros


def test_json_valid_and_well_formed():
    raw = json.loads(gen_macros.MACROS_JSON.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and "macros" in raw
    macros = raw["macros"]
    assert macros, "macros 不可為空"
    for cs, val in macros.items():
        assert cs.startswith("\\"), f"巨集名須以反斜線起頭: {cs!r}"
        if isinstance(val, list):
            assert len(val) in (2, 3), f"{cs}: [body, nargs(, default)]"
            assert isinstance(val[0], str) and isinstance(val[1], int), f"{cs}: 型別錯"
            assert 0 <= val[1] <= 9, f"{cs}: nargs 超界"
        else:
            assert isinstance(val, str), f"{cs}: 值須為 str 或 list"


def test_no_ocr_glue_pseudomacros():
    """故意不收 OCR 黏字偽巨集（定義會掩蓋真錯）。守住此邊界。"""
    macros = gen_macros.load_macros()
    for forbidden in ("\\Nu", "\\muA", "\\cdotE", "\\Chi", "\\Zeta", "\\cdota", "\\cdotL"):
        assert forbidden not in macros, f"不應定義 OCR 黏字偽巨集 {forbidden}"


def test_reader_inline_matches_json():
    """reader 內聯 macros 區段必須 == 由 JSON codegen 的結果。
    防『改了 math_macros.json 卻沒跑 gen_macros 就 commit』導致 reader 與驗證器漂移。"""
    macros = gen_macros.load_macros()
    expected = gen_macros.render_macros_block(macros)
    js = gen_macros.READER_JS.read_text(encoding="utf-8")
    assert expected in js, (
        "reader 的 MACROS 區段與 math_macros.json 不一致 → 跑 "
        "`uv run python -m build.gen_macros` 後再 commit"
    )


def test_reader_loads_required_packages():
    """\\unicode / \\enclose 巨集需 reader 載對應套件，否則瀏覽器端會壞。"""
    js = gen_macros.READER_JS.read_text(encoding="utf-8")
    assert "unicode" in js and "enclose" in js, "reader 須載 unicode + enclose 套件"


if __name__ == "__main__":
    test_json_valid_and_well_formed();   print("✓ JSON 合法且格式正確")
    test_no_ocr_glue_pseudomacros();     print("✓ 未收 OCR 黏字偽巨集")
    test_reader_inline_matches_json();   print("✓ reader 內聯區段 == JSON（codegen 一致）")
    test_reader_loads_required_packages(); print("✓ reader 載 unicode + enclose")
    print("\n全部通過 ✅")
