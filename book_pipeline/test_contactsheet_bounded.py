"""pdf_contactsheet 硬 timeout 測試（效率修：殺 qc 60min PDF hang）。

核心契約：渲染在可硬 kill 的子進程跑、逾 timeout_s 必 TimeoutError；損壞 PDF 必 RuntimeError
（非靜默卡死）。CLI 逾時/失敗回非零 rc=3 + CONTACTSHEET_UNRENDERABLE，qc agent 據此秒級 reject。

為何高含金量：garcia_molina_database_systems 的 qc session 曾因 fitz 在 67MB PDF C 層卡死、燒滿
60min daemon 上限仍產不出 verdict。此測釘住「render 永遠 wall-clock 有界」這條根治不變式。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import fitz
import pytest

from book_pipeline import pdf_contactsheet as cs


def _tiny_pdf(path: str, pages: int = 8) -> None:
    doc = fitz.open()
    for _ in range(pages):
        pg = doc.new_page()
        pg.insert_text((72, 72), 'hello QC')
    doc.save(path)
    doc.close()


def test_contactsheet_normal_renders():
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, 'book.pdf'); _tiny_pdf(pdf)
        out = os.path.join(d, 'sheet.png')
        r = cs.contactsheet(pdf, out, k=6)
        assert r == out and os.path.exists(out)
        # 產出是合法 PNG
        im = fitz.open(out)  # fitz 也能開 PNG
        assert im.page_count >= 1


def test_contactsheet_timeout_is_bounded():
    """timeout_s=0 → 子進程尚在啟動即被判逾時硬 kill（spawn 啟動必 >0s）→ TimeoutError。"""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, 'book.pdf'); _tiny_pdf(pdf)
        out = os.path.join(d, 'sheet.png')
        with pytest.raises(TimeoutError):
            cs.contactsheet(pdf, out, k=6, timeout_s=0)


def test_contactsheet_corrupt_raises_runtime():
    """損壞 PDF → 子進程 _render 內 fitz.open 失敗 → 回傳 err → RuntimeError（非卡死）。"""
    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, 'bad.pdf')
        with open(bad, 'wb') as f:
            f.write(b'%PDF-1.4 not really a pdf at all \x00\x01')
        out = os.path.join(d, 'sheet.png')
        with pytest.raises(RuntimeError):
            cs.contactsheet(bad, out, k=6)


def test_cli_unrenderable_exit_code():
    """CLI 對逾時回 rc=3 + CONTACTSHEET_UNRENDERABLE（qc agent 的確定性 reject 訊號）。"""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, 'book.pdf'); _tiny_pdf(pdf)
        out = os.path.join(d, 'sheet.png')
        env = dict(os.environ, BOOK_PIPELINE_CONTACTSHEET_TIMEOUT='0',
                   PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        p = subprocess.run([sys.executable, '-m', 'book_pipeline.pdf_contactsheet', pdf, '--out', out],
                           capture_output=True, text=True, env=env)
        assert p.returncode == 3, (p.returncode, p.stderr)
        assert 'CONTACTSHEET_UNRENDERABLE' in p.stderr


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
