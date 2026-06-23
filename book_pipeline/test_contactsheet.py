"""pdf_contactsheet 漸進式 + 部分容錯測試（#30 false-kill 硬化）。

核心不變量：好書中夾 1 頁病態圖不該被誤殺——逾時/異常時用「已渲好的頁」拼部分 sheet 照常回傳，
只有「連一頁都出不來」才 raise（→ qc agent 才判 reject）。全 hermetic：用 fitz 合成 PDF，不碰 raw_pdfs。
"""
from __future__ import annotations

import os
import tempfile

import fitz
from PIL import Image

from book_pipeline import pdf_contactsheet as cs


def _make_pdf(path: str, pages: int = 4) -> None:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=400, height=600)
        page.insert_text((50, 80), f'PAGE {i + 1}', fontsize=40)
    doc.save(path)
    doc.close()


# ── _assemble 純函式：部分頁也出有效圖、按頁碼排序 ──────────────────────────────
def test_assemble_partial_pages_valid_png():
    """殘存 2 頁（非滿 6）仍拼出可開啟的 PNG（部分 sheet 不該壞）。"""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, 's.png')
        cells = [(2, Image.new('RGB', (cs.CELL_W, 1000), 'white')),
                 (4, Image.new('RGB', (cs.CELL_W, 900), 'gray'))]
        r = cs._assemble(cells, out)
        im = Image.open(r); im.load()
        assert im.width == cs.COLS * cs.CELL_W + (cs.COLS + 1) * cs.PAD


def test_assemble_handles_out_of_order_pages():
    """亂序抵達的頁（漸進式收集不保證順序）拼圖不崩潰、照常出圖（內部按頁碼排序）。"""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, 's.png')
        cells = [(5, Image.new('RGB', (cs.CELL_W, 800), 'white')),
                 (1, Image.new('RGB', (cs.CELL_W, 800), 'white'))]
        r = cs._assemble(cells, out)
        assert os.path.exists(r)


# ── contactsheet 端到端：全頁、零頁逾時、壞檔 ─────────────────────────────────
def test_full_render_synthetic_pdf():
    """合成 4 頁 PDF → 完整渲染出有效 sheet。"""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, 'b.pdf'); _make_pdf(pdf, 4)
        out = os.path.join(d, 's.png')
        r = cs.contactsheet(pdf, out, k=4, timeout_s=60)
        im = Image.open(r); im.load()
        assert im.width > 0 and im.height > 0


def test_zero_page_timeout_raises():
    """timeout_s=0 → 一頁都來不及收 → TimeoutError（→ qc reject 的唯一正當路徑）。"""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, 'b.pdf'); _make_pdf(pdf, 4)
        try:
            cs.contactsheet(pdf, os.path.join(d, 's.png'), k=4, timeout_s=0)
            assert False, '應 raise TimeoutError'
        except TimeoutError as e:
            assert '連一頁都出不來' in str(e)


def test_corrupt_pdf_raises_runtime():
    """非 PDF 檔 → 子進程 fitz.open 拋 → 零頁+err → RuntimeError（不靜默回空）。"""
    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, 'bad.pdf')
        with open(bad, 'w') as f:
            f.write('not a pdf')
        try:
            cs.contactsheet(bad, os.path.join(d, 's.png'), k=4, timeout_s=30)
            assert False, '應 raise'
        except (RuntimeError, TimeoutError):
            pass  # 兩者皆可（視 fitz 何時失敗）；關鍵是不靜默成功


if __name__ == '__main__':
    import sys
    import pytest
    sys.exit(pytest.main([__file__, '-q']))
