"""agent_history 單元測試（無外部依賴）：uv run python -m book_pipeline.test_agent_history

覆蓋自癒/防護路徑：start→event→finish 正常歸檔、孤兒 JSONL 還原（finish 沒跑成）、
pid 活+新鮮不誤還原、pid 重用嫌疑（活但逾時）仍還原、finish 冪等取代 reconcile 先還原的
同 id、corpus（slug=None）還原、reconcile 不刪可救歷程。這是 /dev 抽屜歷史正確性的地基。"""

import json
import os
import subprocess
import tempfile
import time

from book_pipeline import agent_history as hist


def _setup():
    d = tempfile.mkdtemp(prefix='hist_test_')
    hist.HIST_DIR = d
    hist.SESS_DIR = os.path.join(d, 'sessions')
    hist.INDEX_PATH = os.path.join(d, 'index.json')
    hist._sessions.clear()
    hist._last_by_verb.clear()
    return d


def _dead_pid():
    p = subprocess.Popen(['sleep', '0'])
    p.wait()
    return p.pid  # 已回收 → os.kill(pid, 0) 報 ProcessLookupError


def _write_orphan(sid, events):
    os.makedirs(hist.SESS_DIR, exist_ok=True)
    with open(hist._sess_path(sid), 'w', encoding='utf-8') as f:
        for kind, label, t in events:
            f.write(json.dumps({'t': t, 'kind': kind, 'label': label}, ensure_ascii=False) + '\n')


def test_happy_path():
    _setup()
    key = 'audit:123'
    hist.start(key, 'foo', 'audit', 123, 'kimi', 'kimi')
    hist.event(key, 'text', 'hello')
    hist.event(key, 'tool', 'shell: ls')
    hist.event(key, 'tool', 'read file')
    sid = hist.finish(key, 0)
    rows = hist._read_index()
    assert len(rows) == 1 and rows[0]['id'] == sid, rows
    assert rows[0]['ok'] is True and rows[0]['total_calls'] == 2 and rows[0]['events'] == 3, rows[0]
    assert 'reconstructed' not in rows[0], rows[0]
    assert len(hist.load_session(sid)) == 3
    print('✓ start→event→finish happy path（calls/events 計數、index 落地）')


def test_orphan_reconstructed():
    _setup()
    sid = f'20260617T085940Z-audit-wald_x-{_dead_pid()}'
    _write_orphan(sid, [
        ('text', '我先讀指引', '2026-06-17T08:59:55+00:00'),
        ('tool', 'shell: rg ...', '2026-06-17T09:00:01+00:00'),
        ('tool', 'shell: uv run ...', '2026-06-17T09:02:09+00:00'),
        ('text', '落 extract_rules', '2026-06-17T09:02:47+00:00'),
    ])
    assert hist.reconcile() == 1
    r = hist._read_index()[0]
    assert r['id'] == sid and r['reconstructed'] is True, r
    assert r['slug'] == 'wald_x' and r['verb'] == 'audit', r
    assert r['events'] == 4 and r['total_calls'] == 2, r
    assert r['rc'] is None and r['ok'] is None, r
    assert r['started'] == '2026-06-17T08:59:40+00:00', r['started']
    assert r['ended'] == '2026-06-17T09:02:47+00:00', r['ended']
    assert r['duration_s'] == 187, r['duration_s']  # 08:59:40 → 09:02:47
    assert os.path.exists(hist._sess_path(sid)), '還原不得刪 JSONL（真相源）'
    assert hist.reconcile() == 0, '冪等：已在 index 不再重複還原'
    print('✓ 孤兒 JSONL → 還原 index row（reconstructed，不刪檔，冪等）')


def test_live_pid_skipped():
    _setup()
    sid = f'20260617T100000Z-audit-livebook-{os.getpid()}'  # 本進程 = 活、JSONL 剛寫 = 新鮮
    _write_orphan(sid, [('text', 'running', '2026-06-17T10:00:05+00:00')])
    assert hist.reconcile() == 0
    assert hist._read_index() == []
    print('✓ pid 活 + JSONL 新 → 不還原（留給 live session 自行 finish）')


def test_stale_alive_reconstructed():
    _setup()
    sid = f'20260617T100000Z-qc-stalebook-{os.getpid()}'  # pid 活但 JSONL 逾時 → pid 重用嫌疑
    _write_orphan(sid, [('text', 'x', '2026-06-17T10:00:05+00:00')])
    old = time.time() - hist._ORPHAN_AGE_S - 10
    os.utime(hist._sess_path(sid), (old, old))
    assert hist.reconcile() == 1
    print('✓ pid 活但 JSONL 逾 _ORPHAN_AGE_S（pid 重用嫌疑）→ 還原')


