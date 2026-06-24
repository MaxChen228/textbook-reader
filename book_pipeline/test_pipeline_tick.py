"""pipeline_tick._run_build_all 序列化測試。

回歸守衛：LOOP_CONCURRENCY=32 讓多本同時 do_deploy，每本 build_all → convert_images
cwebp ProcessPool 開滿核 → N×cpu_count 進程爆炸（實測 6 本並發=48 進程、load 57、每本反而
5× 慢、餓死觀測心跳）。_BUILD_SEM（預設 1）把並發 build 序列化。本測試以 mock subprocess.run
記錄並發峰值，釘死「同時最多 1 本 build」不變式。
"""
from __future__ import annotations

import threading
import time

from book_pipeline import pipeline_tick as pt


def test_build_parallel_default_serialized():
    # 預設 BUILD_PARALLEL=1 → 序列化（env 可覆寫，但常態必須序列化避免進程爆炸）
    assert pt.BUILD_PARALLEL == 1
    assert isinstance(pt._BUILD_SEM, type(threading.Semaphore()))


def test_run_build_all_serialized_peak_concurrency_one():
    """5 執行緒並發呼 _run_build_all，mock subprocess.run 記錄重疊峰值 → 必為 1。"""
    peak = {'cur': 0, 'max': 0}
    lk = threading.Lock()

    class _R:
        returncode = 0

    def fake_run(cmd, cwd=None, **kw):
        with lk:
            peak['cur'] += 1
            peak['max'] = max(peak['max'], peak['cur'])
        time.sleep(0.03)  # 製造重疊窗：若無 _BUILD_SEM，峰值會 >1
        with lk:
            peak['cur'] -= 1
        return _R()

    orig = pt.subprocess.run
    pt.subprocess.run = fake_run
    try:
        ts = [threading.Thread(target=pt._run_build_all, args=(f'b{i}',)) for i in range(5)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert peak['max'] == 1, f'build 應序列化（並發峰值=1），實得 {peak["max"]}'
    finally:
        pt.subprocess.run = orig


def test_audit_parallel_default_and_sem():
    # audit/catalog_audit 並發閘預設（env 可調，但常態須有上限避免子進程爆發）
    assert pt.AUDIT_PARALLEL == 8
    assert isinstance(pt._AUDIT_SEM, type(threading.Semaphore()))
    assert pt._CPU_HEAVY_LLM_STAGES == ('audit', 'catalog_audit')


def test_llm_concurrency_guard_caps_only_cpu_heavy():
    """audit/catalog_audit 走 _AUDIT_SEM（CPU-heavy 子進程）；qc/sol_extract 不套（network-bound 輕量）。"""
    import contextlib
    assert pt._llm_concurrency_guard('audit') is pt._AUDIT_SEM
    assert pt._llm_concurrency_guard('catalog_audit') is pt._AUDIT_SEM
    assert isinstance(pt._llm_concurrency_guard('qc'), contextlib.nullcontext)
    assert isinstance(pt._llm_concurrency_guard('sol_extract'), contextlib.nullcontext)


def test_run_build_all_returns_rc():
    class _R:
        returncode = 7

    orig = pt.subprocess.run
    pt.subprocess.run = lambda cmd, cwd=None, **kw: _R()
    try:
        assert pt._run_build_all('whatever') == 7
    finally:
        pt.subprocess.run = orig


if __name__ == '__main__':
    test_build_parallel_default_serialized()
    test_run_build_all_serialized_peak_concurrency_one()
    test_audit_parallel_default_and_sem()
    test_llm_concurrency_guard_caps_only_cpu_heavy()
    test_run_build_all_returns_rc()
    print('全部通過 ✅')
