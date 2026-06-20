"""對 _sol 解答書切 problems by chapter，並 merge 到主書 chNN.json 的 problem.solution 欄位。

解答書結構**因書而異**，故走 per-sol 配置：
  book_pipeline/mineru_data/<sol_slug>/sol_rules.yaml （入 git，貴重成果）

無 sol_rules.yaml 時用 Griffiths 預設（Chapter N / Problem N.M），與舊行為等價。

sol_rules.yaml schema：
  chapter_re:      '^Chapter\\s+(\\d+)\\s*$'          # 1 capture = 章號（int）
  problem_re:      '^Problem\\s+(\\d+\\.\\d+[a-z]?)'  # group(1) = 對主書 p['num'] 的 key
  multi_per_block: false   # true：一個 text block 內擠多答案（Boas 風格），用 finditer 切
  equation_label_re: '\\\\tag\\s*\\{([0-9]+\\.[0-9]+[a-z]?)\\}'  # 可選

key 對齊原則：problem_re 的 group(1) 必須等於主書 parsed/chNN.json 內 problem['num'] 字串。
  - Griffiths/Boas：主書 num 為 "N.M" → problem_re group(1) 抓 "N.M"
  - Kittel：主書 num 純整數（章內 reset）→ group(1) 抓題序整數，章靠 chapter_re
  - Hartle：sol 題號 "C-M"、主書 num 純整數 → group(1) 抓 M（題序）

CLI:
  python -m book_pipeline.sol_extract <main_slug> <sol_slug> [--dry-run]
  --dry-run：不寫主書，只印 per-chapter 配對率（校準 sol_rules.yaml 用）
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from . import editions
from . import parser

DATA_DIR = Path('book_pipeline/mineru_data')

DEFAULTS = {
    'chapter_re': r'^Chapter\s+(\d+)\s*$',
    'problem_re': r'^Problem\s+(\d+\.\d+[a-z]?)',
    'multi_per_block': False,
    'equation_label_re': r'\\tag\s*\{([0-9]+\.[0-9]+[a-z]?)\}',
}


def load_sol_rules(sol_slug: str) -> dict:
    p = DATA_DIR / sol_slug / 'sol_rules.yaml'
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    raw = raw or {}
    if raw.get('_pending'):
        sys.exit(f'{sol_slug}/sol_rules.yaml 標記 _pending（主書品質不足，不該 merge）。詳見該檔註解。')
    r = {**DEFAULTS, **{k: raw[k] for k in DEFAULTS if k in raw}}
    chap = re.compile(r['chapter_re'])
    prob = re.compile(r['problem_re'])
    if chap.groups != 1:
        sys.exit(f'sol_rules.chapter_re 須恰好 1 capture group（章號），現有 {chap.groups}')
    if prob.groups < 1:
        sys.exit('sol_rules.problem_re 須至少 1 capture group（group(1)=對主書 key）')
    return {
        'chapter_re': chap,
        'problem_re': prob,
        'multi_per_block': bool(r['multi_per_block']),
        'eq_label_re': re.compile(r['equation_label_re']),
    }


def extract_sol_chapters(sol_slug: str, rules: dict) -> dict[int, dict[str, list]]:
    """sol 書 → {ch_num: {problem_key: body[]}}"""
    blocks = parser.expand_list_blocks(json.loads(
        (DATA_DIR / sol_slug / 'unified' / 'content_list.json').read_text()
    ))
    chap_re = rules['chapter_re']
    prob_re = rules['problem_re']
    multi = rules['multi_per_block']
    eq_re = rules['eq_label_re']

    # 章 anchor：lvl=1 text 命中 chapter_re
    chapters: list[tuple[int, int]] = []
    for i, b in enumerate(blocks):
        if b.get('text_level') == 1 and b.get('type') == 'text':
            m = chap_re.match((b.get('text') or '').strip())
            if m:
                chapters.append((int(m.group(1)), i))

    out: dict[int, dict[str, list]] = {}
    for k, (ch_num, start) in enumerate(chapters):
        end = chapters[k + 1][1] if k + 1 < len(chapters) else len(blocks)
        problems: dict[str, list] = {}
        current_num: str | None = None
        current_body: list = []

        def flush():
            nonlocal current_num, current_body
            if current_num is not None:
                problems[current_num] = current_body

        for j in range(start + 1, end):
            b = blocks[j]
            text = (b.get('text') or '').strip()
            if b.get('type') == 'text':
                # multi：block 內所有命中；非 multi：僅行首一個
                if multi:
                    matches = list(prob_re.finditer(text))
                else:
                    m0 = prob_re.match(text)
                    matches = [m0] if m0 else []
                if matches:
                    # 第一個命中前的殘文補給前一題
                    pre = text[:matches[0].start()].strip()
                    if current_num is not None and pre:
                        current_body.append({'t': 'p', 'md': pre})
                    for idx, mt in enumerate(matches):
                        flush()
                        current_num = mt.group(1)
                        current_body = []
                        seg_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
                        seg = text[mt.end():seg_end].strip()
                        if seg:
                            current_body.append({'t': 'p', 'md': seg})
                    continue
            if current_num is None:
                continue
            struct = parser.block_to_struct(b, eq_re, False, False)
            if struct:
                current_body.append(struct)
        flush()
        out[ch_num] = problems
    return out


def merge_into_main(main_slug: str, sol_data: dict[int, dict[str, list]],
                    dry_run: bool = False) -> dict:
    """讀主書 parsed/chNN.json，把 problem.solution 注入（dry_run 不寫檔）。回傳統計。"""
    main_dir = DATA_DIR / main_slug / 'parsed'
    stats = {'chapters': 0, 'problems_total': 0, 'problems_with_sol': 0,
             'sol_unmatched': 0, 'per_ch': []}
    for ch_num, sol_problems in sorted(sol_data.items()):
        fname = main_dir / f'ch{ch_num:02d}.json'
        if not fname.exists():
            print(f'  ⚠ ch{ch_num:02d}.json 不存在，跳過（sol 有 {len(sol_problems)} 題）')
            continue
        data = json.loads(fname.read_text())
        stats['chapters'] += 1
        used = set()
        ch_tot = len(data.get('problems', []))
        ch_hit = 0
        for p in data['problems']:
            stats['problems_total'] += 1
            sol_body = sol_problems.get(p['num'])
            if sol_body is not None:
                if not dry_run:
                    p['solution'] = sol_body
                stats['problems_with_sol'] += 1
                ch_hit += 1
                used.add(p['num'])
            elif 'solution' in p and not dry_run:
                # 主書這題不在 sol（可能 sol 重 ingest 後少了）→ 移除舊 solution
                del p['solution']
        unmatched = set(sol_problems) - used
        stats['sol_unmatched'] += len(unmatched)
        stats['per_ch'].append((ch_num, ch_hit, ch_tot, sorted(unmatched)[:6]))
        if not dry_run:
            fname.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return stats


def edition_block_reason(sol_slug: str) -> str | None:
    """merge 前版本防護：讀 sol 的 editions.sol_alignment（書單管理 skill 由 LLM 親判寫入）。
    aligned 明確 False → 回拒絕原因（題號恐錯位）；對齊或未判（None/缺檔）→ None（fail-open 放行）。
    **不自己比對版本字串**（鐵律：版本對齊由 LLM 親判，本函式只讀其結論、不重判）。"""
    align = (editions.load(sol_slug) or {}).get('sol_alignment') or {}
    if align.get('aligned') is False:
        pv = align.get('parent_version') or '?'
        sv = align.get('sol_version') or '?'
        return f'解答本版次({sv}) ≠ 母書版次({pv})，題號恐錯位（LLM 親判不對齊）'
    return None


def _open_edition_mismatch_proposal(main_slug: str, sol_slug: str, reason: str) -> None:
    """版本不對齊 → 開 sol/edition-mismatch proposal（架構師換源/重解）。best-effort、不影響擋 merge。"""
    try:
        from . import proposals
        pid = proposals.propose(
            domain='sol', type_='edition-mismatch', source='sol_extract', slug=sol_slug,
            title=f'{sol_slug} 解答本版次與母書 {main_slug} 不對齊',
            evidence=reason,
            proposal='換源找對應母書版次的解答本，或調 booklists edition_pref 後重解析母子書',
            risk='不修則該書解答題號錯位、答非所問')
        print(f'  → 已開 proposal {pid}（sol/edition-mismatch）')
    except Exception as e:
        print(f'  （proposal 開立失敗、不影響擋 merge：{e}）')


def main(main_slug: str, sol_slug: str, dry_run: bool = False) -> int:
    # 版本對齊閘（純防護、淨安全）：LLM 親判母子版次不對齊 → 拒 merge、開 proposal。dry_run（校準
    # 配對率）不擋。fail-open：未判/對齊一律放行（不擋好書）。
    reason = edition_block_reason(sol_slug)
    if reason and not dry_run:
        print(f'[sol] ⚠ 版本對齊閘擋下 merge：{reason}')
        _open_edition_mismatch_proposal(main_slug, sol_slug, reason)
        print('  → 不 merge（題號錯位防護；交架構師換源/重解後再跑）')
        return 3
    rules = load_sol_rules(sol_slug)
    cfg = 'sol_rules.yaml' if (DATA_DIR / sol_slug / 'sol_rules.yaml').exists() else '預設(Griffiths)'
    print(f'[sol] extract {sol_slug}  config={cfg}  multi_per_block={rules["multi_per_block"]}')
    sol_data = extract_sol_chapters(sol_slug, rules)
    total_sol = sum(len(v) for v in sol_data.values())
    print(f'  抽出 {len(sol_data)} 章、{total_sol} 題解答')

    print(f'[sol] {"DRY-RUN " if dry_run else ""}merge → {main_slug}/parsed/chNN.json')
    stats = merge_into_main(main_slug, sol_data, dry_run=dry_run)
    pct = 100 * stats['problems_with_sol'] // max(stats['problems_total'], 1)
    print(f'  章={stats["chapters"]}'
          f' 題總={stats["problems_total"]}'
          f' 配對成功={stats["problems_with_sol"]} ({pct}%)'
          f' sol 沒對到主書={stats["sol_unmatched"]}')
    if dry_run:
        print('  per-chapter（章: 配對/主書題數  未配對sol樣本）：')
        for ch, hit, tot, un in stats['per_ch']:
            mark = '' if hit else '  ⚠ 全空'
            print(f'    ch{ch:02d}: {hit}/{tot}{mark}  un={un}')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('main_slug')
    ap.add_argument('sol_slug')
    ap.add_argument('--dry-run', action='store_true',
                    help='不寫主書，只印 per-chapter 配對率（校準 sol_rules.yaml 用）')
    a = ap.parse_args()
    raise SystemExit(main(a.main_slug, a.sol_slug, dry_run=a.dry_run))
