#!/usr/bin/env python3
"""book_pipeline.llm_policy — LLM 派工配置的單一真相源（跨 stage 共用）。

每個 LLM stage「怎麼派」收斂成一個 DispatchSpec：provider failover 優先序、各家模型、
codex reasoning effort、timeout。三層合併（resolve_dispatch）：
  DEFAULT_DISPATCH ← STAGE_DISPATCH[verb]（per-stage 覆寫）← env（運維臨時拉桿，最高）。
新增可操縱維度＝擴 DispatchSpec 一個欄 + 消費端讀它，無第三處散落。

抽成獨立模組（原住 pipeline_tick）→ pipeline_tick（CLI-headless 派工）與 math_sweep
（ccNexus HTTP batch）共用同一配置層：model/provider/effort/timeout 永遠只有一處可改。
本模組純配置、零副作用、不 import 任何 pipeline 執行層（避免循環依賴）；唯一外部互動是
resolve_dispatch 的選用 log callback（呼叫端注入，預設靜默）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Callable

KNOWN_PROVIDERS = ('codex-pool', 'codex', 'claude')


@dataclass(frozen=True)
class DispatchSpec:
    """單一 stage 的派工配置。欄位 None＝繼承上層 / 不帶該旗標。"""
    chain: tuple[str, ...] | None = None   # provider failover 優先序
    codex_model: str | None = None         # codex 家族模型（gpt-5.x；池子白名單見下）
    codex_effort: str | None = None        # codex reasoning effort（low/medium/high）
    claude_model: str | None = None        # claude 模型（Claude Max；codex 家族不適用）
    timeout: int | None = None             # 派工 wall-clock 上限（秒）


# 全域底：chain＝優先榨池子（maxn970228 獨立額度）→ 原生 codex → Claude Max 保底。
# （kimi 已於 2026-06-20 全面下架：斷線窗 fallback 產出品質不可靠，寧落 Claude Max 高品質保底。）
# codex 家族預設 gpt-5.4（池子白名單 gpt-5.5/5.4/5.4-mini/5.3-codex-spark 內，需 ccNexus
# fork 透傳修復在線才切得動非 5.5）。timeout 1h：正常 audit ~25min，留卡死護欄餘裕。
DEFAULT_DISPATCH = DispatchSpec(
    chain=('codex-pool', 'codex', 'claude'),
    codex_model='gpt-5.4',
    timeout=3600,
)
# per-stage 覆寫（只列偏離預設者；未列 stage 全走 DEFAULT_DISPATCH）。reasoning effort 分層：
# 重判斷（audit/catalog_audit/sol_extract）high、視覺 qc low。crawl 查證＝「合格存在四維查證」
# （/restock 庫存模式 + booklist-manager skill：每本書 fan-out 多 haiku subagent 交叉查夠格∧連結∧版本∧
# 解答對齊）→ 鎖 claude-only——subagent fan-out 是 claude code 獨有，codex headless 無法 spawn claude
# haiku（故不走 codex 家族、不適用 effort）。env BOOK_PIPELINE_PROVIDER_CHAIN 仍可臨時凌駕（但設非
# claude → skill 的 fan-out 失效）。
# math_sweep 走 ccNexus HTTP（執行路徑與 CLI 不同）但模型仍收斂於此：只取 codex_model
# （ccNexus 後端模型同 codex 家族白名單），chain/effort/timeout 由 HTTP 端各自適配，不消費。
STAGE_DISPATCH: dict[str, DispatchSpec] = {
    'audit':         DispatchSpec(codex_effort='high'),
    'catalog_audit': DispatchSpec(codex_effort='high'),
    'sol_extract':   DispatchSpec(codex_effort='high'),
    'crawl':         DispatchSpec(chain=('claude',)),     # 多源查證需 subagent fan-out（見上）
    'qc':            DispatchSpec(codex_effort='low'),
}


def _merge(base: DispatchSpec, over: DispatchSpec | None) -> DispatchSpec:
    """over 的非 None 欄覆寫 base。"""
    if over is None:
        return base
    return replace(base, **{k: v for k, v in vars(over).items() if v is not None})


def _env_override(spec: DispatchSpec) -> DispatchSpec:
    """env 運維臨時拉桿凌駕（最高優先）。未設的 env 不動該欄。"""
    ch = os.environ.get('BOOK_PIPELINE_PROVIDER_CHAIN', '').strip()
    if ch:
        parsed = tuple(p.strip().lower() for p in ch.split(',') if p.strip())
        if parsed:
            spec = replace(spec, chain=parsed)
    for env_key, field in (('BOOK_PIPELINE_CODEX_MODEL', 'codex_model'),
                           ('BOOK_PIPELINE_CODEX_EFFORT', 'codex_effort'),
                           ('BOOK_PIPELINE_CLAUDE_MODEL', 'claude_model')):
        v = os.environ.get(env_key)
        if v:
            spec = replace(spec, **{field: v})
    to = os.environ.get('BOOK_PIPELINE_LLM_TIMEOUT')
    if to:
        spec = replace(spec, timeout=int(to))
    return spec


def resolve_dispatch(verb: str, log: Callable[[str], None] | None = None) -> DispatchSpec:
    """三層合併 → fully-resolved spec：DEFAULT ← STAGE_DISPATCH[verb] ← env。
    log：選用警告通道（呼叫端注入，預設靜默）→ 本模組不依賴任何執行層 logger。"""
    spec = _env_override(_merge(DEFAULT_DISPATCH, STAGE_DISPATCH.get(verb)))
    unknown = [p for p in (spec.chain or ()) if p not in KNOWN_PROVIDERS]
    if unknown and log is not None:
        log(f'⚠ provider chain 含未知 provider {unknown}（合法：{KNOWN_PROVIDERS}）'
            f' → 將走 claude CLI 預設分支，恐非預期')
    return spec


def math_sweep_model() -> str:
    """math sweep（ccNexus HTTP batch）的模型。執行路徑與 CLI 派工不同（HTTP vs codex exec），
    但**模型收斂於本配置層**：BOOK_PIPELINE_MATH_MODEL 專屬覆寫優先（math 特需時可單獨換），
    否則沿用 'math_sweep' stage 的 codex_model（預設 gpt-5.4，與 CLI 派工同源、同受全域
    BOOK_PIPELINE_CODEX_MODEL 覆寫 → 「model 只有一處可改」）。ccNexus 後端模型同 codex 家族白名單。
    chain/effort/timeout 由 HTTP 端各自適配，不在此消費。"""
    m = os.environ.get('BOOK_PIPELINE_MATH_MODEL')
    if m:
        return m
    return resolve_dispatch('math_sweep').codex_model or DEFAULT_DISPATCH.codex_model
