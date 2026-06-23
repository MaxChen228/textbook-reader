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

from book_pipeline import provider_chain as pc

# provider 合法名單與 runtime override 的家在 provider_chain（單一真相、dep-light、不反向 import 本模組
# → 無循環）。此處 reexport 保 API 相容（resolve_dispatch 的 unknown-provider warning 等沿用）。
KNOWN_PROVIDERS = pc.KNOWN_PROVIDERS


@dataclass(frozen=True)
class DispatchSpec:
    """單一 stage 的派工配置。欄位 None＝繼承上層 / 不帶該旗標。"""
    chain: tuple[str, ...] | None = None   # provider failover 優先序
    codex_model: str | None = None         # codex 家族模型（gpt-5.x；池子白名單見下）
    codex_effort: str | None = None        # codex reasoning effort（low/medium/high）
    claude_model: str | None = None        # claude 模型（Claude Max；codex 家族不適用）
    timeout: int | None = None             # 派工 wall-clock 上限（秒）


# 全域底：chain＝原生 codex（OAuth 直連，最穩）→ codex-pool（ccNexus 池子備援額度）→ Claude Max 保底。
# （kimi 已於 2026-06-20 全面下架：斷線窗 fallback 產出品質不可靠，寧落 Claude Max 高品質保底。）
# 此為**碼層常態順序**（git 跨機、設計意圖）；runtime 臨時換主力 provider 走 `devctl chain set …`
# （寫 .control/provider_chain.json override，凌駕本預設、低於 env）→ 免改碼/免 commit/免重部署。
# codex 家族預設 gpt-5.4（池子白名單 gpt-5.5/5.4/5.4-mini/5.3-codex-spark 內，需 ccNexus
# fork 透傳修復在線才切得動非 5.5）。timeout 1h：正常 audit ~25min，留卡死護欄餘裕。
DEFAULT_DISPATCH = DispatchSpec(
    chain=('codex', 'codex-pool', 'claude'),
    codex_model='gpt-5.4',
    timeout=3600,
)
# per-stage 覆寫（只列偏離預設者；未列 stage 全走 DEFAULT_DISPATCH）。reasoning effort 分層：
# 重判斷（audit/catalog_audit/sol_extract）high、視覺 qc low。
# 註：crawl 已不在此——daemon 降級為純收錄引擎（買書員確定性下載 + ingest→deploy），不再派 crawl LLM；
# 填書單（四維查證）改由使用者親打 /restock 在互動 session fan-out 驅動，不經本派工層。
# math_sweep 走 ccNexus HTTP（執行路徑與 CLI 不同）但模型仍收斂於此：只取 codex_model
# （ccNexus 後端模型同 codex 家族白名單），chain/effort/timeout 由 HTTP 端各自適配，不消費。
STAGE_DISPATCH: dict[str, DispatchSpec] = {
    'audit':         DispatchSpec(codex_effort='high'),
    'catalog_audit': DispatchSpec(codex_effort='high'),
    'sol_extract':   DispatchSpec(codex_effort='high'),
    'qc':            DispatchSpec(codex_effort='low'),
}


def _merge(base: DispatchSpec, over: DispatchSpec | None) -> DispatchSpec:
    """over 的非 None 欄覆寫 base。"""
    if over is None:
        return base
    return replace(base, **{k: v for k, v in vars(over).items() if v is not None})


def _chain_override(spec: DispatchSpec) -> DispatchSpec:
    """runtime 控制檔 override chain（.control/provider_chain.json）→ 凌駕碼層 DEFAULT、低於 env。
    fail-open：壞檔/無檔 → load_override 回 None → 不動 chain（沿用 DEFAULT），派工絕不因壞檔癱瘓
    （與 pipeline_gates fail-safe hold 刻意相反，見 provider_chain 模組 docstring）。"""
    ov = pc.load_override()
    if ov:
        spec = replace(spec, chain=ov)
    return spec


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
    """四層合併 → fully-resolved spec：DEFAULT ← STAGE_DISPATCH[verb] ← 控制檔 chain override ← env。
    chain override（runtime、.control/provider_chain.json）凌駕碼層常態、低於 env 逃生口。
    log：選用警告通道（呼叫端注入，預設靜默）→ 本模組不依賴任何執行層 logger。"""
    spec = _merge(DEFAULT_DISPATCH, STAGE_DISPATCH.get(verb))
    spec = _chain_override(spec)  # runtime 控制檔 override（凌駕碼層常態）
    spec = _env_override(spec)    # env 最高（運維單次逃生口）
    unknown = [p for p in (spec.chain or ()) if p not in KNOWN_PROVIDERS]
    if unknown and log is not None:
        log(f'⚠ provider chain 含未知 provider {unknown}（合法：{KNOWN_PROVIDERS}）'
            f' → 將走 claude CLI 預設分支，恐非預期')
    return spec


def effective_chain() -> tuple[str, ...]:
    """最終生效的 provider failover 順序（DEFAULT ← runtime override ← env）。chain 不被 STAGE_DISPATCH
    覆寫（STAGE 只覆寫 effort）→ 任一 verb 解出的 chain 皆同；用一個不在 STAGE_DISPATCH 的 probe verb
    取全域 chain。供 devctl chain / snapshot 顯示「現在實際用哪條鏈」。"""
    return resolve_dispatch('_chain_probe').chain or DEFAULT_DISPATCH.chain


def math_sweep_model() -> str:
    """math sweep（ccNexus HTTP batch）的模型。執行路徑與 CLI 派工不同（HTTP vs codex exec），
    但**模型收斂於本配置層**：BOOK_PIPELINE_MATH_MODEL 專屬覆寫優先（math 特需時可單獨換），
    否則沿用 'math_sweep' stage 的 codex_model（預設 gpt-5.4，與 CLI 派工同源、同受全域
    BOOK_PIPELINE_CODEX_MODEL 覆寫 → 「model 只有一處可改」）。chain/effort/timeout 由 HTTP 端各自適配。

    **必須顯式釘 `@codex-pool` endpoint**（2026-06-23 ccNexus 輪詢故障調查定論）：ccNexus 閘道
    不做 model→endpoint 匹配，裸 model 名會落入「默認輪詢」在所有 enabled endpoint 間亂打 → 撞到
    已下架/額度爆的 kimi、或 codex-pool 輪詢路徑的 transform bug → 整批空回應、零落地（誤判成「池斷線」）。
    顯式 `@codex-pool/<model>` 前綴繞過輪詢、直釘 codex 池（實證顯式路徑穩定成功）。已帶 @ 前綴者尊重原值
    （運維可用 BOOK_PIPELINE_MATH_MODEL=@其他endpoint/model 改釘別池）。"""
    base = (os.environ.get('BOOK_PIPELINE_MATH_MODEL')
            or resolve_dispatch('math_sweep').codex_model or DEFAULT_DISPATCH.codex_model)
    return base if base.startswith('@') else f'@codex-pool/{base}'
