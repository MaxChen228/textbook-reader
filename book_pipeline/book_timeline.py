#!/usr/bin/env python3
"""book_pipeline.book_timeline — 每本書的階段轉換時間軸（觀測式，非侵入）。

第一性原理：系統每次從磁碟即時推算「當前階段」，但不留歷史。要時間軸不必去
instrument 每個 do_*（易漏、耦合），改用**觀測者**：devctl 每次 build_snapshot 對每書
算出當前階段標籤 → observe(slug, label)。label 與上次不同才 append 一筆 {stage, at}
（冪等去重），於是時間軸自然從「被觀測到的階段變化」長出來，無論轉換由 daemon 或人手
觸發都記得到。snapshot 由 tick log() 事件（~8s 節流）+ 60s 心跳驅動 → 轉換 8s 內入帳。

寫 book_pipeline/book_timeline.json（gitignore，runtime 觀測史，各機獨立、不入 git）。
tick 進程與 devsnapshot 心跳進程可能並發 → fcntl.flock 跨進程保護 RMW。
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BP = os.path.join(ROOT, 'book_pipeline')
PATH = os.path.join(BP, 'book_timeline.json')
LOCK = os.path.join(BP, 'book_timeline.lock')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _load(f) -> dict:
    try:
        f.seek(0)
        return json.load(f) or {}
    except Exception:
        return {}


def observe(slug: str, stage: str) -> None:
    """記錄 slug 當前階段；與該書最後一筆不同才 append（冪等去重）。"""
    if not slug or not stage:
        return
    with open(LOCK, 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = {}
            if os.path.exists(PATH):
                try:
                    data = json.load(open(PATH)) or {}
                except Exception:
                    data = {}
            events = data.setdefault(slug, [])
            if events and events[-1].get('stage') == stage:
                return  # 階段未變，不重複記
            events.append({'stage': stage, 'at': _now()})
            tmp = PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, PATH)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def seed(slug: str, stage: str, at: str) -> None:
    """僅當該書時間軸為空時，插入一筆歷史錨點（回填既有書已知的舊時戳，如 deployed_at）。
    已有任何記錄則不動（避免覆蓋觀測到的真實序）。"""
    if not slug or not stage or not at:
        return
    with open(LOCK, 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = {}
            if os.path.exists(PATH):
                try:
                    data = json.load(open(PATH)) or {}
                except Exception:
                    data = {}
            if data.get(slug):
                return  # 已有記錄，不回填
            data[slug] = [{'stage': stage, 'at': at, 'seeded': True}]
            tmp = PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, PATH)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def get(slug: str) -> list:
    """單書時間軸 [{stage, at}...]，按發生序。"""
    try:
        return (json.load(open(PATH)) or {}).get(slug, [])
    except Exception:
        return []


def load_all() -> dict:
    try:
        return json.load(open(PATH)) or {}
    except Exception:
        return {}
