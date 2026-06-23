#!/usr/bin/env python3
"""book_pipeline.provider_chain — LLM provider failover 順序的「合法名單 + runtime override」單一真相。

兩個關注點收斂於此：
① **KNOWN_PROVIDERS**：合法 provider 名單（派工語意的家）。llm_policy reexport 之、CLI 用它擋 typo。
② **runtime chain override**：控制檔 `.control/provider_chain.json`（gitignore runtime、per-machine），
   讓「臨時換主力 provider」免改碼/免 commit/免重部署——寫檔 + SIGUSR1 → 下次派工即生效。

碼層常態順序住 `llm_policy.DEFAULT_DISPATCH.chain`（git 跨機、設計意圖）；本檔的 override 是
**凌駕碼層常態的 runtime 拉桿**（仍低於 env `BOOK_PIPELINE_PROVIDER_CHAIN`——env 是更急迫的單次逃生口）。
無 override 檔（常態）→ load_override 回 None → llm_policy 用 DEFAULT。

**刻意零重依賴（只 json/os/fcntl/contextlib）+ 零憑證 + 不 import llm_policy**（避免與 llm_policy
的循環 import：llm_policy → provider_chain 單向）。鏡像 pipeline_gates 的原子寫/路徑風格。

**fail-open（與 pipeline_gates 的 fail-safe hold 刻意相反）**：缺檔/壞檔/結構違規/含未知 provider
→ load_override 回 None → 退回碼層 DEFAULT（一定合法的安全順序）。理由：chain 是「用哪個 provider」，
壞檔不該癱瘓派工；gates 是「該不該跑」，壞檔寧可停（停命脈安全）。兩者語意不同、fail 方向相反。

控制檔格式：`{ "chain": ["codex", "codex-pool", "claude"] }`（缺檔 = 無 override）。
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os

# 合法 provider 名單（kimi 已於 2026-06-20 全面下架，不列）。順序在此**不具意義**（純成員檢查用）；
# 真正的 failover 優先序由 DEFAULT_DISPATCH.chain（碼層常態）或本檔 override（runtime）決定。
KNOWN_PROVIDERS = ('codex', 'codex-pool', 'claude')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROL_DIR = os.path.join(ROOT, 'book_pipeline', '.control')
CHAIN_PATH = os.path.join(CONTROL_DIR, 'provider_chain.json')


def _normalize(raw) -> tuple[str, ...] | None:
    """把原始 dict 正規化成合法 chain tuple；任何結構違規/含未知 provider/空 → None（呼叫端 fail-open 退 DEFAULT）。
    嚴格驗證：一個 typo（如 'codexx'）寧可整份退回 DEFAULT（可見、安全），不靜默丟單條。"""
    if not isinstance(raw, dict):
        return None
    chain = raw.get('chain')
    if not isinstance(chain, list) or not chain:
        return None
    out = []
    for p in chain:
        if not isinstance(p, str) or p not in KNOWN_PROVIDERS:
            return None  # 未知 provider / 型別錯 → 整份作廢退 DEFAULT
        out.append(p)
    return tuple(out)


def load_override() -> tuple[str, ...] | None:
    """容錯讀 provider_chain.json，回 chain tuple；缺檔/壞檔/結構違規/含未知 provider → None（fail-open）。
    None＝無 override → 呼叫端（llm_policy）用碼層 DEFAULT_DISPATCH.chain。無 cache：每次讀磁碟
    （派工頻率低、非熱路徑；換來「改檔即下次派工生效」的即時性，免進程內快取失效問題）。"""
    try:
        with open(CHAIN_PATH) as f:
            raw = json.load(f)
    except Exception:
        return None  # 缺檔（常態）/壞 json/讀取錯 → 無 override
    return _normalize(raw)


# ── 寫路徑（CLI mutator；flock 序列化 + os.replace 原子落盤）──────────────────────────────
@contextlib.contextmanager
def _locked():
    """獨佔鎖保護 read-modify-write。鎖檔由當前 CONTROL_DIR 即時推導（測試 redirect 後須隨之轉 tempdir、
    絕不碰真 .control/）。"""
    os.makedirs(CONTROL_DIR, exist_ok=True)
    lf = open(os.path.join(CONTROL_DIR, '.provider_chain.lock'), 'w')
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


def _atomic_write(data: dict) -> None:
    os.makedirs(CONTROL_DIR, exist_ok=True)
    tmp = f'{CHAIN_PATH}.tmp{os.getpid()}'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CHAIN_PATH)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def set_chain(providers: list[str]) -> tuple[str, ...]:
    """寫 runtime override chain。驗：非空、每個 ∈ KNOWN_PROVIDERS、無重複（重複的 failover 順序無意義、是 typo）。
    非法 → ValueError（CLI 防呆）。回落盤後的 chain tuple。"""
    if not providers:
        raise ValueError('chain 不可空（至少一個 provider）')
    unknown = [p for p in providers if p not in KNOWN_PROVIDERS]
    if unknown:
        raise ValueError(f'未知 provider {unknown}（合法：{list(KNOWN_PROVIDERS)}）')
    if len(set(providers)) != len(providers):
        raise ValueError(f'chain 含重複 provider {providers}（failover 順序不該重複）')
    with _locked():
        _atomic_write({'chain': list(providers)})
    return tuple(providers)


def clear() -> None:
    """清除 runtime override（刪控制檔）→ 回碼層 DEFAULT_DISPATCH.chain。冪等（無檔亦不報錯）。"""
    with _locked():
        with contextlib.suppress(FileNotFoundError):
            os.remove(CHAIN_PATH)
