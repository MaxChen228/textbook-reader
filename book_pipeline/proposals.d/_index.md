# 建議佇列（proposals）— 由 JSON store 自動生成，請勿手改

正本 = `book_pipeline/proposals.d/<id>.json`（一案一檔）。新增/改狀態一律走 CLI：
`uv run python -m book_pipeline.proposals {propose|resolve|list|check|gate}`。
決策樹/閘/生命週期（owner 知識）正本：`book_pipeline/proposals.py` 模組 docstring。

## domain: crawl  （1 條；proposed=1）

### P-2026-06-18-cohen-tannoudji-qm-2nd-ed — cohen_tannoudji_qm 在 2nd ed 下指涉不清
- proposed | type=booklist-fix | source=crawl
- 證據：slug=cohen_tannoudji_qm, title=Quantum Mechanics, edition_pref=2nd；z-lib 命中 122132670《Quantum Mechanics, Volume 1: Basic Concepts, Tools, and Applications, Second Edition》與 6061115《Quantum Mechanics 1-3》；同書單另有 cohen_tannoudji_qm_vol2。
- 提議：把 cohen_tannoudji_qm 明確改成 Volume 1（或改成新的 vol1 slug），避免與 2nd ed 三卷本/全套合集混淆。
- 風險：若維持現狀，crawl agent 可能把同一 canonical 書誤落到 vol1 或 1-3 合集，造成 SoT 與實際 PDF 不一致。

## domain: engine  （1 條；proposed=1）

### P-2026-06-18-young-freedman-university-physic — build_catalogs 離散圖說(detached caption)回收能力 — young_freedman audit worker 越界版
- proposed | type=tooling-gap | source=scope_guard-retroactive
- 證據：young_freedman audit worker (session 20260617T221215Z, 57min/236ev) 撞到「圖說是獨立 text block 緊鄰 image、非 image 內」，build_catalogs 抓不到 → smoke H6/H7 fail。worker 擅改 build_catalogs.py 約70行(FIG_BARE_CAPTION_RE/SUBFIG_PARENT_CAPTION_RE/_find_nearby_visual_anchor)讓自己過。但它看不到跨模組不變式：此改動打破 test_catalog_id_parity（corpus 衍生 fig-1.2--1 vs build_catalogs fig-1.2 → reader 點目錄跳不到）。
- 提議：idea 本身合理（離散 caption 回收是真缺口），但須由架構師正式重做：與 corpus 的 anchor id 衍生保持 parity（test_catalog_id_parity 當閘）、且 bare-caption regex 要夠嚴避免把章首 "1.1 What a physical theory is" 摘要誤當圖說。worker 原始 patch 全文如下：

diff --git a/book_pipeline/build_catalogs.py b/book_pipeline/build_catalogs.py
index 764d7c2..701078a 100644
--- a/book_pipeline/build_catalogs.py
+++ b/book_pipeline/build_catalogs.py
@@ -34,6 +34,9 @@ FIG_NUM_RE = re.compile(rf'Fig(?:ure|\.)?\s*({CAT_NUM_PATTERN})', re.IGNORECASE)
 TBL_NUM_RE = re.compile(rf'(?:Table|Tab\.)\s*({CAT_NUM_PATTERN})', re.IGNORECASE)
 FIG_CAPTION_RE = re.compile(rf'^\s*(?:Fig(?:ure|\.)?)\s*({CAT_NUM_PATTERN})\s*[.:：-]?\s*(.*)', re.IGNORECASE | re.DOTALL)
 TBL_CAPTION_RE = re.compile(rf'^\s*(?:Table|Tab\.?)\s*({CAT_NUM_PATTERN})\s*[.:：-]?\s*(.*)', re.IGNORECASE | re.DOTALL)
+FIG_BARE_CAPTION_RE = re.compile(rf'^\s*({CAT_NUM_PATTERN})\s+(?![•])(.+)$', re.DOTALL)
+CAT_NUM_ONLY_RE = re.compile(rf'^\s*({CAT_NUM_PATTERN})\s*$')
+SUBFIG_PARENT_CAPTION_RE = re.compile(rf'^\s*\([a-z]\)\s+.*?\b({CAT_NUM_PATTERN})\s+(.+)$', re.IGNORECASE | re.DOTALL)
 FALLBACK_ID_RE = re.compile(r'^(?:fig|tbl|eq)-(?:ch\d{2}|app[^-]+)(?:-|$)')
 EQ_TAG_RE = re.compile(r'\\tag\s*\{([^}]+)\}')
 
@@ -88,7 +91,7 @@ def _canonical_catalog_id(raw_id: str) -> str:
     return raw_id
 
 
