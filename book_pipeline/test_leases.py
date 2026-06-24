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


# ── 孤兒 agent 進程回收（測得硬一點：誤殺活 worker/互動 session 是災難）──────────────
_ROOT = leases._ROOT


def _fake_ps(lines):
    """回 subprocess.run 替身：只攔 ps，餵受控輸出；其餘照舊。"""
    real = leases.subprocess.run

    def fake(cmd, *a, **k):
        if cmd[:1] == ['ps']:
            class R:
                stdout = '\n'.join(lines) + '\n'
            return R()
        return real(cmd, *a, **k)
    return fake


def test_parse_etime():
    assert leases._parse_etime('08-02:05:15') == 8 * 86400 + 2 * 3600 + 5 * 60 + 15
    assert leases._parse_etime('08:58:45') == 8 * 3600 + 58 * 60 + 45
    assert leases._parse_etime('01:04') == 64
    assert leases._parse_etime('  391') == 391
    assert leases._parse_etime('garbage') == 0  # 容錯回 0
    print('✓ macOS etime 解析（[[dd-]hh:]mm:ss + 容錯）')


def test_orphan_discriminator():
    """鑑別子核心：命中真孤兒、三重排除互動 session / 他專案 codex / 活 controller 子工。"""
    lines = [
        # 真孤兒 codex top（PPID=1 + 'codex' + ROOT 在 argv）→ 命中
        f'10001     1 08-02:05:15 node /opt/homebrew/bin/codex exec --json -C {_ROOT} --sandbox danger-full-access',
        # 真孤兒 claude headless（PPID=1 + 'claude' + '--add-dir' + ROOT）→ 命中
        f'10002     1       05:00 claude -p 任務 --add-dir {_ROOT} --output-format stream-json',
        # 互動 Claude Code session（PPID=1 但無 codex、無 --add-dir、無 ROOT）→ 排除（防自殺）
        '10003     1       01:00 claude --dangerously-skip-permissions',
        # 他專案 codex（PPID=1 + codex 但 ROOT 不符；cwd 兜底亦不符）→ 排除
        '10004     1       02:00 node /opt/homebrew/bin/codex exec --json -C /other/proj --sandbox danger',
        # 活 controller 子工（codex + ROOT 但 PPID!=1）→ 排除（活 worker 絕不誤殺）
        f'10005  9051       03:00 node /opt/homebrew/bin/codex exec -C {_ROOT}',
    ]
    orig_run, orig_cwd, orig_pgid = leases.subprocess.run, leases._cwd_is, os.getpgid
    leases.subprocess.run = _fake_ps(lines)
    leases._cwd_is = lambda pid, root: False  # 他專案 cwd 不符 → 兜底也排除
    os.getpgid = lambda pid: pid
    try:
        leases._ORPHAN_CACHE['at'] = 0.0
        orphs = leases._agent_orphans()
        assert sorted(o['pid'] for o in orphs) == [10001, 10002], orphs
        ages = {o['pid']: o['age_sec'] for o in orphs}
        assert ages[10001] == 8 * 86400 + 2 * 3600 + 5 * 60 + 15 and ages[10002] == 300, ages
    finally:
        leases.subprocess.run, leases._cwd_is, os.getpgid = orig_run, orig_cwd, orig_pgid
    print('✓ 孤兒鑑別子：命中真孤兒、排除互動 session/他專案/活 worker')


def test_orphan_cwd_fallback():
    """codex node 孫程序 argv 未必帶 ROOT → 退查 cwd==ROOT 命中（防 top 已退殘留漏網）。"""
    lines = ['20001     1   10:00 node /opt/homebrew/lib/node_modules/@openai/codex/worker --max-old-space-size=6144']
    orig_run, orig_cwd, orig_pgid = leases.subprocess.run, leases._cwd_is, os.getpgid
    leases.subprocess.run = _fake_ps(lines)
    leases._cwd_is = lambda pid, root: pid == 20001  # 此孫程序 cwd==ROOT
    os.getpgid = lambda pid: pid
    try:
        leases._ORPHAN_CACHE['at'] = 0.0
        assert [o['pid'] for o in leases._agent_orphans()] == [20001]
    finally:
        leases.subprocess.run, leases._cwd_is, os.getpgid = orig_run, orig_cwd, orig_pgid
    print('✓ node 孫程序 argv 無 ROOT → cwd 兜底命中')


def test_reap_only_kills_matched():
    """reap 安全鐵律：只殺命中孤兒，絕不碰互動 session。"""
    lines = [
        f'30001     1   10:00 node /opt/homebrew/bin/codex exec -C {_ROOT}',  # 孤兒 → 殺
        '30002     1   10:00 claude --dangerously-skip-permissions',          # 互動 → 不殺
    ]
    killed_pg, killed_pid = [], []
    orig = (leases.subprocess.run, leases._cwd_is, os.killpg, os.kill, os.getpgid)
    leases.subprocess.run = _fake_ps(lines)
    leases._cwd_is = lambda pid, root: False
    os.killpg = lambda pg, sig: killed_pg.append(pg)
    os.kill = lambda pid, sig: killed_pid.append(pid)
    os.getpgid = lambda pid: pid
    try:
        leases._ORPHAN_CACHE['at'] = 0.0
        res = leases.reap_orphans()
        assert res['reaped'] == 1 and res['groups'] == 1, res
        assert killed_pid == [30001] and killed_pg == [30001], (killed_pid, killed_pg)
    finally:
        leases.subprocess.run, leases._cwd_is, os.killpg, os.kill, os.getpgid = orig
    print('✓ reap 只殺命中孤兒、絕不碰互動 session')


def test_count_orphans_cache():
    """count_orphans 10s 快取：1s snapshot 熱路徑不每次 ps。"""
    calls = [0]
    real = leases.subprocess.run

    def fake(cmd, *a, **k):
        if cmd[:1] == ['ps']:
            calls[0] += 1
            class R:
                stdout = ''  # 無孤兒
            return R()
        return real(cmd, *a, **k)
    orig = leases.subprocess.run
    leases.subprocess.run = fake
    try:
        leases._ORPHAN_CACHE['at'] = 0.0
        v1 = leases.count_orphans()
        v2 = leases.count_orphans()  # 走快取、不再 ps
        assert v1 == v2 == {'count': 0, 'oldest_sec': 0}
        assert calls[0] == 1, f'第二次應走快取，ps 只該呼叫一次，實 {calls[0]}'
    finally:
        leases.subprocess.run = orig
        leases._ORPHAN_CACHE['at'] = 0.0
    print('✓ count_orphans 10s 快取（熱路徑不每次 ps）')


if __name__ == '__main__':
    test_active_sees_live()
    test_dead_pid_reaped()
    test_release()
    test_runaway_two_phase_kill()
    test_pid_reuse_not_killed()
    test_crawl_plan_no_slug()
    test_parse_etime()
    test_orphan_discriminator()
    test_orphan_cwd_fallback()
    test_reap_only_kills_matched()
    test_count_orphans_cache()
    print('\n全部通過 ✅')
