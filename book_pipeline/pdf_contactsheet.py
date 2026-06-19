#!/usr/bin/env python3
"""book_pipeline.pdf_contactsheet — 抽樣頁拼成單張 contact sheet PNG。

省 token 的視覺驗證：把 PDF 均勻抽 N 頁渲染後拼成一張圖，LLM 一次 vision
呼叫即可判斷「是否正確書/版次、清晰度、掃描或數位、是否完整」，免逐頁多次呼叫。
只對 pdf_triage 判 needs_llm=True 的書跑。

用法：
  uv run --with pymupdf --with pillow python -m book_pipeline.pdf_contactsheet \
      <slug|pdf路徑> [--pages 6] [--out 路徑] [--zoom 1.3]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import fitz
from PIL import Image, ImageDraw

from book_pipeline.cpu_gate import cpu_bound

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, 'raw_pdfs')
SLUG_MAP = os.path.join(ROOT, 'book_pipeline', 'slug_map.json')
OUT_DIR = os.path.join(ROOT, 'book_pipeline', 'reports', 'contactsheets')

CELL_W = 760  # 每格寬（px），兼顧清晰與整體尺寸
COLS = 2
PAD = 8
LABEL_H = 22


def _resolve(arg: str) -> str | None:
    if os.path.isfile(arg):
        return arg
    cand = os.path.join(RAW, f'{arg}.pdf')
    if os.path.isfile(cand):
        return cand
    try:
        m = (json.load(open(SLUG_MAP)) or {}).get('map', {})
        for fn, slug in m.items():
            if slug == arg and os.path.isfile(os.path.join(RAW, fn)):
                return os.path.join(RAW, fn)
    except Exception:
        pass
    return None


def _pick_pages(n: int, k: int) -> list[int]:
    """均勻取 k 頁，跳過封面/索引區（落在 6%–94%）。"""
    if n <= k:
        return list(range(n))
    lo, hi = 0.06, 0.94
    return [min(n - 1, int((lo + (hi - lo) * i / (k - 1)) * n)) for i in range(k)]


@cpu_bound('contactsheet')
def contactsheet(path: str, out: str, k: int = 6, zoom: float = 1.3) -> str:
    doc = fitz.open(path)
    n = doc.page_count
    idxs = _pick_pages(n, k)

    cells = []
    for i in idxs:
        page = doc[i]
        # width-bounded zoom：最終只需 CELL_W 寬，渲染解析度就綁在「略大於 CELL_W」即可，
        # 與頁面實體尺寸脫鉤。掃描書 mediabox/嵌圖巨大時，固定 zoom 會全解析度解碼再 downscale
        # 丟掉（>30s 像卡死，正是 qc agent 燒掉 10–30 工具調用跟渲染器搏鬥的根因）。封頂後 ≤幾秒。
        pw = page.rect.width or CELL_W
        eff_zoom = min(zoom, (CELL_W * 1.1) / pw)
        pix = page.get_pixmap(matrix=fitz.Matrix(eff_zoom, eff_zoom))
        img = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
        scale = CELL_W / img.width
        img = img.resize((CELL_W, int(img.height * scale)))
        cells.append((i + 1, img))
    doc.close()

    cell_h = max(im.height for _, im in cells)
    rows = math.ceil(len(cells) / COLS)
    W = COLS * CELL_W + (COLS + 1) * PAD
    H = rows * (cell_h + LABEL_H) + (rows + 1) * PAD
    sheet = Image.new('RGB', (W, H), 'white')
    draw = ImageDraw.Draw(sheet)

    for idx, (pno, im) in enumerate(cells):
        r, c = divmod(idx, COLS)
        x = PAD + c * (CELL_W + PAD)
        y = PAD + r * (cell_h + LABEL_H + PAD)
        draw.text((x, y), f'p.{pno}', fill='black')
        sheet.paste(im, (x, y + LABEL_H))

    os.makedirs(os.path.dirname(out), exist_ok=True)
    # 原子寫：先寫 .tmp 再 rename。渲染慢/中途被 SIGKILL（daemon kick -k）時，絕不留半寫入壞檔
    # ——qc agent 曾親撞「contactsheet PNG 是半寫入壞檔」、再燒一輪工具調用繞它。
    tmp = out + '.tmp'
    sheet.save(tmp, 'PNG')
    os.replace(tmp, out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description='PDF 抽樣頁 contact sheet')
    ap.add_argument('target', help='slug 或 PDF 路徑')
    ap.add_argument('--pages', type=int, default=6)
    ap.add_argument('--zoom', type=float, default=1.3)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    path = _resolve(args.target)
    if not path:
        ap.error(f'找不到：{args.target}')
    stem = os.path.splitext(os.path.basename(path))[0]
    out = args.out or os.path.join(OUT_DIR, f'{stem}.png')
    print(contactsheet(path, out, k=args.pages, zoom=args.zoom))
    return 0


if __name__ == '__main__':
    sys.exit(main())
