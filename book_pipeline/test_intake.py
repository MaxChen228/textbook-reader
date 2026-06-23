"""intake.set_gates_for 閘門結果回歸測試：uv run python -m book_pipeline.test_intake

守住 2026-06-23 FREEZE bug 不重現：舊「default=hold + 逐本 allow 子書 slug」會——
  ① sol_ingest/sol_extract 派工在**母書**，只 allow `<x>_sol` 子書 → 母書被 default=hold 擋死、
     子書永遠 merge 不進（R3 卡住 nilsson/sedra/khalil 3 本）；
  ② default=hold 連帶凍結全 corpus 既有 sol backlog → daemon 閒置、看板工人 0。
新設計 = default=allow + 只 hold crawl lane（本批 + 全 backlog 齊跑，只擋自動爬新書）。
全 hermetic：redirect pg 路徑到 tmp、mock controller_pid 避免真 SIGUSR1，絕不碰 live .control/gates.json。
"""
from __future__ import annotations

import os
import tempfile

from book_pipeline import intake
from book_pipeline import pipeline_gates as pg
from book_pipeline import pipeline_tick as pt


def _redirect(tmp: str):
    saved = (pg.CONTROL_DIR, pg.GATES_PATH)
    pg.CONTROL_DIR = tmp
    pg.GATES_PATH = os.path.join(tmp, 'gates.json')

    def restore():
        pg.CONTROL_DIR, pg.GATES_PATH = saved
    return restore


def test_set_gates_for_allows_parent_sol_holds_crawl():
    with tempfile.TemporaryDirectory() as tmp:
        restore = _redirect(tmp)
        saved_pid = pt.controller_pid
        pt.controller_pid = lambda: None          # 不發真 SIGUSR1
        try:
            intake.set_gates_for(['nilsson_riedel_electric_circuits_sol'])  # fetch 的是子書 slug
            g = pg.load_gates()
            assert g['default'] == 'allow', g
            # FREEGE-bug 守護：母書（既有 owned、非 fetch 的 slug）的 sol_ingest 必須放行，否則子書 merge 不進
            assert pg.gate_allows('nilsson_riedel_electric_circuits', 'sol_ingest', g) is True
            # 全 corpus 既有 sol backlog 也須放行（不再凍結）
            assert pg.gate_allows('boyd_convex_opt', 'sol_extract', g) is True
            # 只擋自動 crawl lane（保節奏：intake 直接 fetch 是唯一新書來源）
            assert pg.gate_allows(None, 'crawl', g) is False
            assert pg.gate_allows(None, 'math_sweep', g) is True
            assert pg.gate_allows(None, 'gc', g) is True
        finally:
            pt.controller_pid = saved_pid
            restore()
    print('✓ set_gates_for：default=allow + hold crawl，母書 sol/backlog 放行、crawl 擋')


if __name__ == '__main__':
    test_set_gates_for_allows_parent_sol_holds_crawl()
    print('\n全部通過 ✅')