def test_finish_replaces_reconstructed():
    _setup()
    key = 'audit:777'
    hist.start(key, 'mybook', 'audit', 777, 'kimi', 'kimi')
    sid = hist._sessions[key]['id']
    hist.event(key, 'tool', 'shell: ls')
    # 模擬 reconcile 搶先還原了同 id（慢 session 被誤判 stale）
    rows = hist._read_index()
    rows.append(hist._reconstruct_row(sid, hist._sess_path(sid)))
    hist._write_index_locked(rows)
    assert len(hist._read_index()) == 1
    # 真 finish 進來 → 取代而非追加
    hist.finish(key, 0)
    rows = hist._read_index()
    assert len(rows) == 1, ('finish 應取代同 id、不重複登記', rows)
    r = rows[0]
    assert r['id'] == sid and r.get('reconstructed') is not True, r
    assert r['ok'] is True and r['rc'] == 0, r
    print('✓ finish 冪等：取代 reconcile 先還原的同 id（不重複登記）')


def test_sidecar_provider_recovery():
    """start() 落 sidecar → controller 被 SIGKILL（繞過 finish）→ reconcile 重建仍補回 provider。
    這正是原 bug（math_sweep 被部署硬殺 → provider=null 幽靈）的回歸防護。"""
    _setup()
    key = 'math_sweep:codex'
    hist.start(key, None, 'math_sweep', _dead_pid(), 'codex', 'gpt-5.4')
    sid = hist._sessions[key]['id']
    hist.event(key, 'tool', 'shell: rg ...')
    assert os.path.exists(hist._meta_path(sid)), 'start() 應落 metadata sidecar'
    hist._sessions.clear()  # 模擬 controller 被殺：in-mem session 全失（finish 從沒跑）
    assert hist.reconcile() == 1
    r = hist._read_index()[0]
    assert r['reconstructed'] is True and r['rc'] is None, r
    assert r['provider'] == 'codex' and r['harness'] == 'codex-cli' and r['model'] == 'gpt-5.4', r
    print('✓ start sidecar → 被殺後 reconcile 重建仍補回 provider（非 null，回歸防護）')


def test_codex_pool_harness():
    """codex-pool（預設主力 provider，codex CLI 走 ccNexus 池）的 harness 須標 codex-cli 而非 claude-cli。"""
    assert hist._harness_of('codex-pool') == 'codex-cli'
    assert hist._harness_of('codex') == 'codex-cli'
    assert hist._harness_of('kimi') == 'claude-cli'
    assert hist._harness_of('claude') == 'claude-cli'
    assert hist._harness_of('ccnexus') == 'ccnexus-http'
    print('✓ codex-pool harness 標 codex-cli（對齊 _is_codex，不再誤標 claude-cli）')


def test_finish_removes_sidecar():
    """正常 finish → 權威 row 落 index、冗餘 sidecar 清掉；正常 row 仍帶 provider。"""
    _setup()
    key = 'audit:55'
    hist.start(key, 'b', 'audit', 55, 'kimi', 'kimi')
    sid = hist._sessions[key]['id']
    assert os.path.exists(hist._meta_path(sid))
    hist.finish(key, 0)
    assert not os.path.exists(hist._meta_path(sid)), 'finish 後 sidecar 應移除'
    r = hist._read_index()[0]
    assert r['provider'] == 'kimi' and 'reconstructed' not in r, r
    print('✓ finish 移除冗餘 sidecar；正常 row 仍帶 provider')


def test_reconstruct_without_sidecar_null():
    """無 sidecar（舊孤兒 / start 寫盤失敗）→ provider 退回 null（向後相容）。"""
    _setup()
    sid = f'20260101T000000Z-audit-old-{_dead_pid()}'
    _write_orphan(sid, [('tool', 'x', '2026-01-01T00:00:05+00:00')])
    assert hist.reconcile() == 1
    r = hist._read_index()[0]
    assert r['provider'] is None and r['reconstructed'] is True, r
    print('✓ 無 sidecar → provider 退回 null（相容舊孤兒）')


def test_corpus_slug_none():
    _setup()
    sid = f'20260617T083744Z-math_sweep-corpus-{_dead_pid()}'
    _write_orphan(sid, [('tool', 'x', '2026-06-17T08:37:50+00:00')])
    assert hist.reconcile() == 1
    r = hist._read_index()[0]
    assert r['slug'] is None and r['verb'] == 'math_sweep', r
    print('✓ corpus session（slug=None）還原正確')


if __name__ == '__main__':
    test_happy_path()
    test_orphan_reconstructed()
    test_live_pid_skipped()
    test_stale_alive_reconstructed()
    test_finish_replaces_reconstructed()
    test_sidecar_provider_recovery()
    test_codex_pool_harness()
    test_finish_removes_sidecar()
    test_reconstruct_without_sidecar_null()
    test_corpus_slug_none()
    print('\n全部通過 ✅')
