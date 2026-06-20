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
import multiprocessing as mp
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

# 硬 wall-clock 上限：width-bounded zoom 已治 per-page 巨圖解碼，但 `fitz.open` 與 C 層
# 解碼在「損壞 xref / 67MB 掃描 / 嵌入巨圖」時仍會卡死整場（CPU 低、無輸出）——qc agent 親撞
# 過、燒滿 60min timeout 才放棄、產不出 verdict。SIGALRM 攔不住 C 層卡死，唯一穩的是把 render
# 丟子進程、逾時硬 kill。預設 90s（合法大書 width-bounded 後 ≤幾秒、永遠用不到；只封頂災難）。
RENDER_TIMEOUT = int(os.environ.get('BOOK_PIPELINE_CONTACTSHEET_TIMEOUT', '90'))


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


def _render(path: str, out: str, k: int, zoom: float) -> str:
    """純渲染工作（在子進程跑，受 RENDER_TIMEOUT 硬封頂）。不持 cpu_slot——由外層 contactsheet 持。"""
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


def _render_proc(path: str, out: str, k: int, zoom: float, q) -> None:
    """子進程入口：成功把 out 路徑放進 queue；任何例外把訊息放進 queue（不靠 exitcode 傳因）。"""
    try:
        q.put(('ok', _render(path, out, k, zoom)))
    except BaseException as e:  # noqa: BLE001 — 連 C 層 abort 外的都回報，不讓子進程靜默死
        q.put(('err', f'{type(e).__name__}: {e}'))


@cpu_bound('contactsheet')
def contactsheet(path: str, out: str, k: int = 6, zoom: float = 1.3,
                 timeout_s: int = RENDER_TIMEOUT) -> str:
    """渲染 contact sheet，硬封頂 timeout_s 秒。逾時 → 子進程硬 kill + TimeoutError（含明確訊息），
    讓 qc agent 秒級得知「此 PDF 無法在有界時間內渲染」→ 直接判 reject，不再卡滿 60min daemon 上限。

    為何子進程：fitz C 層在損壞/超大 PDF 上會卡死且不理會 Python signal（SIGALRM 無效）；唯有
    把工作丟可硬 kill 的子進程才能保證 wall-clock 有界。spawn context（daemon 內有 thread，fork 不安全）。"""
    ctx = mp.get_context('spawn')
    q = ctx.Queue()
    p = ctx.Process(target=_render_proc, args=(path, out, k, zoom, q))
    p.start()
    p.join(timeout_s)
    if p.is_alive():
        p.terminate(); p.join(5)
        if p.is_alive():
            p.kill(); p.join()
        raise TimeoutError(
            f'contactsheet 渲染逾 {timeout_s}s 仍未完成（PDF 可能損壞/超大掃描/嵌入巨圖），已硬中止')
    try:
        # 用 get(timeout) 而非 get_nowait：子進程正常結束時 Queue feeder thread 可能尚未把小 payload
        # flush 進 pipe，get_nowait 會誤撲空。小 payload + 5s 餘裕足以覆蓋該競態。
        kind, payload = q.get(timeout=5)
    except Exception:
        raise RuntimeError(f'contactsheet 子進程異常結束（exitcode={p.exitcode}）、無結果回傳')
    if kind == 'err':
        raise RuntimeError(f'contactsheet 渲染失敗：{payload}')
    return payload


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
    try:
        print(contactsheet(path, out, k=args.pages, zoom=args.zoom))
    except (TimeoutError, RuntimeError) as e:
        # 渲染無法在有界時間內完成 = 此 PDF 不可用於視覺 QC。明確非零退出，qc agent 據此直接 reject，
        # 不必自行跟卡死的渲染器搏鬥（過去那是燒滿 60min daemon 上限、產不出 verdict 的根因）。
        print(f'CONTACTSHEET_UNRENDERABLE: {e}', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
