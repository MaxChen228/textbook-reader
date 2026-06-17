#!/usr/bin/env python3
"""build/gen_macros.py — 把 book_pipeline/math_macros.json 的 macros codegen 注入
assets/qbank-shared.js 的 mathJaxConfig.tex.macros（MACROS:BEGIN/END marker 區段）。

為何 codegen 而非 fetch：qbank-shared.js 的 openPrintWindow 會 JSON.stringify(mathJaxConfig)
寫進新視窗，fetch 在新視窗不可靠；內聯則 print window 自動繼承、零競態。reader 與
驗證器（render_check.js 直接讀 JSON）共用同一份 math_macros.json → 零漂移。

build_all 部署唯一路徑會先呼叫 run()，故 reader 上站時 macros 必為最新。冪等。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MACROS_JSON = ROOT / "book_pipeline" / "math_macros.json"
READER_JS = ROOT / "assets" / "qbank-shared.js"

BEGIN = "/* MACROS:BEGIN"
END = "/* MACROS:END */"


def load_macros() -> dict:
    raw = json.loads(MACROS_JSON.read_text(encoding="utf-8"))
    return raw.get("macros", raw) if isinstance(raw, dict) else {}


def render_macros_block(macros: dict) -> str:
    """產生 BEGIN..END 之間（含 marker）的完整文字。8 空白縮排對齊 tex: 物件層。"""
    lines = [
        "        " + BEGIN + " — generated from book_pipeline/math_macros.json"
        " by build/gen_macros.py; do not edit by hand */",
        "        macros: {",
    ]
    for key in sorted(macros):
        val = macros[key]
        lines.append(f"          {json.dumps(key, ensure_ascii=False)}: "
                     f"{json.dumps(val, ensure_ascii=False)},")
    lines.append("        },")
    lines.append("        " + END)
    return "\n".join(lines)


def apply_to_text(js: str, block: str) -> str:
    if BEGIN not in js or END not in js:
        raise RuntimeError(f"找不到 MACROS marker，請先在 {READER_JS} 的 tex: 內加 BEGIN/END 區段")
    start = js.index(BEGIN)
    # 回退到該行行首（保留前置縮排由 block 自帶）
    line_start = js.rfind("\n", 0, start) + 1
    end = js.index(END) + len(END)
    return js[:line_start] + block + js[end:]


def run() -> bool:
    """注入並寫回；內容無變化回 False（冪等）。"""
    macros = load_macros()
    block = render_macros_block(macros)
    js = READER_JS.read_text(encoding="utf-8")
    new = apply_to_text(js, block)
    if new == js:
        return False
    READER_JS.write_text(new, encoding="utf-8")
    return True


if __name__ == "__main__":
    changed = run()
    print(f"gen_macros: {'updated' if changed else 'unchanged'} {READER_JS.relative_to(ROOT)} "
          f"({len(load_macros())} macros)")
