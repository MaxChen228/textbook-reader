"""book_pipeline/validate_rules.py — extract_rules.yaml schema 驗證（audit-book §5 硬門檻）。

吃 mineru_data/<slug>/{extract_rules.yaml, unified/content_list.json}，
檢查 yaml 是否符合 parser.py 能安全消化的 schema 契約。**不產任何檔**，純驗證。

audit-book.md §5「主對話 Validate（commit 前必做）」的單一真相：
規則改這裡，audit-book.md §5 只引用本 CLI，避免內聯 python 漂移。

用法：
  uv run --with pyyaml python -m book_pipeline.validate_rules <slug>
退出碼：0=合規、1=不合規（列出每條違規）
"""
from __future__ import annotations

import re
import argparse
import sys
from pathlib import Path

import yaml

from book_pipeline.parser import DATA_DIR, load_unified, _valid_slug

SCHEMA_KEYS = {
    'slug', 'title', 'author', 'edition', 'subject', 'publisher', 'language',
    'filter_types', 'ignore_image_content', 'ignore_chart_content',
    'body_start_page', 'appendices_start_page', 'bibliography_start_page',
    'index_start_page', 'inline_problems', 'chapters', 'appendices',
    'section_re', 'subsection_re', 'heading_priority',
    'problem_start_re', 'problem_chapter_must_match',
    'problem_num_namespace_by_section', 'problems_end_re', 'solution_start_re',
    'problems_start_re', 'suppress_running_header_sections',
    'equation_strip_dollar', 'equation_label_re',
    'example_start_re', 'figure_caption_merge', 'figure_caption_main_re',
    'known_missing_problems', 'heading_text_level',
}
REQUIRED_KEYS = (
    'slug', 'title', 'body_start_page', 'appendices_start_page',
    'chapters', 'section_re', 'subsection_re', 'heading_priority',
    'problem_start_re', 'problem_chapter_must_match',
    'equation_strip_dollar', 'equation_label_re',
)
ALLOWED_FILTER = {'header', 'page_number', 'footer', 'page_footnote', 'aside_text', 'ref_text'}


