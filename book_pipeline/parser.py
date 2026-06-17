"""book_pipeline/parser.py — deterministic 規則化解析器。

吃 mineru_data/<slug>/unified/content_list.json + extract_rules.yaml
產出 mineru_data/<slug>/parsed/{book.json, ch{NN}.json, app{X}.json}

設計原則：
  - 零 LLM、純 regex + idx 操作。可重複跑、輸出 deterministic。
  - 文字逐字 copy 自 content_list，**不重寫**。
  - 雜訊型 block（filter_types）100% 不出現在輸出。
  - 公式 `$$` 包殼剝掉、`\\tag{...}` 抽 label。
  - 題號從 problem body 起頭剝掉，存到 problem.num。

用法：
  uv run --with pyyaml python -m book_pipeline.parser <slug>
  uv run --with pyyaml python -m book_pipeline.parser sakurai_mqm3
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

try:
    from book_pipeline import build_catalogs
    from book_pipeline.math_normalize import normalize_chunk_math, normalize_tex
except ModuleNotFoundError:  # 允許 uv run python book_pipeline/parser.py <slug>
    import build_catalogs
    from math_normalize import normalize_chunk_math, normalize_tex

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'book_pipeline' / 'mineru_data'


# ── 讀檔 ─────────────────────────────────────────────────────────────────────

def load_rules(slug: str) -> dict:
    path = DATA_DIR / slug / 'extract_rules.yaml'
    if not path.is_file():
        sys.exit(f'缺 extract_rules.yaml：{path}')
    return yaml.safe_load(path.read_text())


def load_unified(slug: str) -> list[dict]:
    path = DATA_DIR / slug / 'unified' / 'content_list.json'
    if not path.is_file():
        sys.exit(f'缺 unified content_list：{path}')
    return json.loads(path.read_text())


# ── 公式處理 ──────────────────────────────────────────────────────────────────

EQ_WRAPPER_RE = re.compile(r'^\$\$\s*\n?(.*?)\n?\s*\$\$\s*$', re.DOTALL)


def strip_eq_wrapper(raw: str) -> str:
    """剝掉 $$...$$ 包殼。"""
    m = EQ_WRAPPER_RE.match(raw.strip())
    return m.group(1).strip() if m else raw.strip()


def extract_eq_label(tex: str, label_re: re.Pattern) -> tuple[str, str | None]:
    """從 tex 末段抽 \\tag{X.Y}，回 (tex_without_tag, label)。"""
    m = label_re.search(tex)
    if not m:
        return tex, None
    label = m.group(1)
    # 移除 \tag{...} 本身（可能 trailing 空白），保留主公式
    cleaned = label_re.sub('', tex).strip()
    return cleaned, label


# ── block → struct ───────────────────────────────────────────────────────────

CAT_NUM_PATTERN = r'[A-Z]?\d+[A-Z]?(?:[.\-–—]\d+)+(?:[A-Za-z](?![A-Za-z]))?'


def fig_id_from_caption(caption: str) -> str | None:
    """從 caption 開頭 'Fig. 1.4' / 'FIGURE 1.4' 抽出 '1.4' → 'fig-1.4'。"""
    m = re.search(
        rf'\bFig(?:ure|\.)?\s*({CAT_NUM_PATTERN})',
        caption.strip(),
        re.IGNORECASE,
    )
    if not m:
        return None
    num = re.sub(r'[\-–—]', '.', m.group(1))
    return f'fig-{num}'


TABLE_ID_RE = re.compile(
    rf'\b(?:Table|TABLE|Tab\.?)\s*({CAT_NUM_PATTERN})',
    re.IGNORECASE,
)


def table_id_from_caption(caption: str) -> str | None:
    """從 caption 的正式 'Table 1.1' / 'Tab. 2.3' 抽出編號 → 'tbl-1.1'。"""
    m = TABLE_ID_RE.search(caption.strip())
    if not m:
        return None
    num = re.sub(r'[\-–—]', '.', m.group(1))
    return f'tbl-{num}'


def _media_fallback_id(t: str, stem: str, source: str, idx: int) -> str:
    prefix = {'fig': 'fig', 'table': 'tbl', 'eq': 'eq'}[t]
    suffix = f'{source}-{idx}' if source != 'body' else str(idx)
    return f'{prefix}-{stem}-{suffix}'


def _dedupe_id(raw_id: str, seen: dict[str, int]) -> str:
    if raw_id not in seen:
        seen[raw_id] = 0
        return raw_id
    seen[raw_id] += 1
    return f'{raw_id}--{seen[raw_id]}'


def _assign_block_ids(blocks: list[dict], stem: str, source: str,
                      seen: dict[str, int]) -> None:
    for idx, b in enumerate(blocks):
        t = b.get('t')
        if t not in ('fig', 'table', 'eq'):
            continue
        raw_id = (b.get('id') or '').strip()
        if not raw_id:
            if t == 'eq' and b.get('label'):
                raw_id = f"eq-{b['label']}"
            else:
                raw_id = _media_fallback_id(t, stem, source, idx)
        b['id'] = _dedupe_id(raw_id, seen)


def assign_catalog_ids(chunk: dict, stem: str) -> None:
    """補齊圖、表、公式 block 的 DOM/catalog 共用 id。"""
    seen: dict[str, int] = {}
    _assign_block_ids(chunk.get('body', []), stem, 'body', seen)
    for pidx, prob in enumerate(chunk.get('problems', [])):
        _assign_block_ids(prob.get('body', []), stem, f'prob{pidx}', seen)
        _assign_block_ids(prob.get('solution', []), stem, f'sol{pidx}', seen)


def block_to_struct(b: dict, label_re: re.Pattern,
                    ignore_image_content: bool, ignore_chart_content: bool) -> dict | None:
    """單一 MinerU block → 我們的 schema element。回 None 表示忽略。"""
    t = b.get('type')
    text = (b.get('text') or '').strip()

    if t in ('text', 'list'):
        # MinerU 的 type=list 常把內容放在 list_items 陣列、text 留空。
        # 必須讀 list_items 才不會漏題（Griffiths Problem 1.44/1.45/2.58 等 (a)(b)(c) 子題）。
        if t == 'list' and not text:
            items = [(x or '').strip() for x in (b.get('list_items') or [])]
            items = [x for x in items if x]
            if items:
                return {'t': 'p', 'md': '\n\n'.join(items)}
            return None
        if not text:
            return None
        return {'t': 'p', 'md': text}

    if t == 'equation':
        # normalize 須在抽 label 前：R1 把 \tag{$..$}→\tag{..} 才被 label_re 命中
        tex = normalize_tex(strip_eq_wrapper(b.get('text') or ''))
        tex, label = extract_eq_label(tex, label_re)
        if not tex:
            return None
        out: dict = {'t': 'eq', 'tex': tex}
        if label:
            out['label'] = label
        return out

    if t in ('image', 'chart'):
        img_path = b.get('img_path') or ''
        fname = img_path.rsplit('/', 1)[-1] if img_path else ''
        if not fname:
            return None
        # ignore_image_content / ignore_chart_content 在 rules，丟 b['content']
        captions = b.get('chart_caption') if t == 'chart' else b.get('image_caption')
        captions = captions or []
        caption = ' '.join(c.strip() for c in captions if c).strip()
        out: dict = {'t': 'fig', 'src': fname}
        # kind：natural_image → photo（暗色溫和 dim）；其他 → line（暗色反相，含 None / text_image / chart / flowchart / chemical）
        out['kind'] = 'photo' if b.get('sub_type') == 'natural_image' else 'line'
        # aspect：由 MinerU bbox 算寬高比，前端用 aspect-ratio 預留 layout 避免 CLS
        bbox = b.get('bbox') or []
        if len(bbox) == 4:
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            if w > 0 and h > 0:
                out['aspect'] = round(w / h, 3)
        if caption:
            out['caption'] = caption
            fid = fig_id_from_caption(caption)
            if fid:
                out['id'] = fid
        return out

    if t == 'code':
        code = (b.get('code_body') or b.get('content') or '').strip()
        if not code:
            return None
        captions = b.get('code_caption') or []
        caption = ' '.join(c.strip() for c in captions if c).strip()
        out = {'t': 'table', 'html': f'<pre><code>{html.escape(code)}</code></pre>'}
        if caption:
            out['caption'] = caption
            tid = table_id_from_caption(caption)
            if tid:
                out['id'] = tid
            else:
                fid = fig_id_from_caption(caption)
                if fid:
                    out['id'] = fid
        return out

    if t == 'table':
        body_html = b.get('table_body') or ''
        if not body_html:
            return None
        captions = b.get('table_caption') or []
        footnotes = b.get('table_footnote') or []
        out = {'t': 'table', 'html': body_html}
        cap = ' '.join(c.strip() for c in captions if c).strip()
        if cap:
            out['caption'] = cap
            tid = table_id_from_caption(cap)
            if tid:
                out['id'] = tid
            else:
                fid = fig_id_from_caption(cap)
                if fid:
                    out['id'] = fid
        fn = ' '.join(f.strip() for f in footnotes if f).strip()
        if fn:
            out['footnote'] = fn
        return out

    return None


# ── heading 偵測 ──────────────────────────────────────────────────────────────

def detect_heading(text: str, lvl: int | None,
                   section_re: re.Pattern, subsection_re: re.Pattern,
                   example_re: re.Pattern | None,
                   heading_level: int | list[int] = 1) -> dict | None:
    """檢查 text 是否為 section/subsection/example heading。回 None 表非 heading。
    heading_level：MinerU 把 section heading 標成哪個 text_level（預設 1）。可給 int
    或 list[int]——OCR level 不一致時（Dummit&Foote 多數 section lvl2、少數 straggler lvl1）
    設 [1, 2] 兩級都收。Munkres 這類 chapter=lvl1、§-section=lvl2 的書設 2。"""
    levels = heading_level if isinstance(heading_level, (list, tuple, set, frozenset)) else (heading_level,)
    if lvl not in levels:
        return None
    t = text.strip()
    # section id 去內部空白：救低品質 OCR 把 section 編號拆字（Dummit&Foote `1 0.1`→`10.1`、
    # `1 .1`→`1.1`）。clean 書本 id 無空白 → no-op，對其他書無影響。
    # subsection 優先（1.1.1）才不會被 section regex 吞掉
    m = subsection_re.match(t)
    if m:
        return {'t': 'subsection', 'id': re.sub(r'\s+', '', m.group(1)), 'title': m.group(2).strip()}
    m = section_re.match(t)
    if m:
        return {'t': 'section', 'id': re.sub(r'\s+', '', m.group(1)), 'title': m.group(2).strip()}
    if example_re:
        m = example_re.match(t)
        if m:
            return {'t': 'example', 'id': m.group(1)}
    return None


# ── body 構築 ─────────────────────────────────────────────────────────────────

def build_body(blocks: list[dict], rules: dict, label_re: re.Pattern,
               section_re: re.Pattern, subsection_re: re.Pattern,
               example_re: re.Pattern | None,
               figure_caption_merge: bool,
               figure_caption_main_re: re.Pattern | None) -> list[dict]:
    """blocks → body[]（流水順序）。Example 視為 inline marker，後續段落按順序排在 body，
    暫不做子樹包覆（schema 是 flat array，example 後面的 block 直到下個 heading 都屬於該 example）。"""
    ignore_image_content = rules.get('ignore_image_content', False)
    ignore_chart_content = rules.get('ignore_chart_content', False)
    heading_level = rules.get('heading_text_level', 1)

    out: list[dict] = []
    pending_fig_subcaption: str | None = None   # for figure_caption_merge

    for b in blocks:
        text = (b.get('text') or '').strip()
        t = b.get('type')
        lvl = b.get('text_level')

        # heading
        if t == 'text':
            h = detect_heading(text, lvl, section_re, subsection_re, example_re, heading_level)
            if h:
                out.append(h)
                continue

        # 圖片 caption 合併：若上一個 fig caption 是 "(a)" 等子標、本 fig caption 是 "Fig. X.Y ..."，
        # 則把上一個 fig 的 caption 換成本 caption，本 fig 用空 caption
        struct = block_to_struct(b, label_re, ignore_image_content, ignore_chart_content)
        if struct is None:
            continue

        if figure_caption_merge and struct['t'] == 'fig':
            caption = struct.get('caption', '')
            if figure_caption_main_re and caption and figure_caption_main_re.match(caption):
                # 找最近的 fig（caption 是子標 "(a)" / "(b)"）
                for prev in reversed(out):
                    if prev.get('t') == 'fig':
                        prev_cap = prev.get('caption', '')
                        if re.match(r'^\([a-z]\)', prev_cap):
                            prev['caption'] = caption
                            fid = fig_id_from_caption(caption)
                            if fid:
                                prev['id'] = fid
                            # 本 fig 保留為獨立 block 但去掉重複 caption
                            struct.pop('caption', None)
                            struct.pop('id', None)
                        break

        out.append(struct)

    return out


# ── problems 切題 ─────────────────────────────────────────────────────────────

def expand_list_blocks(blocks: list[dict]) -> list[dict]:
    """把 type=list（text 空、內容在 list_items）攤平成多個 type=text block。
    MinerU 常把連續編號題目（"2. ...", "3. ..."）OCR 成單一 list block，
    切題迴圈只看 b['text'] 會整段漏掉 → 每個 list_item 各自成 text block，
    讓 problem_start_re 能逐項命中。list_item 不繼承 text_level（避免被誤判 heading）。"""
    out: list[dict] = []
    for b in blocks:
        if b.get('type') == 'list' and not (b.get('text') or '').strip():
            items = [(x or '').strip() for x in (b.get('list_items') or [])]
            items = [x for x in items if x]
            if items:
                for it in items:
                    nb = dict(b)
                    nb['type'] = 'text'
                    nb['text'] = it
                    nb.pop('list_items', None)
                    nb.pop('text_level', None)
                    out.append(nb)
                continue
        out.append(b)
    return out


def split_problems(blocks: list[dict], rules: dict, ch_num: int,
                   label_re: re.Pattern, problem_start_re: re.Pattern,
                   problem_chapter_must_match: bool,
                   section_re: re.Pattern | None = None,
                   subsection_re: re.Pattern | None = None,
                   problems_end_re: re.Pattern | None = None,
                   solution_start_re: re.Pattern | None = None) -> list[dict]:
    """problems 區 blocks → problems[]。每題 num 從 text 起頭剝掉。
    section_re/subsection_re：lvl=1 text 匹配時 close 當前題目（救 inline-problem 書本
    把章節 heading 誤吞進 problem.body 的問題）。對章末 Problems 區型書無副作用。
    problems_end_re：lvl=1 text 命中 → 提早結束 problems 區、丟棄其後所有 block（救 brookshear
    章末 CHAPTER REVIEW PROBLEMS 後緊接的 SOCIAL ISSUES/ADDITIONAL READING 用相同 N. 編號被吸入）。"""
    ignore_image_content = rules.get('ignore_image_content', False)
    ignore_chart_content = rules.get('ignore_chart_content', False)
    heading_level = rules.get('heading_text_level', 1)

    problems: list[dict] = []
    current: dict | None = None
    in_solution = False  # solution_start_re 命中後：後續 block 收進 current['solution']
    max_num_seen = 0     # 章末 Problems 題號嚴格遞增；回退 → 已離開題目區（supplement 正文）

    for b in expand_list_blocks(blocks):
        t = b.get('type')
        text = (b.get('text') or '').strip()

        # problems-end terminator（最高優先）：heading-lvl text 命中 → close current 並停止整個 problems 區
        if (problems_end_re is not None and b.get('text_level') == heading_level and t == 'text'
                and problems_end_re.match(text)):
            if current:
                problems.append(current)
                current = None
                in_solution = False
            break

        # solution-start：題目開啟中遇 solution heading → 後續收進 current['solution']（heading 丟棄）
        if (solution_start_re is not None and current is not None and b.get('text_level') == heading_level
                and t == 'text' and solution_start_re.match(text)):
            in_solution = True
            continue

        # heading-terminator：heading-lvl text 命中 section/subsection regex → close current
        if (current is not None and b.get('text_level') == heading_level and t == 'text'
                and ((section_re and section_re.match(text))
                     or (subsection_re and subsection_re.match(text)))):
            problems.append(current)
            current = None
            in_solution = False
            continue

        # 偵測新題起點：text 型 + 行首 N.M 開頭（含 lvl=1 短句如 "1.2 Prove"）
        if t in ('text', 'list') and text:
            m = problem_start_re.match(text)
            if m:
                num = m.group(1)
                # 章號 prefix 比對
                if problem_chapter_must_match:
                    try:
                        if int(num.split('.')[0]) != ch_num:
                            # 不屬於本章 → 不視為題目起點
                            m = None
                    except ValueError:
                        m = None
                # 遞增守則：題號回退（≤ 已見最大）視為離開題目區的偽命中，不開新題
                if m and max_num_seen > 0:
                    try:
                        if int(num.split('.')[-1]) <= max_num_seen:
                            m = None
                    except ValueError:
                        pass
                if m:
                    # close 上一題
                    if current:
                        problems.append(current)
                    # 剝題號
                    tail = text[m.end():].strip()
                    current = {'num': num, 'body': []}
                    in_solution = False
                    try:
                        max_num_seen = max(max_num_seen, int(num.split('.')[-1]))
                    except ValueError:
                        pass
                    if tail:
                        current['body'].append({'t': 'p', 'md': tail})
                    continue

        if current is None:
            # 還沒看到第一題（problems 區開頭可能有「Problems」heading 與序言），跳過
            continue

        struct = block_to_struct(b, label_re, ignore_image_content, ignore_chart_content)
        if struct is None:
            continue
        (current.setdefault('solution', []) if in_solution else current['body']).append(struct)

    if current:
        problems.append(current)
    return problems


# ── 章節 / 附錄解析 ───────────────────────────────────────────────────────────

def slice_blocks(all_blocks: list[dict], ranges: list[tuple[int, int]]) -> list[dict]:
    """從 [(start_block_idx_inclusive, end_block_idx_exclusive), ...] 取 block。"""
    out: list[dict] = []
    for s, e in ranges:
        out.extend(all_blocks[s:e])
    return out


def filter_noise(blocks: list[dict], filter_types: set[str]) -> list[dict]:
    return [b for b in blocks if b.get('type') not in filter_types]


def compile_regexes(rules: dict) -> dict[str, re.Pattern]:
    return {
        'section': re.compile(rules['section_re']),
        'subsection': re.compile(rules['subsection_re']),
        'problem_start': re.compile(rules['problem_start_re']),
        'eq_label': re.compile(rules['equation_label_re']),
        'example': re.compile(rules['example_start_re']) if rules.get('example_start_re') else None,
        'fig_main_caption': re.compile(rules['figure_caption_main_re']) if rules.get('figure_caption_main_re') else None,
        'problems_end': re.compile(rules['problems_end_re']) if rules.get('problems_end_re') else None,
        'solution_start': re.compile(rules['solution_start_re']) if rules.get('solution_start_re') else None,
    }


def walk_inline_chapter(blocks: list[dict], rules: dict, ch_num: int,
                        label_re: re.Pattern, problem_start_re: re.Pattern,
                        section_re: re.Pattern, subsection_re: re.Pattern,
                        example_re: re.Pattern | None,
                        problem_chapter_must_match: bool,
                        namespace_by_section: bool = False,
                        problems_end_re: re.Pattern | None = None,
                        solution_start_re: re.Pattern | None = None) -> tuple[list[dict], list[dict]]:
    """Inline-mode 章節 walker：題目散落正文中（Griffiths 風格）。
    單流水掃整章 [cti+1, nci-1]，無 problems_block_idx 二分。
    - text + lvl=1 命中 section/subsection/example → close current problem，heading 進 chapter body
    - text/list 開頭命中 problem_start_re → close 舊題、開新題
    - 其他 block → 在題目模式進 problem.body，否則進 chapter.body

    namespace_by_section=True（Strang per-section Problem Set 風格）：
      每節題號從 1 重置，walker 把當前 section_id 串到 num 前避免同章重複。
      e.g. §1.2 題 1 → num='1.2.1'。fallback：未遇 section 前用 ch_num。
    """
    ignore_image_content = rules.get('ignore_image_content', False)
    ignore_chart_content = rules.get('ignore_chart_content', False)
    heading_level = rules.get('heading_text_level', 1)

    body: list[dict] = []
    problems: list[dict] = []
    current: dict | None = None
    current_section_id: str = str(ch_num)  # fallback：第一個 section 前用章號
    max_num_seen = 0        # 非 namespace 模式：題號遞增守則（擋正文 numbered list 偽命中）
    problems_ended = False  # problems_end_re 命中後：題目起點失效、後續歸 body
    in_solution = False     # solution_start_re 命中後：後續 block 收進 current['solution']

    for b in expand_list_blocks(blocks):
        t = b.get('type')
        text = (b.get('text') or '').strip()
        lvl = b.get('text_level')

        # problems-end terminator（最高優先）：close current，其後不再開新題（heading/內容歸 body）
        if (not problems_ended and problems_end_re is not None and lvl == heading_level and t == 'text'
                and problems_end_re.match(text)):
            if current:
                problems.append(current)
                current = None
                in_solution = False
            problems_ended = True

        # solution-start：題目開啟中遇 solution heading → 後續收進 current['solution']（heading 本身丟棄）
        if (solution_start_re is not None and current is not None and lvl == heading_level and t == 'text'
                and solution_start_re.match(text)):
            in_solution = True
            continue

        # heading：close 當前題目，heading 一定屬 chapter body
        if t == 'text':
            h = detect_heading(text, lvl, section_re, subsection_re, example_re, heading_level)
            if h:
                if current:
                    problems.append(current)
                    current = None
                    in_solution = False
                if h['t'] in ('section', 'subsection') and h.get('id'):
                    current_section_id = h['id']
                body.append(h)
                continue

        # 偵測新題起點（problems_end 後不再開新題）
        if not problems_ended and t in ('text', 'list') and text:
            m = problem_start_re.match(text)
            if m:
                raw_num = m.group(1)
                if problem_chapter_must_match:
                    try:
                        if int(raw_num.split('.')[0]) != ch_num:
                            m = None
                    except ValueError:
                        m = None
                # 遞增守則（非 namespace 模式）：題號回退視為正文 numbered list 偽命中
                if m and not namespace_by_section and max_num_seen > 0:
                    try:
                        if int(raw_num) <= max_num_seen:
                            m = None
                    except ValueError:
                        pass
                if m:
                    if current:
                        problems.append(current)
                    tail = text[m.end():].strip()
                    num = f'{current_section_id}.{raw_num}' if namespace_by_section else raw_num
                    current = {'num': num, 'body': []}
                    in_solution = False
                    if not namespace_by_section:
                        try:
                            max_num_seen = max(max_num_seen, int(raw_num))
                        except ValueError:
                            pass
                    if tail:
                        current['body'].append({'t': 'p', 'md': tail})
                    continue

        struct = block_to_struct(b, label_re, ignore_image_content, ignore_chart_content)
        if struct is None:
            continue
        if current is not None:
            (current.setdefault('solution', []) if in_solution else current['body']).append(struct)
        else:
            body.append(struct)

    if current:
        problems.append(current)
    return body, problems


def parse_chapter(ch: dict, all_blocks: list[dict], rules: dict,
                  regexes: dict[str, re.Pattern]) -> dict:
    filter_types = set(rules.get('filter_types', []))
    title_idx = ch['chapter_title_block_idx']
    sec_title_idx = ch.get('chapter_title_block_idx_secondary')
    problems_idx = ch.get('problems_block_idx')
    next_ch_idx = ch['next_chapter_block_idx']

    body_start = title_idx + 1
    if sec_title_idx is not None:
        body_start = max(body_start, sec_title_idx + 1)

    # inline 模式：rules.inline_problems=True 且本章 problems_block_idx is null
    # → 整章單流水，題目跟正文交錯切（Griffiths ed4/qm3 ch12 等）
    if rules.get('inline_problems') and problems_idx is None:
        ch_blocks = filter_noise(all_blocks[body_start:next_ch_idx], filter_types)
        body, problems = walk_inline_chapter(
            ch_blocks, rules, ch['num'],
            label_re=regexes['eq_label'],
            problem_start_re=regexes['problem_start'],
            section_re=regexes['section'],
            subsection_re=regexes['subsection'],
            example_re=regexes['example'],
            problem_chapter_must_match=rules.get('problem_chapter_must_match', True),
            namespace_by_section=rules.get('problem_num_namespace_by_section', False),
            problems_end_re=regexes['problems_end'],
            solution_start_re=regexes['solution_start'],
        )
        return {'num': ch['num'], 'title': ch['title'], 'body': body, 'problems': problems}

    # 二分模式：有獨立 Problems heading（sakurai/kittel/cheng/blundell 等）
    if problems_idx is not None:
        body_end = problems_idx
        problems_start = problems_idx + 1
        problems_end = next_ch_idx
    else:
        body_end = next_ch_idx
        problems_start = problems_end = next_ch_idx

    body_blocks = filter_noise(all_blocks[body_start:body_end], filter_types)
    problem_blocks = filter_noise(all_blocks[problems_start:problems_end], filter_types)

    body = build_body(
        body_blocks, rules,
        label_re=regexes['eq_label'],
        section_re=regexes['section'],
        subsection_re=regexes['subsection'],
        example_re=regexes['example'],
        figure_caption_merge=rules.get('figure_caption_merge', False),
        figure_caption_main_re=regexes['fig_main_caption'],
    )

    problems = split_problems(
        problem_blocks, rules, ch_num=ch['num'],
        label_re=regexes['eq_label'],
        problem_start_re=regexes['problem_start'],
        problem_chapter_must_match=rules.get('problem_chapter_must_match', True),
        section_re=regexes['section'],
        subsection_re=regexes['subsection'],
        problems_end_re=regexes['problems_end'],
        solution_start_re=regexes['solution_start'],
    )

    return {'num': ch['num'], 'title': ch['title'], 'body': body, 'problems': problems}


def parse_appendix(app: dict, next_start_idx: int, all_blocks: list[dict],
                   rules: dict, regexes: dict[str, re.Pattern]) -> dict:
    filter_types = set(rules.get('filter_types', []))
    title_idx = app['chapter_title_block_idx']
    body_start = title_idx + 1
    body_end = next_start_idx
    body_blocks = filter_noise(all_blocks[body_start:body_end], filter_types)
    body = build_body(
        body_blocks, rules,
        label_re=regexes['eq_label'],
        section_re=regexes['section'],
        subsection_re=regexes['subsection'],
        example_re=regexes['example'],
        figure_caption_merge=rules.get('figure_caption_merge', False),
        figure_caption_main_re=regexes['fig_main_caption'],
    )
    return {'id': app['id'], 'title': app['title'], 'body': body, 'problems': []}


# ── 主流程 ────────────────────────────────────────────────────────────────────

def parse_book(slug: str) -> dict:
    rules = load_rules(slug)
    all_blocks = load_unified(slug)
    regexes = compile_regexes(rules)

    out_dir = DATA_DIR / slug / 'parsed'
    out_dir.mkdir(parents=True, exist_ok=True)

    # 章節
    chapter_files: list[dict] = []
    chapters_rules = rules.get('chapters', [])
    for ch in chapters_rules:
        data = parse_chapter(ch, all_blocks, rules, regexes)
        fname = f"ch{ch['num']:02d}.json"
        assign_catalog_ids(data, fname.removesuffix('.json'))
        normalize_chunk_math(data)
        (out_dir / fname).write_text(json.dumps(data, ensure_ascii=False, indent=2))
        chapter_files.append({
            'num': ch['num'],
            'title': ch['title'],
            'file': fname,
            'body_count': len(data['body']),
            'problem_count': len(data['problems']),
        })

    # 附錄
    appendix_files: list[dict] = []
    appendices = rules.get('appendices', [])
    bib_start = rules.get('bibliography_start_page')
    index_start = rules.get('index_start_page')
    # 附錄末尾用下一個附錄起點 / 或 bibliography_start_page 對應的 block 起點
    for i, app in enumerate(appendices):
        if i + 1 < len(appendices):
            next_idx = appendices[i + 1]['chapter_title_block_idx']
        else:
            # 最後一個附錄：用 bibliography 起點或 index 起點（取較小）
            # bib 通常在 index 之前；bib 不存在時用 index 切掉 Index 區避免被吞進 appendix.body
            cutoff = bib_start if bib_start is not None else index_start
            next_idx = first_block_idx_after_page(all_blocks, cutoff) if cutoff is not None else len(all_blocks)
        data = parse_appendix(app, next_idx, all_blocks, rules, regexes)
        fname = f"app{app['id']}.json"
        assign_catalog_ids(data, fname.removesuffix('.json'))
        normalize_chunk_math(data)
        (out_dir / fname).write_text(json.dumps(data, ensure_ascii=False, indent=2))
        appendix_files.append({
            'id': app['id'],
            'title': app['title'],
            'file': fname,
            'body_count': len(data['body']),
        })

    # 缺題報告 → _gaps.md
    missing = check_problem_gaps(slug, chapter_files, out_dir, rules)
    write_gaps_report(out_dir, slug, missing)

    # book.json
    book = {
        'slug': slug,
        'title': rules['title'],
        'author': rules.get('author'),
        'edition': rules.get('edition'),
        'subject': rules.get('subject'),
        'publisher': rules.get('publisher'),
        'language': rules.get('language', 'en'),
        'chapters': chapter_files,
        'appendices': appendix_files,
    }
    (out_dir / 'book.json').write_text(json.dumps(book, ensure_ascii=False, indent=2))

    # 產圖/表/式子目錄
    build_catalogs.build_catalogs(slug)

    return {
        'book': book,
        'missing_warnings': missing,
    }


def first_block_idx_after_page(blocks: list[dict], page_idx: int) -> int:
    for i, b in enumerate(blocks):
        if b.get('page_idx', 0) >= page_idx:
            return i
    return len(blocks)


def check_problem_gaps(slug: str, chapter_files: list[dict], out_dir: Path,
                       rules: dict) -> list[dict]:
    """掃每章 problems 編號是否連續；非預期 gap 報告，已知 gap 標 expected。"""
    known: dict[int, set[str]] = {}
    for kp in rules.get('known_missing_problems', []) or []:
        known[kp['chapter']] = set(kp.get('nums', []))

    warnings: list[dict] = []
    for cf in chapter_files:
        ch_num = cf['num']
        data = json.loads((out_dir / cf['file']).read_text())
        nums = [p['num'] for p in data['problems']]
        if not nums:
            continue
        # 預期：1 ~ max（題號可能含點 N.M 取末段，或純整數取自身）
        try:
            seconds = sorted({int(n.split('.')[-1]) for n in nums})
            max_n = max(seconds)
            expected_set = set(range(1, max_n + 1))
            got_set = set(seconds)
            gap = sorted(expected_set - got_set)
            gap_strs = [f'{ch_num}.{g}' for g in gap]
            known_strs = known.get(ch_num, set())
            unexpected = [g for g in gap_strs if g not in known_strs]
            covered_known = [g for g in gap_strs if g in known_strs]
            if unexpected or covered_known:
                warnings.append({
                    'chapter': ch_num,
                    'unexpected_missing': unexpected,
                    'known_missing': covered_known,
                    'count_present': len(nums),
                    'expected_range': f'{ch_num}.1–{ch_num}.{max_n}',
                })
        except (ValueError, IndexError):
            continue
    return warnings


def write_gaps_report(out_dir: Path, slug: str, warnings: list[dict]) -> None:
    """把題號 gap 寫成 _gaps.md。known_missing 與 unexpected 分區呈現。"""
    if not warnings:
        gaps_path = out_dir / '_gaps.md'
        gaps_path.write_text(f'# {slug} 題號 gap 報告\n\n無 gap。\n')
        return
    lines = [f'# {slug} 題號 gap 報告', '']
    lines.append('每章 problems[] 編號連續性檢查。`known_missing` 來自 `extract_rules.yaml`')
    lines.append('（audit 顯式確認），`probable_ocr_miss` 是 parser 跑出來的候選 — 多為')
    lines.append('MinerU 把該題吃成空 list/失蹤的痕跡。若 probable 看起來不對勁，')
    lines.append('回頭檢查 `unified/content_list.json` 該範圍 page_idx。')
    lines.append('')
    for w in warnings:
        lines.append(f"## Chapter {w['chapter']}")
        lines.append(f"- 範圍：{w['expected_range']}")
        lines.append(f"- 解析得：{w['count_present']} 題")
        if w['known_missing']:
            lines.append(f"- known_missing（已在 rules.yaml 列）：{w['known_missing']}")
        if w['unexpected_missing']:
            lines.append(f"- **probable_ocr_miss**：{w['unexpected_missing']}")
        lines.append('')
    (out_dir / '_gaps.md').write_text('\n'.join(lines))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('slug')
    args = ap.parse_args()

    result = parse_book(args.slug)
    book = result['book']

    print(f"[ok] {args.slug}：{book['title']}")
    print(f"  章節：{len(book['chapters'])}, 附錄：{len(book['appendices'])}")
    for cf in book['chapters']:
        print(f"    ch{cf['num']:02d} {cf['title'][:40]:40s}  "
              f"body={cf['body_count']:4d}  problems={cf['problem_count']:3d}")
    for af in book['appendices']:
        print(f"    app{af['id']}  {af['title'][:40]:40s}  body={af['body_count']:4d}")

    warns = result['missing_warnings']
    if warns:
        total_known = sum(len(w['known_missing']) for w in warns)
        total_probable = sum(len(w['unexpected_missing']) for w in warns)
        print(f'\n題號 gap：known={total_known}, probable_ocr_miss={total_probable} '
              f'→ 詳見 parsed/_gaps.md')


if __name__ == '__main__':
    main()
