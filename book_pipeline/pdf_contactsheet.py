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
import queue as _queue
import sys
import time

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


def _render_one(doc, i: int, zoom: float) -> Image.Image:
    """單頁 → CELL_W 寬的 PIL 影像。width-bounded zoom：渲染解析度綁「略大於 CELL_W」、與頁面實體尺寸
    脫鉤——掃描書 mediabox/嵌圖巨大時，固定 zoom 會全解析度解碼再 downscale 丟掉（>30s 像卡死，正是
    qc agent 燒 10–30 工具調用跟渲染器搏鬥的根因）。封頂後 ≤幾秒。"""
    page = doc[i]
    pw = page.rect.width or CELL_W
    eff_zoom = min(zoom, (CELL_W * 1.1) / pw)
    pix = page.get_pixmap(matrix=fitz.Matrix(eff_zoom, eff_zoom))
    img = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
    scale = CELL_W / img.width
    return img.resize((CELL_W, int(img.height * scale)))


def _assemble(cells: list[tuple[int, Image.Image]], out: str) -> str:
    """把 (頁碼, 影像) 拼成 contact sheet、原子寫出。cells 可為部分頁（逾時殘存）——只要 ≥1 頁就出圖。"""
    cells = sorted(cells, key=lambda c: c[0])
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


def _render_proc(path: str, k: int, zoom: float, q) -> None:
    """子進程入口：**逐頁漸進式**把渲好的 cell 餵回 queue（('cell',頁碼,raw_rgb,w,h)），全部完成放 ('done',)，
    例外放 ('err',msg)。漸進式是關鍵——父進程逾時硬 kill 時仍能用「已抵達的頁」拼部分 sheet，
    不再因「6 頁中某 1 頁病態卡 fitz C 層」連累整張被殺、誤殺好書（#30 false-kill 向量）。"""
    try:
        doc = fitz.open(path)
        for i in _pick_pages(doc.page_count, k):
            img = _render_one(doc, i, zoom)
            q.put(('cell', i + 1, img.tobytes(), img.width, img.height))
        doc.close()
        q.put(('done',))
    except BaseException as e:  # noqa: BLE001 — 連 C 層 abort 外的都回報，不讓子進程靜默死
        q.put(('err', f'{type(e).__name__}: {e}'))


@cpu_bound('contactsheet')
def contactsheet(path: str, out: str, k: int = 6, zoom: float = 1.3,
                 timeout_s: int = RENDER_TIMEOUT) -> str:
    """渲染 contact sheet，硬封頂 timeout_s 秒。**漸進式 + 部分容錯**：子進程逐頁餵回，父進程在 deadline
    前盡量收；逾時則用「已收到的頁」拼出部分 sheet 照常回傳——只有「連一頁都出不來」（PDF 損壞/首頁就
    卡 C 層）才 raise TimeoutError → qc agent 才判 reject。如此好書中夾 1 頁病態圖不會被誤殺（#30）。

    為何子進程：fitz C 層在損壞/超大 PDF 上會卡死且不理會 Python signal（SIGALRM 無效）；唯有把工作丟
    可硬 kill 的子進程才能保證 wall-clock 有界。spawn context（daemon 內有 thread，fork 不安全）。"""
    ctx = mp.get_context('spawn')
    q = ctx.Queue()
    p = ctx.Process(target=_render_proc, args=(path, k, zoom, q))
    p.start()
    cells: list[tuple[int, Image.Image]] = []
    err: str | None = None
    start = time.monotonic()
    while True:
        left = timeout_s - (time.monotonic() - start)
        if left <= 0:
            break  # deadline 到：用已收到的頁（可能 0）
        try:
            msg = q.get(timeout=left)
        except _queue.Empty:
            break
        if msg[0] == 'cell':
            _, pno, raw, w, h = msg
            cells.append((pno, Image.frombytes('RGB', (w, h), raw)))
        elif msg[0] == 'done':
            break
        elif msg[0] == 'err':
            err = msg[1]; break
    if p.is_alive():
        p.terminate(); p.join(5)
        if p.is_alive():
            p.kill(); p.join()
    if cells:  # 至少一頁 → 出圖（部分亦可，讓 agent 依可見內容判斷，不因 tooling 逾時誤殺好書）
        if len(cells) < k:
            print(f'⚠ contactsheet 部分渲染：{len(cells)}/{k} 頁（其餘逾 {timeout_s}s/異常，已用殘存頁出圖）',
                  file=sys.stderr, flush=True)
        return _assemble(cells, out)
    if err:  # 連一頁都沒出且子進程報錯
        raise RuntimeError(f'contactsheet 渲染失敗：{err}')
    raise TimeoutError(
        f'contactsheet 渲染逾 {timeout_s}s 連一頁都出不來（PDF 可能損壞/首頁卡 C 層/嵌入巨圖），已硬中止')


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
