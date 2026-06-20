#!/usr/bin/env python3
"""book_pipeline/sol_scout.py — audit-sol 的確定性勘查工具（一次吐章錨可行性 + 題號樣本）。

存在理由（給 agent 足夠好的工具、殺 60min flail）：sol worker 過去得手刻 inline python 反覆翻
解答書 content_list 試 chapter_re，但 `sol_extract.extract_sol_chapters` 的章 anchor **硬綁
`text_level==1 且 type=='text'`**——章標落在 lvl2/header 的書（munkres 1 lvl1 vs 338 lvl2、
srednicki 3 vs 336 header）或純標題無章號的書（simon「Limits and Continuity」），**任何
chapter_re 都救不了**（限制在 level 不在 regex）。worker 卻常迭代到撞 60min daemon 上限才 _pending。

本工具把「章標到底有沒有落在引擎認得的位置」一次確定性算清，附判讀：引擎可錨 → 照常 merge；
章標在他處/無章號 → 直接 _pending + harness-gap proposal，**不要**耗時迭代 chapter_re。

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

# 章號＝裸整數（後不接 '.數字'，排除 'N.M …' section 被誤當章）、或 'Chapter N'、'N Title'。
_CH_NUMBERED = re.compile(
    r'^(?:Chapter\s+|CHAPTER\s+|Ch\.?\s+|第)?(\d+)(?![.\d])(?:[\s:章.\-].*)?$|^(\d+)\s+[A-Z]')
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

    lvl1 = buckets.get('text_level==1', {'total': 0, 'numbered': 0, 'num_samples': [], 'plain_samples': []})
    eng_anchors = lvl1['numbered']                       # 引擎只認這個
    off = sum(v['numbered'] for k, v in buckets.items() if k != 'text_level==1')  # 引擎搆不到的

    P = print
    P(f'# sol scout — {sol_slug}（N={N} blocks）')
    P(f'主書={main_slug}'
      + (f'，parsed 章數={main_ch}' if main_ch is not None else '，parsed 章數=（主書未 parse / 無 book.json）'))
    P('')
    P('## 章錨候選分布（⚠ 引擎 `extract_sol_chapters` 只認 `text_level==1 且 type==text`）')
    for key in sorted(buckets, key=lambda k: (k != 'text_level==1', k)):
        v = buckets[key]
        tag = '  ← 引擎唯一認的位置' if key == 'text_level==1' else (
            '  ← 引擎搆不到' if v['numbered'] else '')
        P(f'  {key:18} 總={v["total"]:<4} 帶數字章號={v["numbered"]:<3}{tag}')
        if v['num_samples']:
            P(f'      數字章標樣本 = {v["num_samples"]}')
    P('')
    P('## 題號 prefix 樣本（供寫 problem_re；group(1) 對齊主書 num）')
    P(f'  {_prob_samples(blocks) or "（未偵測到明顯題號 prefix）"}')
    P('')
    P('## 判讀（→ 你二擇一：merge 或 _pending+proposal）')
    for line in _verdict(eng_anchors, off, lvl1['total'], main_ch):
        P(f'  {line}')


def _verdict(eng_anchors: int, off: int, lvl1_total: int, main_ch: int | None) -> list[str]:
    """確定性判讀。核心：引擎只認 lvl1 章錨——eng_anchors≈0 而章標在他處/無章號 = 現行引擎救不了。"""
    shortfall = (main_ch is not None and main_ch >= 3 and eng_anchors < max(2, main_ch // 2))
    if eng_anchors == 0 and off > 0:
        return ['⚠ lvl1 找到 0 個數字章標，但 lvl2/header 有 %d 個——章標落在引擎搆不到的位置。' % off,
                '→ 現行 `extract_sol_chapters` 只認 lvl1，**任何 chapter_re 都救不了（限制在 level 不在 regex）**。',
                '→ 直接 `_pending: true` + 開 `--type harness-gap` proposal（章 anchor 不在 lvl1）。**勿耗時迭代 chapter_re。**']
    if eng_anchors == 0 and lvl1_total > 0:
        return ['⚠ lvl1 有 %d 個文字 block 但 0 個帶數字章號（多為純標題章，如「Limits and Continuity」）。' % lvl1_total,
                '→ numeric chapter_re（group(1)=int 章號）無法匹配無號標題。現行引擎無題名→章號映射能力。',
                '→ 直接 `_pending: true` + 開 proposal（章標無數字章號）。**勿迭代 chapter_re。**']
    if eng_anchors == 0:
        return ['⚠ 無 lvl1 文字 block 可當章錨 → 引擎無錨可用。',
                '→ `_pending: true` + 開 proposal。']
    base = ['✓ lvl1 有 %d 個數字章標、引擎可錨——依題號樣本寫 chapter_re/problem_re、dry-run 量配對率、語義抽樣後 merge。' % eng_anchors]
    if shortfall:
        base.append('⚠ 但引擎可錨數(%d) 遠少於主書章數(%s)：多數章標可能在 lvl2/header（見上分布）。'
                    '先 dry-run，若大量章 0/N 全空 → 屬章錨不在 lvl1，轉 _pending+proposal、勿硬迭代。' % (eng_anchors, main_ch))
    return base


def main() -> int:
    ap = argparse.ArgumentParser(description='sol 解答書章錨可行性勘查')
    ap.add_argument('sol_slug', help='解答書 slug（通常 <main>_sol）')
    args = ap.parse_args()
    scout(args.sol_slug)
    return 0


if __name__ == '__main__':
    sys.exit(main())
