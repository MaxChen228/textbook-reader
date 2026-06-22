#!/usr/bin/env python3
"""book_pipeline/sol_scout.py — audit-sol 的確定性勘查工具（一次吐章錨可行性 + 題號樣本）。

存在理由（給 agent 足夠好的工具、殺 60min flail）：sol worker 過去得手刻 inline python 反覆翻
解答書 content_list 試 chapter_re。`sol_extract.extract_sol_chapters` 的章 anchor **2026-06 起
章錨層級可配（`chapter_level` 預設 null=任意 text_level）**——章標落在 lvl2/header 的書（munkres、
srednicki header 章標）現在**照樣可錨**（由 anchored chapter_re 當濾網），不再是阻塞。真阻塞只剩
「無 int 數字章號」：純標題章（simon「Limits and Continuity」）、羅馬數字章標（kardar Chapter I/II），
那些 `int(group)` 不了，才該 _pending。

本工具把「有沒有帶數字章號的章標可錨」一次確定性算清（跨所有 text_level），附判讀：有數字章標 →
照常 merge（不分層級）；全無數字章號 → _pending + proposal（羅馬→harness-gap／純標題→source-quality）。

定位同 audit_scout：給【候選 + 樣本 + 判讀】，最終決定（merge / _pending）仍由 LLM 下。

用法：uv run python -m book_pipeline.sol_scout <sol_slug>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

DATA_DIR = Path('book_pipeline/mineru_data')

# 章號＝整數章標：'Chapter N'、'N Title'、'N.'、'10.'（kittel 式裸章號帶句點）。負前瞻只擋 'N.數字'
# （= 'N.M' section 被誤當章），故 `1.`/`Chapter 1.` 放行、`1.2 Section` 排除。
# 註：用 `int(group)` 不了的羅馬/字母章號（Chapter I/ONE）刻意不收——引擎 extract_sol_chapters 也 int()
# 不了它們，那種書本就該 _pending，scout 不收=判讀正確、非 false _pending。
_CH_NUMBERED = re.compile(
    r'^(?:Chapter\s+|CHAPTER\s+|Ch\.?\s+|第)?(\d+)(?!\.\d)(?:[\s:章.\-].*)?$')
_NONCHAP = ('contents', 'preface', 'foreword', 'introduction', 'acknowledg',
            'about the', 'index', 'bibliography', 'references', 'appendix', 'solutions to')
# 題號 prefix 偵測（供寫 problem_re）：'1.2 '、'1. '、'Problem 1.2'、'P.2-1'、'2-1.'…
_PROB_PREFIX = re.compile(
    r'^(?:Problem|Exercise|Prob\.?|Ex\.?)?\s*(\d+[.\-]\d+[a-z]?|\d+)[.\)\s]')


def _load(slug: str) -> list[dict]:
    p = DATA_DIR / slug / 'unified' / 'content_list.json'
    if not p.is_file():
        sys.exit(f'❌ 找不到 {p}（先完成 ingest）')
    return json.loads(p.read_text())


def _main_chapter_count(main_slug: str) -> int | None:
    """主書 parsed/book.json 的章數（判「引擎找到的錨遠少於應有章數」用）。無 parsed → None。"""
    p = DATA_DIR / main_slug / 'parsed' / 'book.json'
    if not p.is_file():
        return None
    try:
        return len((json.loads(p.read_text()) or {}).get('chapters') or [])
    except Exception:
        return None


def _bucket(blocks: list[dict]) -> dict[str, dict]:
    """把 text/header block 依 (text_level / header) 分桶，數總數、帶數字章號數、留樣本。"""
    out: dict[str, dict] = {}
    for b in blocks:
        t = b.get('type')
        lvl = b.get('text_level')
        if t == 'text':
            key = f'text_level=={lvl}' if lvl in (1, 2) else (
                f'text_level=={lvl}' if lvl else 'text(無 level)')
        elif t == 'header':
            key = 'header'
        else:
            continue
        txt = (b.get('text') or '').strip()
        if not txt:
            continue
        d = out.setdefault(key, {'total': 0, 'numbered': 0, 'num_samples': [], 'plain_samples': []})
        d['total'] += 1
        low = txt.lower()
        if _CH_NUMBERED.match(txt) and not any(low.startswith(w) for w in _NONCHAP):
            d['numbered'] += 1
            if len(d['num_samples']) < 6:
                d['num_samples'].append(txt[:52])
        elif len(d['plain_samples']) < 4:
            d['plain_samples'].append(txt[:52])
    return out


def _prob_samples(blocks: list[dict], limit: int = 12) -> list[str]:
    out: list[str] = []
    for b in blocks:
        if b.get('type') != 'text':
            continue
        txt = (b.get('text') or '').strip()
        if _PROB_PREFIX.match(txt):
            out.append(txt[:48])
            if len(out) >= limit:
                break
    return out


def scout(sol_slug: str) -> None:
    blocks = _load(sol_slug)
    N = len(blocks)
    main_slug = re.sub(r'_sol$', '', sol_slug)
    main_ch = _main_chapter_count(main_slug)
    buckets = _bucket(blocks)

    eng_anchors = sum(v['numbered'] for v in buckets.values())   # 引擎現認任意層級的數字章標（chapter_level 預設 null）
    text_total = sum(v['total'] for v in buckets.values())       # 全文字 block 數（判「有 block 但無數字章號」）

    P = print
    P(f'# sol scout — {sol_slug}（N={N} blocks）')
    P(f'主書={main_slug}'
      + (f'，parsed 章數={main_ch}' if main_ch is not None else '，parsed 章數=（主書未 parse / 無 book.json）'))
    P('')
    P('## 章錨候選分布（引擎 `extract_sol_chapters` 預設認任意 `text_level` 的 text block；chapter_re 當濾網）')
    for key in sorted(buckets, key=lambda k: (k != 'text_level==1', k)):
        v = buckets[key]
        tag = '  ← 可錨（帶數字章號）' if v['numbered'] else ''
        P(f'  {key:18} 總={v["total"]:<4} 帶數字章號={v["numbered"]:<3}{tag}')
        if v['num_samples']:
            P(f'      數字章標樣本 = {v["num_samples"]}')
    P('')
    P('## 題號 prefix 樣本（供寫 problem_re；group(1) 對齊主書 num）')
    P(f'  {_prob_samples(blocks) or "（未偵測到明顯題號 prefix）"}')
    P('')
    P('## 判讀（→ 你二擇一：merge 或 _pending+proposal）')
    for line in _verdict(eng_anchors, text_total, main_ch):
        P(f'  {line}')


def _verdict(eng_anchors: int, text_total: int, main_ch: int | None) -> list[str]:
    """確定性判讀。章錨現認任意 text_level（chapter_level 預設 null）→ 只要有「帶數字章號」的 text
    block 即可錨；eng_anchors==0 才是真阻塞（純標題章/羅馬數字無 int 章號／源頭缺 anchor）。"""
    if eng_anchors == 0 and text_total > 0:
        return ['⚠ 有 %d 個文字 block 但 0 個帶數字章號（純標題章如「Limits and Continuity」，或羅馬數字章標）。' % text_total,
                '→ numeric chapter_re（group(1)=int 章號）無法匹配無號/羅馬標題；引擎缺題名/羅馬→章號映射。',
                '→ `_pending: true` + 開 proposal：羅馬數字→`harness-gap`（引擎缺 int 映射）；純標題無號→`source-quality`。**勿迭代 chapter_re。**']
    if eng_anchors == 0:
        return ['⚠ 無任何文字 block 可當章錨 → 源頭缺 chapter heading。',
                '→ `_pending: true` + 開 `source-quality` proposal。']
    base = ['✓ 有 %d 個數字章標、引擎可錨（任意層級皆可，含 lvl2/header）——依題號樣本寫 chapter_re/problem_re、dry-run 量配對率、語義抽樣後 merge。' % eng_anchors]
    if main_ch is not None and main_ch >= 3 and eng_anchors < max(2, main_ch // 2):
        base.append('⚠ 但可錨數(%d) 遠少於主書章數(%s)：部分章標可能無數字章號或源頭漏章。'
                    '先 dry-run，若大量章 0/N 全空 → 查該章無號/缺解答，判 merge 殘缺 vs _pending。' % (eng_anchors, main_ch))
    return base


def main() -> int:
    ap = argparse.ArgumentParser(description='sol 解答書章錨可行性勘查')
    ap.add_argument('sol_slug', help='解答書 slug（通常 <main>_sol）')
    args = ap.parse_args()
    scout(args.sol_slug)
    return 0


if __name__ == '__main__':
    sys.exit(main())
