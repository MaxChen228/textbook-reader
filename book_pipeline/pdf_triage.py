#!/usr/bin/env python3
"""book_pipeline.pdf_triage — PDF type 分類與堪用度判定（確定性，pymupdf，零 LLM）。

爬回的 PDF 在丟 MinerU 前先做確定性體檢，決定：proceed / review / reject。
大多數判斷 pymupdf 指標就夠，只有「邊界/可疑」才需 LLM 視覺驗證（needs_llm=True，
交給 pdf_contactsheet.py 渲染抽樣頁 → 一次 vision 呼叫）。

PDF type 分類（影響 MinerU OCR 品質與成本）：
  born_digital   向量文字層、嵌入字型、圖少 → MinerU 最佳，文字逐字精準
  scanned_image  每頁滿版掃描圖、無文字層 → MinerU 靠 OCR，品質看掃描解析度
  ocr_sandwich   滿版掃描圖 + 隱藏 OCR 文字層 → 可用，但文字層常有錯
  hybrid         文字 + 穿插圖（一般電子教科書常態）→ 良好

品質軸：掃描影像中位 DPI、空白頁比、bytes/頁。
完整性：頁數、空白頁比（缺章難從單檔判，留給 contact sheet / 後續比對）。

用法：
  uv run --with pymupdf python -m book_pipeline.pdf_triage <slug|pdf路徑> [...] [--json]
  uv run --with pymupdf python -m book_pipeline.pdf_triage --all [--json]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys

import fitz  # pymupdf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, 'raw_pdfs')
SLUG_MAP = os.path.join(ROOT, 'book_pipeline', 'slug_map.json')

SAMPLE_PAGES = 25  # 抽樣頁數上限（均勻取，兼顧速度與代表性）


def _resolve(arg: str) -> str | None:
    """slug 或路徑 → 實際 PDF 路徑。"""
    if os.path.isfile(arg):
        return arg
    cand = os.path.join(RAW, f'{arg}.pdf')
    if os.path.isfile(cand):
        return cand
    # 反查 slug_map：slug → raw 檔名
    try:
        m = (json.load(open(SLUG_MAP)) or {}).get('map', {})
        for fn, slug in m.items():
            if slug == arg and os.path.isfile(os.path.join(RAW, fn)):
                return os.path.join(RAW, fn)
    except Exception:
        pass
    return None


def _sample_indices(n: int) -> list[int]:
    if n <= SAMPLE_PAGES:
        return list(range(n))
    step = n / SAMPLE_PAGES
    return sorted({int(i * step) for i in range(SAMPLE_PAGES)})


def _page_metrics(page) -> dict:
    """單頁：文字字元數、影像覆蓋率、最大影像 DPI、向量繪圖數。"""
    rect = page.rect
    page_area = max(rect.width * rect.height, 1)
    text = page.get_text("text") or ''
    nchars = len(text.strip())

    img_area = 0.0
    dpis = []
    for img in page.get_images(full=True):
        xref, _, w, h = img[0], img[1], img[2], img[3]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            rects = []
        for r in rects:
            a = r.width * r.height
            img_area += a
            if r.width > 1 and w:
                dpis.append(w / (r.width / 72.0))
    cover = min(img_area / page_area, 1.0)

    try:
        ndraw = len(page.get_drawings())
    except Exception:
        ndraw = 0

    return {'nchars': nchars, 'cover': cover,
            'dpi': max(dpis) if dpis else None, 'ndraw': ndraw}


def classify(path: str) -> dict:
    try:
        doc = fitz.open(path)
    except Exception as e:
        return {'path': path, 'error': f'開檔失敗：{e}', 'verdict': 'reject',
                'needs_llm': False, 'reasons': ['無法開啟']}
    if doc.is_encrypted and not doc.authenticate(''):
        return {'path': path, 'error': '加密', 'verdict': 'reject',
                'needs_llm': False, 'reasons': ['PDF 加密']}

    n = doc.page_count
    idxs = _sample_indices(n)
    pm = []
    for i in idxs:
        try:
            pm.append(_page_metrics(doc[i]))
        except Exception:
            continue
    doc.close()
    if not pm:
        return {'path': path, 'error': '無可讀頁', 'verdict': 'reject',
                'needs_llm': False, 'reasons': ['取樣頁全失敗']}

    chars_pp = statistics.mean(m['nchars'] for m in pm)
    cover = statistics.mean(m['cover'] for m in pm)
    dpis = [m['dpi'] for m in pm if m['dpi']]
    median_dpi = round(statistics.median(dpis)) if dpis else None
    draw_pp = statistics.mean(m['ndraw'] for m in pm)
    blank = sum(1 for m in pm if m['nchars'] < 10 and m['cover'] < 0.05) / len(pm)
    bytes_pp = round(os.path.getsize(path) / max(n, 1))

    # ── 分類 ──
    reasons = []
    if chars_pp < 30 and cover > 0.85:
        ptype = 'scanned_image'
    elif chars_pp >= 100 and cover > 0.85:
        ptype = 'ocr_sandwich'
    elif chars_pp >= 600 and cover < 0.6:
        ptype = 'born_digital'
    else:
        ptype = 'hybrid'

    # ── 品質 ──
    quality = 'good'
    if ptype in ('scanned_image', 'ocr_sandwich'):
        if median_dpi is None:
            quality = 'marginal'; reasons.append('掃描檔但測不到 DPI')
        elif median_dpi < 150:
            quality = 'bad'; reasons.append(f'掃描 DPI 過低 {median_dpi}')
        elif median_dpi < 250:
            quality = 'marginal'; reasons.append(f'掃描 DPI 偏低 {median_dpi}')
    if blank > 0.25:
        quality = 'bad' if quality == 'good' else quality
        reasons.append(f'空白頁比偏高 {blank:.0%}')
    if bytes_pp < 15000 and ptype != 'born_digital':
        reasons.append(f'每頁位元組偏低 {bytes_pp}')

    # ── 判決 + 是否需 LLM 視覺驗證 ──
    if ptype == 'born_digital' and quality == 'good':
        verdict, needs_llm = 'proceed', False
        reasons.append('向量文字、圖少 → MinerU 最佳')
    elif ptype == 'hybrid' and quality == 'good' and chars_pp > 300:
        verdict, needs_llm = 'proceed', False
        reasons.append('文字充足的混合版 → 良好')
    elif quality == 'bad':
        verdict, needs_llm = 'review', True
        reasons.append('品質疑慮 → 視覺驗證確認是否可用')
    else:
        verdict, needs_llm = 'review', True
        reasons.append('邊界情況（掃描/sandwich/字少）→ 視覺驗證')

    return {
        'path': path, 'pages': n, 'sampled': len(pm),
        'chars_per_page': round(chars_pp), 'image_cover': round(cover, 2),
        'median_dpi': median_dpi, 'draw_per_page': round(draw_pp, 1),
        'blank_ratio': round(blank, 2), 'bytes_per_page': bytes_pp,
        'type': ptype, 'quality': quality,
        'verdict': verdict, 'needs_llm': needs_llm, 'reasons': reasons,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='PDF type 分類與堪用度判定')
    ap.add_argument('targets', nargs='*', help='slug 或 PDF 路徑')
    ap.add_argument('--all', action='store_true', help='全 raw_pdfs/')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    if args.all:
        paths = sorted(glob.glob(os.path.join(RAW, '*.pdf')))
    else:
        paths = []
        for t in args.targets:
            p = _resolve(t)
            if p:
                paths.append(p)
            else:
                print(f'找不到：{t}', file=sys.stderr)
        if not paths:
            ap.error('需指定 slug/路徑 或 --all')

    results = [classify(p) for p in paths]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    print(f"{'name':32} {'type':13} {'qual':8} {'c/pg':>5} {'cov':>5} {'dpi':>5} "
          f"{'blank':>5} {'verdict':>8} llm")
    for r in results:
        name = os.path.basename(r['path'])[:32]
        if r.get('error'):
            print(f"{name:32} ERROR: {r['error']}")
            continue
        print(f"{name:32} {r['type']:13} {r['quality']:8} {r['chars_per_page']:>5} "
              f"{r['image_cover']:>5} {str(r['median_dpi'] or '-'):>5} "
              f"{r['blank_ratio']:>5} {r['verdict']:>8} {'Y' if r['needs_llm'] else ''}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