def validate(slug: str) -> int:
    if not _valid_slug(slug):
        print(f'❌ {slug}: invalid slug')
        return 1
    rules_path = DATA_DIR / slug / 'extract_rules.yaml'
    if not rules_path.is_file():
        print(f'❌ {slug}: 缺 extract_rules.yaml：{rules_path}')
        return 1
    try:
        R = yaml.safe_load(rules_path.read_text())
    except Exception as e:
        print(f'❌ {slug}: YAML 載入失敗: {e}')
        return 1
    if not isinstance(R, dict):
        print(f'❌ {slug}: extract_rules.yaml 不是 mapping')
        return 1
    B = load_unified(slug)
    N = len(B)
    errs: list[str] = []

    for k in REQUIRED_KEYS:
        if k not in R:
            errs.append(f'缺 top-level key: {k}')

    if R.get('heading_priority') != ['subsection_re', 'section_re']:
        errs.append('heading_priority 必須是 [subsection_re, section_re]')

    # Regex 可編譯
    for k in ('section_re', 'subsection_re', 'problem_start_re', 'equation_label_re'):
        if k in R:
            try:
                re.compile(R[k])
            except Exception as e:
                errs.append(f'{k} 編譯失敗: {e}')
    for k in ('example_start_re', 'figure_caption_main_re', 'problems_end_re', 'solution_start_re',
              'problems_start_re'):
        if R.get(k):
            try:
                re.compile(R[k])
            except Exception as e:
                errs.append(f'{k} 編譯失敗: {e}')

    # capture group 數
    for k in ('section_re', 'subsection_re'):
        if k in R:
            try:
                if re.compile(R[k]).groups != 2:
                    errs.append(f'{k} 必須有恰好 2 個 capture group')
            except Exception:
                pass
    if 'problem_start_re' in R:
        try:
            if re.compile(R['problem_start_re']).groups != 1:
                errs.append('problem_start_re 必須恰好 1 個 capture group')
        except Exception:
            pass
    if R.get('example_start_re'):
        try:
            if re.compile(R['example_start_re']).groups != 1:
                errs.append('example_start_re 必須恰好 1 個 capture group')
        except Exception:
            pass

    # section_re 不准對 N.M 純編號 block 命中且 title 為空（thomas_calculus 慘案）
    if 'section_re' in R:
        try:
            sec = re.compile(R['section_re'])
            bad_samples = []
            for s in ('1.1', '1.1 ', '4.1', '4.1 ', '10.5 '):
                m = sec.match(s)
                if m and (m.group(2) or '').strip() == '':
                    bad_samples.append(s)
            if bad_samples:
                errs.append(f'section_re 對純編號 block {bad_samples} 命中且 title 為空 — '
                            f'N.M alternation 須強制 title 非空（用 \\s+(.+)$ 或 lookahead (?=[\\s ]+\\S)）')
        except Exception:
            pass

    # heading_text_level：選填，MinerU section heading 的 text_level（預設 1，須 ≥1 正整數）。
    # 可為 list[int]——OCR level 不一致時兩級都收（Dummit&Foote [1, 2]）。
    if 'heading_text_level' in R:
        hl = R['heading_text_level']
        hl_list = hl if isinstance(hl, list) else [hl]
        if not hl_list or not all(isinstance(x, int) and not isinstance(x, bool) and x >= 1 for x in hl_list):
            errs.append(f'heading_text_level 須為 ≥1 的整數或其 list（得到 {hl!r}）')

    # suppress_running_header_sections：選填 bool（opt-in；inline 模式抑制頁頂跑馬燈假 section
    # heading 推進 namespace）。非 bool 會靜默落 truthy/falsy → 明確擋下。
    if 'suppress_running_header_sections' in R and not isinstance(R['suppress_running_header_sections'], bool):
        errs.append(f'suppress_running_header_sections 須為 bool（得到 {R["suppress_running_header_sections"]!r}）')

    # known_missing_problems schema: list of {chapter:int, nums:[str]}
    for i, kp in enumerate(R.get('known_missing_problems') or []):
        if not isinstance(kp, dict):
            errs.append(f'known_missing_problems[{i}] 不是 dict（須 {{chapter, nums}}）')
            continue
        if not isinstance(kp.get('chapter'), int):
            errs.append(f'known_missing_problems[{i}].chapter 不是 int')
        if not isinstance(kp.get('nums'), list):
            errs.append(f'known_missing_problems[{i}].nums 不是 list')

    # chapter 結構與 idx 合法性
    for i, c in enumerate(R.get('chapters', []) or []):
        for k in ('num', 'title', 'page_start', 'page_end',
                  'chapter_title_block_idx', 'problems_block_idx', 'next_chapter_block_idx'):
            if k not in c:
                errs.append(f'chapter[{i}] 缺 {k}')
        cti = c.get('chapter_title_block_idx')
        nci = c.get('next_chapter_block_idx')
        pbi = c.get('problems_block_idx')
        if cti is not None and not (0 <= cti < N):
            errs.append(f'chapter[{i}] chapter_title_block_idx={cti} 超出 [0,{N})')
        if nci is not None and not (0 < nci <= N):
            errs.append(f'chapter[{i}] next_chapter_block_idx={nci} 超出 (0,{N}]')
        if cti is not None and nci is not None and cti >= nci:
            errs.append(f'chapter[{i}] chapter_title_block_idx({cti}) >= next_chapter_block_idx({nci})')
        if pbi is not None and cti is not None and nci is not None and not (cti < pbi < nci):
            errs.append(f'chapter[{i}] problems_block_idx({pbi}) 不在 ({cti},{nci})')

    # 章號連續無跳號
    nums = [c.get('num') for c in (R.get('chapters', []) or [])]
    if nums and nums != list(range(nums[0], nums[0] + len(nums))):
        errs.append(f'章號跳號或非遞增: {nums}')

    # 附錄 idx 合法
    for i, a in enumerate(R.get('appendices', []) or []):
        cti = a.get('chapter_title_block_idx')
        if cti is None or not (0 <= cti < N):
            errs.append(f'appendix[{i}] chapter_title_block_idx 不合法')

    # filter_types 白名單
    bad = set(R.get('filter_types', []) or []) - ALLOWED_FILTER
    if bad:
        errs.append(f'filter_types 含未知值: {bad}')

    # 未知 top-level key
    extra = set(R.keys()) - SCHEMA_KEYS
    if extra:
        errs.append(f'未知 top-level key: {extra}')

    # inline_problems 一致性：pbi=null 章內若有 problem_start_re 命中 → 須 inline_problems=true
    inline = R.get('inline_problems', False)
    if not isinstance(inline, bool):
        errs.append('inline_problems 必須是 bool')
    if not inline and 'problem_start_re' in R:
        try:
            ps_re = re.compile(R['problem_start_re'])
            pe_re = re.compile(R['problems_end_re']) if R.get('problems_end_re') else None
            # 章末習題區 heading 詞彙（= audit-book Step 6 P1 精確集 + P2 regex）。binary 模式下真題
            # 一律落在這種 heading 下；故 pbi=null 章「有 problem_start_re 命中」唯有同時存在【未被
            # anchor 的 problems heading】才是 audit 漏抓 anchor 真 bug。無此 heading 的命中＝正文編號
            # 散文清單（振動模式/Boolean 定律/軟體分類…），純 binary 書的無題章本就會有此形態 → 不報。
            # （heading-less inline 題誤設 binary 的 case 靜態無從分辨，歸 parser 動態習題完整性閘。）
            PROB_HEAD_EXACT = {
                'Problems', 'PROBLEMS', 'Exercises', 'EXERCISES',
                'Problem Set', 'PROBLEM SET', '問題', '練習題',
            }
            ph_re = re.compile(
                r'^(Further\s+Problems|End[-\s]of[-\s]Chapter\s+Problems'
                r'|Chapter\s+\d+\s+Problems|Problems\s+for\s+Chapter\s+\d+)'
                r'(\s+on\s+Chapter\s+\d+)?\s*$'
            )

            def _has_unanchored_problems_heading(cti: int, nci: int) -> bool:
                for j in range(cti + 1, nci):
                    b = B[j]
                    if b.get('type') != 'text':
                        continue
                    t = (b.get('text') or '').strip()
                    if t in PROB_HEAD_EXACT or ph_re.match(t):
                        return True
                return False

            bad_chs = []
            for c in (R.get('chapters', []) or []):
                if c.get('problems_block_idx') is not None:
                    continue
                cti, nci = c.get('chapter_title_block_idx'), c.get('next_chapter_block_idx')
                if cti is None or nci is None:
                    continue
                has_hit = False
                for j in range(cti + 1, nci):
                    b = B[j]
                    text = (b.get('text') or '').strip()
                    # problems_end_re lvl1 命中後的 problem_start 命中是合法非題（如 SOCIAL ISSUES）
                    if pe_re is not None and b.get('text_level') == 1 and b.get('type') == 'text' \
                            and pe_re.match(text):
                        break
                    if b.get('type') in ('text', 'list') and ps_re.match(text):
                        has_hit = True
                        break
                # 收窄：命中 ∧ 章內有未 anchor 的 problems heading → 漏抓 anchor 真 bug；
                # 命中但無 problems heading → 正文散文編號清單（純 binary 書無題章常態）→ 不報。
                if has_hit and _has_unanchored_problems_heading(cti, nci):
                    bad_chs.append(c.get('num'))
            if bad_chs:
                errs.append(f'章 {bad_chs} pbi=null 但章內有未 anchor 的 Problems/Exercises heading'
                            f'（應填正確 pbi；若真為 inline 散落題則設 inline_problems=true）')
        except Exception:
            pass

    if errs:
        print(f'❌ {slug}: 不合規（{len(errs)} 項）')
        for e in errs:
            print(f'  - {e}')
        return 1
    print(f'✅ {slug}: {len(R["chapters"])} chapters, '
          f'{len(R.get("appendices", []) or [])} appendices, N={N}')
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='驗證 mineru_data/<slug>/extract_rules.yaml schema')
    ap.add_argument('slug')
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    return validate(args.slug)


if __name__ == '__main__':
    sys.exit(main())
