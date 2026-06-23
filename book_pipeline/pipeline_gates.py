#!/usr/bin/env python3
"""book_pipeline.pipeline_gates — per-book × per-stage 閘門控制的決策核心 + 狀態 I/O。

刻意**零重依賴（只 json/os/fcntl/contextlib）+ 零憑證**：被 host 的 pipeline_tick/devctl 共用，
作為「哪本書、在哪個閘、放行或擋下」的單一真相。**subsume 舊 pipeline_run_state**（全域 pause = 全閘 hold）。

控制檔 `.control/gates.json`（gitignore runtime、per-machine）：
    { "default": "hold"|"allow",
      "rules": [ {"slug": "*"|<slug>, "stage": "*"|<verb>, "action": "allow"|"hold"} ] }

**決策 first-match-wins（防火牆模型，可預測、零隱藏權重）**：`gate_allows` 依序掃 rules，
第一條 match 者的 action 說了算；無 match → default。corpus lane 傳 `slug=None`（只 match `slug=="*"`）。

**fail-safe 預設暫停**（缺檔/壞檔/缺鍵/值非法/未知 stage → hold；停命脈寧可不跑，與 zlib 流量控制
fail-open 刻意相反）。fresh deploy 無檔 → 全 hold → 等價舊「部署後預設暫停，待人工啟動」。

兩目標場景（default:hold + allow 例外，最少驚奇免 catch-all）：
  只做 math sweep：{default:"hold", rules:[{"*","math_sweep","allow"}]}
  只推 N 本不 crawl：{default:"hold", rules:[{slug_i,"*","allow"} ×N]}（crawl 由 default 自動 held）
  全自動：{default:"allow", rules:[]}；全暫停：{default:"hold", rules:[]}（= 舊 pause）
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROL_DIR = os.path.join(ROOT, 'book_pipeline', '.control')
GATES_PATH = os.path.join(CONTROL_DIR, 'gates.json')

# 可 gate 的真實 dispatch verb：per-book（advance_book 內）+ corpus lane（slug=None）。
# **不含 triage**——它是 status.assess 內部的確定性分類（只 surface 成 R triage拒 或往下變 qc/ingest），
# 沒有名為 triage 的 dispatch 點；收進詞表會讓 {stage:"triage"} 規則 silent no-op（最危險的假閘）。
KNOWN_STAGES = frozenset({
    # per-book（advance_book verb；harvest 折進 ingest 同一鍵）
    'qc', 'ingest', 'sol_ingest', 'parse', 'audit', 'catalog_audit', 'sol_extract', 'deploy',
    # corpus lane（以 slug=None 呼叫）
    'crawl', 'math_sweep', 'gc',
})
_DEFAULTS = frozenset({'allow', 'hold'})
_ACTIONS = frozenset({'allow', 'hold'})

# fail-safe 哨兵：缺檔/壞檔/結構違規皆視為此（全 hold）。每次回 dict 副本，呼叫端可安全 mutate。
_FAIL_SAFE = {'default': 'hold', 'rules': []}


def _normalize(raw) -> dict | None:
    """把原始 dict 正規化成 {default, rules}；任何結構違規回 None（→ 呼叫端 fail-safe hold）。
    嚴格驗證（含 stage∈KNOWN_STAGES∪{'*'}）：一個 typo 寧可 fail-safe 全 hold（可見），
    不靜默丟單條（不可見）。CLI mutator 在寫前已驗，故 typo 只可能來自手改檔。"""
    if not isinstance(raw, dict):
        return None
    default = raw.get('default')
    if default not in _DEFAULTS:
        return None
    rules_in = raw.get('rules', [])
    if not isinstance(rules_in, list):
        return None
    rules = []
    for r in rules_in:
        if not isinstance(r, dict):
            return None
        slug = r.get('slug')
        stage = r.get('stage')
        action = r.get('action')
        if not isinstance(slug, str) or not slug:
            return None
        if not isinstance(stage, str) or not stage:
            return None
        if action not in _ACTIONS:
            return None
        if stage != '*' and stage not in KNOWN_STAGES:
            return None  # 擋 silent no-op 假閘（如 triage / typo）
        rules.append({'slug': slug, 'stage': stage, 'action': action})
    return {'default': default, 'rules': rules}


def load_gates() -> dict:
    """容錯讀 gates.json，回正規化 {default, rules}。缺檔/壞檔/結構違規 → fail-safe {default:hold, rules:[]}。
    無 cache：每次讀磁碟（呼叫端應每 cycle 讀一次、快取成快照傳給各 dispatch 點，見 gate_allows 的 gates 參數）。"""
    try:
        with open(GATES_PATH) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return dict(_FAIL_SAFE)
    except Exception:
        return dict(_FAIL_SAFE)
    norm = _normalize(raw)
    return norm if norm is not None else dict(_FAIL_SAFE)


def _matches(rule: dict, slug: str | None, stage: str) -> bool:
    rslug = rule['slug']
    rstage = rule['stage']
    slug_ok = rslug == '*' or rslug == slug  # slug=None（corpus lane）只 match '*'
    stage_ok = rstage == '*' or rstage == stage
    return slug_ok and stage_ok


def gate_allows(slug: str | None, stage: str, gates: dict | None = None) -> bool:
    """(slug, stage) 是否放行。**first-match-wins**：依序第一條 match 的 rule 之 action 決定；
    無 match → default。corpus lane 傳 slug=None（只 match slug=="*" 的 rule）。
    gates 可注入快照（省每次 load）；None 則即時 load。fail-safe（load 失敗 → default hold → 回 False）。"""
    g = gates if gates is not None else load_gates()
    for rule in g.get('rules', []):
        if _matches(rule, slug, stage):
            return rule['action'] == 'allow'
    return g.get('default') == 'allow'


def gates_active(gates: dict | None = None) -> bool:
    """stay-alive 判據 = `default=='hold'`（**刻意不含「任一 hold rule」**）。
    default hold = 擋全部、列舉放行（math-only/全停）→ 值得保活輪詢、≤LOOP_POLL 響應 gate 編輯；
    default allow + 局部 hold rule（如只擋 crawl）= 系統大體在跑、沒派工就真沒事 → 該 idle-exit
    （靠 SIGUSR1/launchd 響應後續 gate 編輯）。把「或任一 hold rule」算進來會讓「只擋 crawl」誤保活空轉。"""
    g = gates if gates is not None else load_gates()
    return g.get('default') == 'hold'


def next_gate_status(slug: str, next_verb: str | None, gates: dict | None = None) -> bool:
    """該書「下一個閘」是否被 held（供 snapshot 的 per-book `gated` 欄；複用 assess 已算的 next_verb）。
    next_verb 為 None/空（無待辦）→ 不 gated。"""
    if not next_verb:
        return False
    return not gate_allows(slug, next_verb, gates)


# ── 寫路徑（CLI mutator；flock 序列化 RMW + os.replace 原子落盤）────────────────────────
@contextlib.contextmanager
def _locked():
    """獨佔鎖保護 read-modify-write（兩個 CLI 同時 add_rule 不丟條目）。鎖檔由當前 CONTROL_DIR 推導
    （**不快取成模組常數**——測試 redirect CONTROL_DIR 後鎖檔須隨之轉去 tempdir，絕不碰真 .control/）。"""
    os.makedirs(CONTROL_DIR, exist_ok=True)
    lock_path = os.path.join(CONTROL_DIR, '.gates.lock')
    lf = open(lock_path, 'w')
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


def _atomic_write(data: dict) -> None:
    os.makedirs(CONTROL_DIR, exist_ok=True)
    tmp = f'{GATES_PATH}.tmp{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, GATES_PATH)


def set_gates(default: str, rules: list[dict]) -> dict:
    """整份覆寫（pause/resume/clear 用）。回正規化結果；default/rule 非法 → ValueError（CLI 防呆）。"""
    norm = _normalize({'default': default, 'rules': rules})
    if norm is None:
        raise ValueError(f'gates 結構非法（default={default!r} 或 rules 含非法 slug/stage/action）')
    with _locked():
        _atomic_write(norm)
    return norm


def set_default(default: str) -> dict:
    """只改 default、保留 rules。"""
    if default not in _DEFAULTS:
        raise ValueError(f'default 須為 allow|hold，得到 {default!r}')
    with _locked():
        g = load_gates()
        norm = {'default': default, 'rules': g['rules']}
        _atomic_write(norm)
    return norm


def add_rule(slug: str, stage: str, action: str, *, index: int | None = None) -> dict:
    """append（index 給定則 insert）一條 rule。stage 須 ∈ KNOWN_STAGES∪{'*'}（擋 silent no-op typo）。
    base 取當前檔（缺/壞 → fail-safe {hold,[]}，在 hold 基線上加 allow 例外是合理 fresh 行為）。"""
    if action not in _ACTIONS:
        raise ValueError(f'action 須為 allow|hold，得到 {action!r}')
    if not slug:
        raise ValueError('slug 不可空（用 * 表示全部）')
    if stage != '*' and stage not in KNOWN_STAGES:
        raise ValueError(f'未知 stage {stage!r}（防 silent no-op 假閘）；合法 stage：'
                         f'{sorted(KNOWN_STAGES)} 或 *')
    rule = {'slug': slug, 'stage': stage, 'action': action}
    with _locked():
        g = load_gates()
        rules = list(g['rules'])
        if index is None:
            rules.append(rule)
        else:
            rules.insert(index, rule)
        norm = {'default': g['default'], 'rules': rules}
        _atomic_write(norm)
    return norm


def rm_rule(index: int) -> dict:
    """依索引移除一條 rule。越界 → IndexError。"""
    with _locked():
        g = load_gates()
        rules = list(g['rules'])
        if not (0 <= index < len(rules)):
            raise IndexError(f'rule index {index} 超出範圍（0..{len(rules) - 1}）')
        rules.pop(index)
        norm = {'default': g['default'], 'rules': rules}
        _atomic_write(norm)
    return norm


def clear_rules() -> dict:
    """清空 rules、保留 default。"""
    with _locked():
        g = load_gates()
        norm = {'default': g['default'], 'rules': []}
        _atomic_write(norm)
    return norm


def pause() -> dict:
    """全暫停 = default hold + 清 rules（subsume 舊 pipeline_run_state 的 running:false）。"""
    return set_gates('hold', [])


def resume() -> dict:
    """全運行 = default allow + 清 rules（全 lane 全書齊發；要細粒度用 gates 子命令）。"""
    return set_gates('allow', [])
