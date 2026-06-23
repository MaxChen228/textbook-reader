"""devctl.provider_health：在飛 worker 落保底偵測（status 頂層醒目告警的純核）。
跑：uv run python -m book_pipeline.test_provider_health

守 2026-06-23 坑：codex 暫態額度黏死 controller、~50min 全 claude，卻只埋在 per-worker provider 欄
沒被注意。provider_health 把「主力 codex 完全沒在用」升級成可在 status 頂層醒目告警的彙總信號。
hermetic：直接餵合成 worker list，provider_health 只讀 chain_status()（碼層常態 codex 首）。
"""
from __future__ import annotations

from book_pipeline import devctl


def test_provider_health_classifies():
    # 全落 claude（主力 codex 完全沒在用）→ fallback（最嚴重，頂層紅字）
    assert devctl.provider_health([{'provider': 'claude'}, {'provider': 'claude'}])['status'] == 'fallback'
    # 主力 codex + 次級混用 → degraded
    assert devctl.provider_health([{'provider': 'codex'}, {'provider': 'claude'}])['status'] == 'degraded'
    # 全主力 → ok
    assert devctl.provider_health([{'provider': 'codex'}, {'provider': 'codex'}])['status'] == 'ok'
    # 無 worker → idle（無信號、不誤報）
    assert devctl.provider_health([])['status'] == 'idle'


def test_provider_health_fields():
    h = devctl.provider_health([{'provider': 'claude'}, {'provider': 'codex-pool'}])
    assert h['primary'] == (devctl.chain_status().get('effective') or ['codex'])[0]
    assert h['on_fallback'] == 2 and h['live'] == {'claude': 1, 'codex-pool': 1}
    # worker 缺 provider 欄不炸
    assert devctl.provider_health([{'slug': 'x'}])['status'] == 'idle'


def test_provider_health_excludes_non_chain_workers():
    """math_sweep(ccnexus)/det worker 非 codex 鏈、有自己的後端 → 不計入落保底判定（2026-06-23 dogfood 假紅字）。"""
    # 只剩 math_sweep 走 ccnexus → idle（非 fallback）
    assert devctl.provider_health([{'provider': 'ccnexus', 'verb': 'math_sweep'}])['status'] == 'idle'
    # det worker 同理排除
    assert devctl.provider_health([{'provider': 'det'}, {'provider': 'ccnexus'}])['status'] == 'idle'
    # 真 per-book worker 落 claude + 同時有 math_sweep ccnexus → 仍判 fallback（ccnexus 不稀釋）
    h = devctl.provider_health([{'provider': 'claude'}, {'provider': 'ccnexus', 'verb': 'math_sweep'}])
    assert h['status'] == 'fallback' and h['live'] == {'claude': 1}


if __name__ == '__main__':
    test_provider_health_classifies()
    test_provider_health_fields()
    test_provider_health_excludes_non_chain_workers()
    print('\n全部通過 ✅')
