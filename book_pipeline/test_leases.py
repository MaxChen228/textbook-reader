"""leases 原語單元測試（無外部依賴）：uv run python -m book_pipeline.test_leases

涵蓋自癒/防護路徑：活租約被看見、死 pid reap、release、兩段式 runaway 殺（TERM→寬限→KILL）、
pid 重用防護（identity 不符 → unlink 但不誤殺）、crawl_plan 無 slug。
這是控制迴圈正確性的地基（frontier 扣租約靠它），故測得硬一點。"""

import json
import os
import subprocess
import sys
import tempfile
import time

from book_pipeline import leases

# 會忽略 SIGTERM 的子工 —— 用來驗證「TERM 寬限後才補 SIGKILL」這條 pass（sleep 收 TERM 即死，測不到）。
_IGN_TERM = ('import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
             'print("up",flush=True);time.sleep(60)')


def _setup_tmpdir():
    d = tempfile.mkdtemp(prefix='lease_test_')
    leases.LEASE_DIR = d
    return d


def _spawn_ign_term():
    p = subprocess.Popen([sys.executable, '-c', _IGN_TERM], start_new_session=True,
                         stdout=subprocess.PIPE, text=True)
    p.stdout.readline()  # 等子工裝好 SIG_IGN（印 "up"）才回，避免 TERM 早於 handler
    return p


def _backdate(path, **kw):
    rec = json.load(open(path))
    rec.update(kw)
    json.dump(rec, open(path, 'w'))
    return rec


def test_active_sees_live():
    _setup_tmpdir()
    leases.acquire('audit', 'foo', os.getpid(), ttl=3600)
    act = leases.active()
    assert len(act) == 1 and act[0]['verb'] == 'audit' and act[0]['slug'] == 'foo', act
    assert leases.is_active('audit', 'foo')
    assert not leases.is_active('audit', 'bar')
    print('✓ 活租約被看見 + is_active 精確')


def test_dead_pid_reaped():
    _setup_tmpdir()
    p = subprocess.Popen(['sleep', '0.05'])
    leases.acquire('parse', 'dead', p.pid, ttl=3600)
    p.wait()
    time.sleep(0.05)
    assert leases.active() == []
    assert not os.path.exists(leases._path('parse', 'dead')), '死 pid 租約應被 unlink'
    print('✓ 死 pid → reap + unlink（transition 重入 frontier）')


def test_release():
    _setup_tmpdir()
    leases.acquire('deploy', 'baz', os.getpid())
    assert os.path.exists(leases._path('deploy', 'baz'))
    leases.release('deploy', 'baz')
    assert not os.path.exists(leases._path('deploy', 'baz'))
    leases.release('deploy', 'baz')  # 冪等
    print('✓ release 主動釋放 + 冪等')


def test_runaway_two_phase_kill():
    _setup_tmpdir()
    p = _spawn_ign_term()
    path = leases.acquire('audit', 'hung', p.pid, ttl=3600)
    _backdate(path, started_at=time.time() - 99999)  # 立即逾時

    # Pass 1：SIGTERM + 留租約寬限。子工忽略 TERM → 仍活；租約仍在、termed_at 已記。
    logs = []
    act = leases.active(log=logs.append)
    assert act == [], act
    time.sleep(0.2)
    assert p.poll() is None, 'pass1 只該 SIGTERM；忽略 TERM 的子工應仍活'
    assert os.path.exists(path), 'pass1 應保留租約（殺人進行中、frontier 仍扣）'
    rec = json.load(open(path))
    assert rec.get('termed_at') is not None and '逾時' in logs[0], (rec, logs)
    print('✓ runaway pass1：SIGTERM + 留租約寬限')

    # Pass 2：寬限到 → SIGKILL + unlink。
    _backdate(path, termed_at=time.time() - leases.KILL_GRACE - 1)
    logs2 = []
    leases.active(log=logs2.append)
    time.sleep(0.3)
    assert p.poll() is not None, 'pass2 應 SIGKILL 殺掉子工'
    assert not os.path.exists(path), 'pass2 應 unlink 租約'
    assert logs2 and 'SIGKILL' in logs2[0], logs2
    print('✓ runaway pass2：寬限到 → SIGKILL + unlink（吃掉舊 timeout-kill）')


def test_pid_reuse_not_killed():
    """pid 重用防護核心場景：identity token 不符 → 視為死、unlink，但**絕不 killpg**（那是別人的進程）。"""
    _setup_tmpdir()
    p = _spawn_ign_term()  # 健在的「無關進程」，模擬 pid 被回收給它
    path = leases.acquire('catalog_audit', 'recycled', p.pid, ttl=3600)
    # 竄改 identity 模擬 pid 重用 + 回填逾時（若無防護，逾時分支會殺 p）
    _backdate(path, identity='BOGUS Sat Jan 1 00:00:00 2000 ghost', started_at=time.time() - 99999)
    leases.active()
    time.sleep(0.2)
    assert p.poll() is None, 'identity 不符 → 絕不該殺這個無關進程'
    assert not os.path.exists(path), 'identity 不符的租約應被 unlink'
    p.kill()  # 收尾
    print('✓ pid 重用防護：identity 不符 → unlink 但不誤殺')


def test_crawl_plan_no_slug():
    _setup_tmpdir()
    leases.acquire('crawl_plan', None, os.getpid())
    assert leases.is_active('crawl_plan', None)
    assert os.path.basename(leases._path('crawl_plan', None)) == 'crawl_plan.json'
    print('✓ slug=None（crawl_plan）key 正確')


if __name__ == '__main__':
    test_active_sees_live()
    test_dead_pid_reaped()
    test_release()
    test_runaway_two_phase_kill()
    test_pid_reuse_not_killed()
    test_crawl_plan_no_slug()
    print('\n全部通過 ✅')