-def _caption_labels(caption: str) -> list[tuple[str, str, str, str]]:
+def _caption_labels(caption: str, default_type: str | None = None) -> list[tuple[str, str, str, str]]:
     """Extract every formal Figure/Table label from one caption.
 
     Returns (type, id_prefix, canonical_num, display_caption).  A single
@@ -106,6 +109,23 @@ def _caption_labels(caption: str) -> list[tuple[str, str, str, str]]:
             num = re.sub(r'[\-–—]', '.', m.group(1))
             matches.append((m.start(), m.end(), typ, prefix, label, num))
     matches.sort(key=lambda item: item[0])
+    if default_type in {'figure', 'table'}:
+        m = FIG_BARE_CAPTION_RE.match(text)
+        if m:
+            num = re.sub(r'[\-–—]', '.', m.group(1).strip())
+            tail = _plain_text(m.group(2))
+            label = 'Figure' if default_type == 'figure' else 'Table'
+            prefix = 'fig' if default_type == 'figure' else 'tbl'
+            display = f'{label} {num}: {tail}' if tail else f'{label} {num}'
+            if not matches or matches[0][0] > len(text) - len(text.lstrip()):
+                return [(default_type, prefix, num, display)]
+        if default_type == 'figure':
+            m = SUBFIG_PARENT_CAPTION_RE.match(text)
+            if m:
+                num = re.sub(r'[\-–—]', '.', m.group(1).strip())
+                tail = _plain_text(m.group(2))
+                display = f'Figure {num}: {tail}' if tail else f'Figure {num}'
+                return [('figure', 'fig', num, display)]
     if not matches:
         return []
     leading_offset = len(text) - len(text.lstrip())
@@ -134,7 +154,7 @@ def _plain_text(value: str) -> str:
     return re.sub(r'\s+', ' ', value).strip()
 
 
-def _leading_caption(block: dict) -> tuple[str, str, str] | None:
+def _leading_caption(block: dict, source: str) -> tuple[str, str, str] | None:
     text = block.get('md') or block.get('text') or ''
     if not isinstance(text, str):
         return None
@@ -150,11 +170,70 @@ def _leading_caption(block: dict) -> tuple[str, str, str] | None:
             continue
         caption = _plain_text(m.group(2)) or f'{label} {num}'
         return kind, f'{prefix}-{num}', f'{label} {num}: {caption}'
+    if source == 'body':
+        m = FIG_BARE_CAPTION_RE.match(text)
+        if m:
+            num = re.sub(r'[\-–—]', '.', m.group(1).strip())
+            caption = _plain_text(m.group(2))
+            return 'figure', f'fig-{num}', f'Figure {num}: {caption}' if caption else f'Figure {num}'
+        m = CAT_NUM_ONLY_RE.match(text)
+        if m:
+            num = re.sub(r'[\-–—]', '.', m.group(1).strip())
+            return 'figure', f'fig-{num}', f'Figure {num}'
+    return None
+
+
+def _find_nearby_visual_anchor(
+    blocks: list[dict],
+    start_idx: int,
+    expected_type: str,
+    ch_label: str,
+    source: str,
+    window: int = 12,
+) -> tuple[int, str, str, str | None, str | None] | None:
+    """Link detached caption paragraphs to the nearby visual they describe.
+
+    We only materialize bare/formal caption paragraphs when a matching visual
+    block appears shortly after them; otherwise numeric prose like
+    "1.1 What a physical theory is" would be misclassified as a figure caption.
+    """
+    target_t = 'fig' if expected_type == 'figure' else 'table'
+    stop_types = {'section', 'example'}
+    upper = min(len(blocks), start_idx + window + 1)
+    for idx in range(start_idx + 1, upper):
+        block = blocks[idx]
+        t = block.get('t')
+        if t in stop_types:
+            break
+        if t != target_t:
+            continue
+        return (
+            idx,
+            _anchor_id(t, block, ch_label, source, idx),
+            block.get('src', ''),
+            block.get('kind'),
+            block.get('aspect'),
+        )
+    lower = max(-1, start_idx - window - 1)
+    for idx in range(start_idx - 1, lower, -1):
+        block = blocks[idx]
+        t = block.get('t')
+        if t in stop_types:
+            break
+        if t != target_t:
+            continue
+        return (
+            idx,
+            _anchor_id(t, block, ch_label, source, idx),
+            block.get('src', ''),
+            block.get('kind'),
+            block.get('aspect'),
+        )
     return None
 
 
 def _visual_semantic(t: str, block: dict) -> tuple[str, str, str | None]:
-    labels = _caption_labels(block.get('caption', ''))
+    labels = _caption_labels(block.get('caption', ''), default_type='figure' if t == 'fig' else 'table')
     if labels:
         typ, prefix, num, _caption = labels[0]
         return typ, prefix, num
@@ -195,7 +274,10 @@ def _anchor_id(t: str, block: dict, ch_label: str, source: str, idx: int) -> str
 
 def _semantic_id(t: str, block: dict) -> str | None:
     """回傳 catalog 語義 id；無可驗證語義時回 None，不產生 fallback。"""
-    labels = _caption_labels(block.get('caption', '')) if t in {'fig', 'table'} else []
+    labels = _caption_labels(
+        block.get('caption', ''),
+        default_type='figure' if t == 'fig' else 'table',
+    ) if t in {'fig', 'table'} else []
     if block.get('catalog_exclude_reason') and not labels:
         return None
     raw_id = (block.get('id') or '').strip()
@@ -244,9 +326,13 @@ def _walk_blocks(blocks: list[dict], section_stack: list[str], ch_label: str,
         sec_id = section_stack[-1] if section_stack else None
 
         if t == 'p':
-            leading = _leading_caption(b)
+            leading = _leading_caption(b, source)
             if leading:
                 typ, entry_id, caption = leading
+                linked_visual = _find_nearby_visual_anchor(blocks, idx, typ, ch_label, source)
+                if not linked_visual:
+                    continue
+                _visual_idx, anchor, src, kind, aspect = linked_visual
                 entries.append({
                     'id': entry_id,
                     'type': typ,
@@ -254,17 +340,18 @@ def _walk_blocks(blocks: list[dict], section_stack: list[str], ch_label: str,
                     'problem': problem_num,
                     'source': source,
                     'caption': caption,
-                    'src': '',
-                    'kind': 'text',
-                    'anchor': (b.get('id') or '').strip() or entry_id,
+                    'src': src,
+                    'kind': 'text' if typ == 'figure' else kind,
+                    'aspect': aspect,
+                    'anchor': anchor,
                 })
             continue
 
         if t == 'fig':
-            labels = _caption_labels(b.get('caption', ''))
+            labels = _caption_labels(b.get('caption', ''), default_type='figure')
             typ, _prefix, _num = _visual_semantic('fig', b)
             anchor = _anchor_id('fig', b, ch_label, source, idx)
-            exclude_reason = None if labels else b.get('catalog_exclude_reason')
+            exclude_reason = None if labels else (b.get('catalog_exclude_reason') or 'unlabeled_visual')
             entries.append({
                 'id': _semantic_id('fig', b),
                 'type': typ,
@@ -300,10 +387,14 @@ def _walk_blocks(blocks: list[dict], section_stack: list[str], ch_label: str,
                 entries.append(alias)
 
         elif t == 'table':
-            labels = _caption_labels(b.get('caption', ''))
+            labels = _caption_labels(b.get('caption', ''), default_type='table')
             typ, _prefix, _num = _visual_semantic('table', b)
             anchor = _anchor_id('table', b, ch_label, source, idx)
-            exclude_reason = None if labels else b.get('catalog_exclude_reason')
+            exclude_reason = None
+            if not labels:
+                exclude_reason = b.get('catalog_exclude_reason')
+                if not exclude_reason and not _plain_text(b.get('caption', '')):
+                    exclude_reason = 'unlabeled_table'
             entries.append({
                 'id': _semantic_id('table', b),
                 'type': typ,
@@ -381,15 +472,6 @@ def _scan_chunk(slug: str, stem: str) -> list[dict]:
         e['chunk_kind'] = chunk_kind
         e['chunk_key'] = chunk_key
 
-    # anchor 是 chunk 內 DOM id；只在同一章/附錄內需要唯一。
-    seen: dict[str, int] = {}
-    for e in entries:
-        key = e['anchor']
-        if key in seen and not e.get('catalog_alias'):
-            seen[key] += 1
-            e['anchor'] = f'{key}--{seen[key]}'
-        elif key not in seen:
-            seen[key] = 0
     return entries
- 風險：原樣會破 reader 目錄導航 + fail parity test → 已還原。idea 待架構師重做（parity-safe 版）。

## domain: math  （8 條；proposed=4）

### P-2026-06-17-collapse-mathtype-slash-phantom- — Collapse MathType slash phantom/kern residue to /
- proposed | type=normalize-rule | source=math_sweep | 偵測=\\kern,\\vphantom,\\mathord,\\left/
- 證據：cluster other occ=4 in dummit_foote_algebra plus token_signals: \\kern occ=21 / 10 books, \\vphantom occ=20 / 9 books; representative samples from dummit_foote_algebra, boas_mp, griffiths_qm3, rudin_analysis, srednicki_qft
- 提議：Replace exact MathType slash residue \\mathord{\\left/ {\\vphantom{...}} \\right. \\kern - delimiterspace} (and equivalent \\mathbin form) with literal /
- 風險：Could collapse non-slash delimiter constructs if pattern too broad; keep match exact on left/phantom/right./kern sequence and rely on full-corpus gate for collateral

### P-2026-06-17-collapse-underlined-angle-ocr-re — Collapse underlined angle OCR residue
- proposed | type=normalize-rule | source=math_sweep | 偵測=\\underline + \\left/
- 證據：clustered underlined-angle residue in alexander_circuits and ogata_control; 22 residual occurrences across 2 books; representative tex=\\underline{{\\left/ 0 ^ {\\circ} \\left. \\right.}}
- 提議：R7 _collapse_underlined_angle: \\underline{{\\left/ ... \\left. \\right.}} -> \\underline{\\angle ...}
- 風險：could misread legitimate underlined slash constructs; matcher constrained to \\underline + \\left/ + \\left. + \\right. and excludes vphantom/delimiterspace forms

### P-2026-06-17-nu-n-ocr-pseudo-macro-collapse — \Nu → N OCR pseudo-macro collapse
- proposed | type=normalize-rule | source=math_sweep | 偵測=\Nu
- 證據：cluster undefined_macro occ=53 / 7 books; sampled all usages are letter N: N_2 in atkins/lindner/thijssen, integer N in rudin/goldstein, Gauss map N in do_carmo, norm N_{K/F} in dummit
- 提議：Layer 1 normalize: replace exact control sequence \\Nu with literal N
- 風險：Pseudo-macro collapse is safe only if corpus-wide usage is consistently Latin N; full-corpus gate must verify no collateral

### P-2026-06-17-strip-stray-display-delimiters-i — strip stray display delimiters inside math payload
- proposed | type=normalize-rule | source=math_sweep | 偵測=\] \[ \( \)
- 證據：cluster: \] occ=3 books=3; \( occ=3 books=1; all are in already-math payloads where mode delimiters become undefined residuals
- 提議：Layer 1 normalize: in normalize_tex, delete stray \\[ and \\] tokens; collapse stray \\( and \\) to literal parentheses inside math payload
- 風險：May alter literal delimiter text shown inside code-like math text; rely on corpus gate and override collateral if any

### P-2026-06-17-bgroup — \bgroup / \aftergroup / \egroup 群組噪訊收斂
- accepted | type=normalize-rule | source=math_sweep | 偵測=\bgroup \egroup \aftergroup
- 決議：R5 _remove_group_noise
- 證據：\mathopen{}\mathclose\bgroup … \aftergroup\egroup 成對噪訊；alexander_circuits/axler_linalg/dummit_foote_algebra/hatcher_algebraic_topology/rudin_analysis/schwartz_qft。×19 occ。
- 提議：Layer 1 normalize 移除成對 \mathopen{}\mathclose\bgroup / \aftergroup\egroup / 殘留 \mathclose\bgroup / 裸 token。
- 風險：低；這些 token 在 MathJax 全 undefined → 凡含者本就 fail，移除只能 fail→pass，回歸閘天然安全。

### P-2026-06-17-ifmmode — \ifmmode 條件乘號展開
- accepted | type=normalize-rule | source=math_sweep | 偵測=\ifmmode
- 決議：R4 _fix_cond_times
- 證據：SU(2) \ifmmode \times \else \texttimes \fi { } …；MathJax 報 Undefined control sequence \ifmmode；出現在 schwartz_qft、srednicki_qft。×17 occ / 2 書。
- 提議：Layer 1 normalize 規則：\ifmmode \times \else \texttimes \fi → \times。
- 風險：低；reader 一律數學區 → 恆等於 \times。全 corpus 回歸確認無誤吞。

### P-2026-06-17-mua — \muA 單位巨集
- rejected | type=macro | source=math_sweep | 偵測=\muA
- 決議：already-resolved single-book
- 處置：已由 math_overrides/sedra_microe.json 5 條 override 清零（bad_occ=0），macro 冗餘
- 證據：I_B = 0.1 \, \muA；僅 sedra_microe。×7 occ / 1 書。
- 提議：原提案 Layer 0 macro \muA→\mu\text{A}。
- 風險：\muA 在禁收清單；只此 1 本無泛化價值，已由 sedra_microe.json override 清零。

### P-2026-06-17-nu — \Nu 映射
- rejected | type=macro | source=math_sweep | 偵測=\Nu
- 決議：pseudo-macro-guard semantically-ambiguous
- 處置：per-slug override（觀測語境全為大寫 N：高斯映射 N、Rudin 自然數界 N、Dummit 範數 N_{K/F}）
- 證據：\Nu \colon S \to \mathbb{R}^3（高斯映射 N）、\Nu \geq \Nu_0、\Nu_{K/F}(\alpha)（範數 N）；6 書。×20 occ。
- 提議：原提案 Layer 0 macro \Nu→\nu。
- 風險：\Nu 在 test_no_ocr_glue_pseudomacros 禁收清單；且語意非唯一——\Nu→\nu 對所有觀測樣本皆錯（實為大寫 N）。
