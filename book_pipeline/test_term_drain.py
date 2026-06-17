"""pipeline_tick 終止安全：uv run python -m book_pipeline.test_term_drain

部署強制重啟（kickstart -k → SIGTERM）或 Ctrl-C 時，controller 須主動快殺在飛 LLM 子工（整個
process group，不留孤兒孫進程），讓其 _run_one finally 秒級跑完 → 不留「未收尾」幽靈、退出遠早於
launchd ExitTimeOut 升級 SIGKILL。覆蓋：登記表增刪、整組 SIGKILL 無孤兒、真實送 SIGTERM 觸發 handler。"""

import os
import signal
import subprocess
import threading
import time

from book_pipeline import pipeline_tick as pt


def _spawn_group():
    """自成 process group 的長命子工樹（父 sh + 背景孫 sleep），模擬 codex CLI（start_new_session）。"""
    p = subprocess.Popen(['sh', '-c', 'sleep 600 & sleep 600'], start_new_session=True)
    time.sleep(0.1)  # 讓孫進程起來
    return p


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def test_register_unregister():
    pt._inflight_children.clear()
    pt._register_child(111, 111)
    pt._register_child(222, 222)
    assert set(pt._inflight_children.values()) == {111, 222}
    pt._unregister_child(111)
    assert set(pt._inflight_children.values()) == {222}
    pt._unregister_child(222)
    assert pt._kill_inflight_children() == 0  # 空表 → 殺 0、不拋
    print('✓ _inflight_children 登記表增刪正確、空表安全')


def test_kill_inflight_group_no_orphan():
    pt._inflight_children.clear()
    p = _spawn_group()
    pgid = os.getpgid(p.pid)
    pt._register_child(p.pid, pgid)
    assert pt._kill_inflight_children() == 1
    assert p.wait(timeout=5) < 0, 'p 應被 signal 殺（returncode 負）'
    time.sleep(0.2)
    assert not _group_alive(pgid), '整個 process group 應殺光（無孤兒孫進程繼續空轉）'
    pt._unregister_child(p.pid)
    print('✓ 快殺整組 SIGKILL：父+孫全死、無孤兒')


def test_term_handler_real_signal():
    """真實對本進程送 SIGTERM → handler 設旗標 + 喚醒 + 快殺在飛子工（不退出，只跑 handler）。"""
    prev_term = signal.getsignal(signal.SIGTERM)
    prev_int = signal.getsignal(signal.SIGINT)
    try:
        pt._inflight_children.clear()
        wake, term = threading.Event(), threading.Event()
        p = _spawn_group()
        pgid = os.getpgid(p.pid)
        pt._register_child(p.pid, pgid)
        assert pt._install_term_handlers(wake, term) is True
        os.kill(os.getpid(), signal.SIGTERM)  # 觸發本進程 handler
        time.sleep(0.3)
        assert term.is_set(), 'handler 應設 terminating 旗標 → loop 跳出排空'
        assert wake.is_set(), 'handler 應喚醒 loop 立即重觀測'
        assert p.wait(timeout=5) < 0, '在飛子工應被 handler 快殺'
        assert not _group_alive(pgid), '整組死透'
        pt._unregister_child(p.pid)
        print('✓ 真實 SIGTERM → handler 快殺子工 + 設旗標 + 喚醒（不退出）')
    finally:
        signal.signal(signal.SIGTERM, prev_term)  # 還原，免污染後續測試/CI
        signal.signal(signal.SIGINT, prev_int)


if __name__ == '__main__':
    test_register_unregister()
    test_kill_inflight_group_no_orphan()
    test_term_handler_real_signal()
    print('\n全部通過 ✅')
