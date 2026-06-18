#!/usr/bin/env python3
"""book_pipeline/audit_scout.py — audit-book 的確定性勘查工具（Step 1–7 機械部分一次吐候選）。

存在理由（給 agent 足夠好的工具）：audit worker 過去得手刻數十個 inline python heredoc 翻
content_list.json（young_freedman 一本 sed×68/cat×30），且為了懂偵測邏輯去讀 parser.py/
build_catalogs.py 源碼——這正是它後來有能力、有動機擅改引擎的根源。本工具把 §3 的機械步驟
（type 統計→filter_types、heading_text_level 偵測、heading 清單、章節/附錄/index 錨點候選、
regex 推斷樣本）確定性化成**一次呼叫的結構化報告**。worker 讀報告即可下判斷、寫 yaml，
**不必開 content_list 生肉、不必讀任何引擎源碼**。

定位：scout=給【候選 + 樣本】，不替 worker 做最終決定（判斷留 LLM；見 enumerate-denominator
鐵則：可列舉的機械步驟確定性化、判斷子步仍交 agent）。報告每節都標「→ 你要判斷什麼」。

用法：uv run --with pyyaml python -m book_pipeline.audit_scout <slug> [--max-headings 400]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

DATA_DIR = Path('book_pipeline/mineru_data')

# §3 Step 2：固定進 filter_types 的雜訊 type（ref_text 須 count>5 才進）。
ALWAYS_FILTER = ('header', 'page_number', 'footer', 'aside_text', 'page_footnote')
_SECISH = re.compile(r'^\d+[.\dA-Z]')           # 'N.M …' / 'NA …'(Axler) 形 section heading
# 章號=裸整數，後面不接 '.數字'（排除 'N.M Section' 被誤當章 N）。負前瞻 (?![.\d]) 是關鍵。
_CH_NUMBERED = re.compile(r'^(?:Chapter\s+)?(\d+)(?![.\d])(?:[\s:].*)?$|^(\d+)\s+[A-Z]')
_NONCHAP = ('contents', 'preface', 'foreword', 'introduction', 'acknowledg',
            'about the', 'index', 'bibliography', 'references', 'appendix')


def _load(slug: str) -> list[dict]:
    p = DATA_DIR / slug / 'unified' / 'content_list.json'
    if not p.is_file():
        sys.exit(f'❌ 找不到 {p}（先完成 ingest）')
    return json.loads(p.read_text())


def _filter_types(blocks: list[dict]) -> tuple[Counter, list[str]]:
    tc = Counter(b.get('type') for b in blocks)
    ft = [t for t in ALWAYS_FILTER if tc.get(t)]
    if tc.get('ref_text', 0) > 5:
        ft.append('ref_text')
    return tc, ft


def _heading_lvl(blocks: list[dict]) -> tuple[int, dict]:
    """heading 必有 text_level（leveled block）；body 段落也常以 'N.M' 開頭但 text_level=None。
    故投票**排除 None**——否則 None 灌票會把 lvl2-heading 書誤判成 1（body 全 flat）。"""
    lvls = Counter(b.get('text_level') for b in blocks
                   if b.get('type') == 'text' and b.get('text_level') in (1, 2, 3)
                   and _SECISH.match((b.get('text') or '').strip()))
    lvl = lvls.most_common(1)[0][0] if lvls else 1
    return (lvl or 1), dict(lvls)


def _headings(blocks: list[dict], lvl: int) -> list[tuple[int, int, str]]:
    return [(i, b.get('page_idx', -1), (b.get('text') or '').strip())
            for i, b in enumerate(blocks)
            if b.get('text_level') == lvl and b.get('type') == 'text']


def _chapter_candidates(headings: list[tuple[int, int, str]]) -> tuple[list[dict], int]:
    """回 (乾淨章序列, raw 命中數)。lvl1 書章標與 section 同層 → Pattern A 會氾濫命中；故對 raw
    候選貪婪取「從最小章號起、連續 +1 的首現」收斂成單調章序列（§3「數字遞增、唯一、無跳號」的
    確定性版）。判斷仍留 worker（可能漏拆兩段式章標），但給收斂候選而非數百行洪水。"""
    raw = []
    for idx, page, text in headings:
        low = text.lower()
        if any(low.startswith(k) for k in _NONCHAP):
            continue
        m = _CH_NUMBERED.match(text)
        if not m:
            continue
        num = next((g for g in m.groups() if g), None)
        if num is not None:
            raw.append({'idx': idx, 'page_idx': page, 'num': int(num), 'text': text[:70]})
    if not raw:
        return [], 0
    # TOC 訊號：目錄行尾隨頁碼（"Chapter 1 Basic Concepts 3"）→ 每章號優先取「非 TOC」出現，
    # 避免首現落在目錄頁而非正文章標。同章號全是 TOC 才退而取首現。
    toc = re.compile(r'\s\d+$')
    by_num: dict[int, list[dict]] = {}
    for c in raw:
        by_num.setdefault(c['num'], []).append(c)
    expect = min(by_num)
    picks = []
    while expect in by_num:
        cands = by_num[expect]
        pick = next((c for c in cands if not toc.search(c['text'])), cands[0])
        picks.append(pick)
        expect += 1
    return picks, len(raw)


def _boundary_candidates(headings: list[tuple[int, int, str]]) -> dict:
    """body/appendix/index/bibliography start-page 候選（§3 Step 4 的確定性掃描）。"""
    out: dict = {'appendices': []}
    for idx, page, text in headings:
        low = text.lower()
        if low.startswith('appendix'):
            out['appendices'].append({'idx': idx, 'page_idx': page, 'text': text[:60]})
        if 'index' not in out and re.fullmatch(r'index', low):
            out['index'] = {'idx': idx, 'page_idx': page}
        if 'bibliography' not in out and low in ('bibliography', 'references'):
            out['bibliography'] = {'idx': idx, 'page_idx': page}
    return out


def _regex_samples(blocks: list[dict]) -> dict:
    """供 §3 Step 7 regex 推斷的真實樣本：題號 prefix、equation \\tag、section heading。"""
    probs, eqs = [], []
    for b in blocks:
        t = (b.get('text') or '').strip()
        if b.get('type') in ('text', 'list') and re.match(r'^(?:Problem\s+|Exercise\s+|P[.\s])?\d', t):
            if len(probs) < 12:
                probs.append(t[:50])
        if b.get('type') == 'equation':
            m = re.search(r'\\tag\s*\{([^}]+)\}', t)
            if m and len(eqs) < 8:
                eqs.append(m.group(1))
    strip_dollar = sum(1 for b in blocks if b.get('type') == 'equation'
                       and (b.get('text') or '').lstrip().startswith('$$'))
    return {'problem_prefixes': probs, 'equation_tags': eqs,
            'equation_strip_dollar_likely': strip_dollar > 0}


def scout(slug: str, max_headings: int = 400) -> None:
    B = _load(slug)
    N = len(B)
    tc, ft = _filter_types(B)
    lvl, lvl_dist = _heading_lvl(B)
    H = _headings(B, lvl)
    chs, ch_raw = _chapter_candidates(H)
    bnd = _boundary_candidates(H)
    rx = _regex_samples(B)

    P = print
    P(f'# audit scout — {slug}（N={N} blocks）')
    P('\n本報告 = §3 Step 1–7 機械部分的確定性候選。**讀它即可，不要再開 content_list 生肉、'
      '不要讀 parser/build_catalogs 源碼**。每節標「→ 你判斷什麼」；最終 yaml 由你決定。\n')

    P('## Step 2 · type 統計 → filter_types')
    P(f'types = {dict(tc)}')
    P(f'→ 建議 filter_types = {ft}（ref_text 僅 count>5 才進；image/chart content 一律 ignore=true）')
    P('→ 你判斷：是否有非常規雜訊 type 要加。\n')

    P('## Step 3 · heading_text_level')
    P(f'section-like(^N.M…) 的 text_level 分布 = {lvl_dist} → 建議 heading_text_level = {lvl}')
    P(f'→ 你判斷：{lvl}=2 時 yaml **必須**設 heading_text_level: 2，否則 body 全 flat。'
      f'章標題可能落在不同 level（由下方 chapter 候選的 idx 指定，不受此欄影響）。\n')

    P(f'## Step 4–5 · 章節錨點候選（lvl={lvl} heading 收斂成單調章序列；raw 命中 {ch_raw}）')
    if chs:
        nums = [c['num'] for c in chs]
        for c in chs:
            P(f"  ch{c['num']:>2}  idx={c['idx']:<5} page_idx={c['page_idx']:<4} {c['text']!r}")
        note = f'（共 {len(chs)} 章 {nums[0]}–{nums[-1]} 連續）'
        if ch_raw > len(chs) * 3:
            note += f'；raw {ch_raw} 含大量 section 同層命中，已收斂取首現——⚠ 若某章標分兩段或被 section 搶首現，人工核 idx'
        P(f'→ {note}')
    else:
        P('  （無編號章候選——可能是純標題型章，需 Pattern B 人工判 + chapter_title_block_idx）')
    P('→ 你判斷：每章 chapter_title_block_idx=idx、page_start=page_idx、next_chapter_block_idx=下章 idx。\n')

    P('## Step 4 · body/appendix/index/bibliography 邊界候選')
    P(f"  appendices = {bnd['appendices'] or '（無 Appendix heading → appendices_start_page = 末章 page_end+1）'}")
    P(f"  index = {bnd.get('index', '（未見獨立 Index heading）')}")
    P(f"  bibliography = {bnd.get('bibliography', '（未見；填 null）')}")
    P('→ 你判斷：body_start_page=首個正文章的 page_idx；附錄吞 Index/Bib 時補 index/bibliography_start_page。\n')

    P('## Step 7 · regex 推斷樣本（真實 block 文字）')
    P(f"  題號 prefix 樣本 = {rx['problem_prefixes']}")
    P(f"  equation \\tag 樣本 = {rx['equation_tags']}")
    P(f"  equation_strip_dollar 建議 = {rx['equation_strip_dollar_likely']}")
    P('→ 你判斷：依樣本定 section_re/subsection_re/problem_start_re/equation_label_re（各 capture group '
      '數見 §2/§7；problem_chapter_must_match 看題號含不含 N.）；problems 區/inline 仍須跑 parser+smoke 驗。\n')

    P('## 接下來（§3 Step 7.5 / §5）')
    P('寫 extract_rules.yaml + _audit.md → validate_rules → parser → smoke → normalize_metadata，'
      '依 smoke 迭代（≤3 輪）。引擎切不動你這本 → 用 schema 欄位；欄位都涵蓋不了才 `proposals propose '
      '--domain engine --type tooling-gap`（§6），**絕不改 book_pipeline/*.py**。')


def main() -> int:
    ap = argparse.ArgumentParser(description='audit-book 確定性勘查')
    ap.add_argument('slug')
    ap.add_argument('--max-headings', type=int, default=400)
    a = ap.parse_args()
    scout(a.slug, a.max_headings)
    return 0


if __name__ == '__main__':
    sys.exit(main())
