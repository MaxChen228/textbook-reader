"""leases 原語單元測試（無外部依賴）：uv run python -m book_pipeline.test_leases

涵蓋四條自癒路徑：活租約被看見、死 pid 被 reap、release 主動釋放、超 TTL runaway 被 killpg。
這是控制迴圈正確性的地基（frontier 扣租約靠它），故測得硬一點。"""

import json
import os
import subprocess
import tempfile
import time

from book_pipeline import leases


def _setup_tmpdir():
    d = tempfile.mkdtemp(prefix='lease_test_')
    leases.LEASE_DIR = d
    return d


def test_active_sees_live():
    _setup_tmpdir()
    # 用本進程 pid（必活）
    leases.acquire('audit', 'foo', os.getpid(), ttl=3600)
    act = leases.active()
    assert len(act) == 1 and act[0]['verb'] == 'audit' and act[0]['slug'] == 'foo', act
    assert leases.is_active('audit', 'foo')
    assert not leases.is_active('audit', 'bar')
    print('✓ 活租約被看見 + is_active 精確')


def test_dead_pid_reaped():
    d = _setup_tmpdir()
    p = subprocess.Popen(['sleep', '0.05'])
    leases.acquire('parse', 'dead', p.pid, ttl=3600)
    p.wait()  # 確保 pid 已死
    time.sleep(0.05)
    act = leases.active()
    assert act == [], act
    assert not os.path.exists(leases._path('parse', 'dead')), '死 pid 租約應被 unlink'
    print('✓ 死 pid → reap + unlink（transition 重入 frontier）')


def test_release():
    _setup_tmpdir()
    leases.acquire('deploy', 'baz', os.getpid())
    assert os.path.exists(leases._path('deploy', 'baz'))
    leases.release('deploy', 'baz')
    assert not os.path.exists(leases._path('deploy', 'baz'))
    leases.release('deploy', 'baz')  # 二次 release 不報錯
    print('✓ release 主動釋放 + 冪等')


def test_ttl_runaway_killed():
    _setup_tmpdir()
    # 長命子工，模擬卡死 runaway
    p = subprocess.Popen(['sleep', '30'], start_new_session=True)
    path = leases.acquire('audit', 'hung', p.pid, ttl=3600)
    # 回填 started_at 到遠古 → 立即逾時
    rec = json.load(open(path))
    rec['started_at'] = time.time() - 99999
    json.dump(rec, open(path, 'w'))
    logs = []
    act = leases.active(log=logs.append)
    assert act == [], act
    assert not os.path.exists(path), '逾時租約應被 unlink'
    time.sleep(0.3)
    assert p.poll() is not None, 'runaway 子工應被 killpg 殺掉'
    assert logs and '逾時' in logs[0], logs
    print('✓ 超 TTL runaway → killpg + unlink + 通報（吃掉舊 timeout-kill）')


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
    test_ttl_runaway_killed()
    test_crawl_plan_no_slug()
    print('\n全部通過 ✅')
