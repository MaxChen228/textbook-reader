"""artifact-guard：純靜態掃**真實 committed** LLM 產物，把結構漂移擋在 deploy 前。

跑：uv run python -m book_pipeline.test_artifacts_committed
（deploy.sh 靠未捕捉 AssertionError → 非零退出把關；此檔是 commit↔deploy 之間唯一
 不需要真實 parsed/content_list 在場就能跑的全 corpus 契約閘。）

掃的四道契約邊界（皆「LLM agent 產出 → 確定性碼消費」）：
  1. 89× extract_rules.yaml   → validate_rules.validate（parser 消費，regex group/章欄漂移＝靜默損毀）
  2. 6×  sol_rules.yaml        → sol_extract.load_sol_rules（用 sys.exit 硬退＝整 tick 連坐被殺）
  3. 35× catalog_overrides/*   → apply_catalog_overrides 的輸入契約（consumer 只在真實 parsed
                                 在場才驗 → 壞 override 可長躺 git，本檔是 deploy 前唯一防線）
  4. 366× *.zh.json overlay    → corpus._patch_blocks 的輸入契約（anchor 是防中譯錯置命脈）

────────────────────────────────────────────────────────────────────────────
已知真實 drift（3 本，extract_rules 契約）— **不是測試 bug，是待修的 committed 真雷**：
  - arnold_ode    : heading_text_level=[2, null] → null 非 ≥1 整數
                    （parser 用它選 section heading 的 MinerU text_level；null 會讓判斷崩）
  - ogata_control : section_re 3 capture group（須 2）、problem_start_re 2 group（須 1）
                    （parser 用 m.group(2) 取章節標題、m.group(1) 取題號；多/少 group →
                     取到編號數字當「標題」、或 group(1) 抓錯欄 → 靜默損毀切章/切題）
  - schwartz_qft  : 第一章缺 problems_block_idx（章內題目整片定位不到 → 題目丟失）
這 3 本以 KNOWN_RULES_DRIFT allowlist 標記：測試**仍實跑並斷言它們確實 RED**（守住
「漂移被偵測到」這條 invariant），只是不讓它們把整檔拖紅。任一本被修好（變綠）會觸發
allowlist 過期斷言，提醒把它移出 allowlist —— 絕不靜默放行。
────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import glob
import io
import json
import re
import shutil
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from book_pipeline import sol_extract as SE
from book_pipeline import validate_rules as VR
from textbooks import corpus

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'book_pipeline' / 'mineru_data'
OVERRIDE_DIR = ROOT / 'book_pipeline' / 'catalog_overrides'

# extract_rules 契約：這 3 本是 committed 真 drift（見檔頭）。測試斷言它們「確實仍 RED」，
# 修好任一本須把它移出此集合（過期斷言會提醒）。絕不當「正確」放行。
#
# 值 = 該本必須出現的失敗訊息**特徵子串**。光看「rc!=0」不夠：若某本的文件記載 bug 被修好、
# 卻同時冒出另一條無關 drift，rc 仍 !=0 → 測試會誤以為「已知 drift 仍在」而靜默放行，
# 既漏報「文件 bug 已修（該移出 allowlist）」、也漏報新 drift。故釘住失敗**理由**，
# 強制 allowlist 永遠對應到「正是那條已知病灶」。
KNOWN_RULES_DRIFT = {
    'arnold_ode':    'heading_text_level',                 # =[2, None]：null 非 ≥1 整數
    'ogata_control': 'capture group',                      # section_re 3 group／problem_start_re 2 group
    'schwartz_qft':  'chapter[0] 缺 problems_block_idx',   # 首章缺 pbi → 題目整片定位不到
}


# ── validate_rules 的 stub 隔離：把 content_list 缺失/內容變異隔離，只留純 schema 契約 ──
#
# validate() 內部 `B = load_unified(slug)` 讀 unified/content_list.json 算 N=len(B)，再拿
# chapter 的 cti/pbi/nci 與 [0,N) 比界。worktree 沒 content_list（機器產物 gitignore），
# 直跑會全本 sys.exit。對策：monkeypatch validate_rules.load_unified（它 `from
# book_pipeline.parser import load_unified` 已綁進**本命名空間**，故 patch VR.load_unified
# 才有效）回傳「夠長且內容空白」的合成 block list：
#   - 夠長（max_ref_idx+2）→ 所有 idx 比界都過 → idx-bound 檢查不會誤紅
#   - 內容空白（text=''）→ inline_problems 一致性檢查掃 B[j] 時 ps_re.match('') 恆不命中
#     → 不會誤觸「pbi=null 章內有題」假陽性
# 殘下被檢的純是「不讀 content_list 內容、只讀長度」的 schema：regex group 數、REQUIRED
# key、heading_text_level 型別、章號連續性 …… 正是 LLM 最會漂移、parser 會踩的那些。

def _max_ref_idx(R: dict) -> int:
    """rules 內引用到的最大 block idx（chapter cti/pbi/nci + appendix cti）。合成 block list
    長度取 max+2 即可讓所有 idx-bound 檢查通過，把焦點留給純 schema 違規。"""
    m = 0
    for c in (R.get('chapters') or []):
        for k in ('chapter_title_block_idx', 'problems_block_idx', 'next_chapter_block_idx'):
            v = c.get(k)
            if isinstance(v, int):
                m = max(m, v)
    for a in (R.get('appendices') or []):
        v = a.get('chapter_title_block_idx')
        if isinstance(v, int):
            m = max(m, v)
    return m


def _synth_blocks(n: int) -> list[dict]:
    # type/text/text_level 三鍵齊備：inline_problems 檢查讀這三鍵；text='' 確保不誤命中題目。
    return [{'type': 'text', 'text': '', 'text_level': None} for _ in range(n)]


def _validate_with_stub(slug: str, rules: dict, data_dir: Path | None = None) -> tuple[int, str]:
    """以 stub 隔離跑 validate(slug)，回 (rc, 收集到的訊息)。

    data_dir 預設 = 真實 DATA_DIR（讀 committed extract_rules）；self-check 時改指 tmp。
    finally 一定還原 load_unified / DATA_DIR，絕不污染後續測試或真實狀態。"""
    n = _max_ref_idx(rules) + 2 if isinstance(rules, dict) else 2
    orig_load, orig_dir = VR.load_unified, VR.DATA_DIR
    VR.load_unified = lambda _s, _n=n: _synth_blocks(_n)
    if data_dir is not None:
        VR.DATA_DIR = data_dir
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = VR.validate(slug)
    finally:
        VR.load_unified, VR.DATA_DIR = orig_load, orig_dir
    return rc, buf.getvalue()


# ════════════════════════════════════════════════════════════════════════════
# 1. extract_rules.yaml × 89 → validate_rules.validate
# ════════════════════════════════════════════════════════════════════════════
def test_all_extract_rules_pass_validate():
    """全 89 本 extract_rules 須通過 validate（schema 契約）。已知 3 本 drift 以 allowlist 標記
    且斷言它們**確實仍 RED** —— 守住「漂移被偵測到」，而非假裝它們合格。

    為何高含金量：P(LLM 吐 regex group 漂移／缺章欄)=高 × silent-corruption（parser
    m.group(2) 取到純編號數字當標題、章題整片丟失）。實證已抓到 3 本 committed 真 bug。"""
    paths = sorted(glob.glob(str(DATA_DIR / '*' / 'extract_rules.yaml')))
    assert len(paths) >= 89, f'extract_rules 數量異常（{len(paths)}）— corpus 是否搬動？'

    failures: list[tuple[str, str]] = []   # 非 allowlist 卻紅的（真回歸）
    drift_still_red: set[str] = set()      # allowlist 中確實「以記載理由」仍紅的
    wrong_reason: list[str] = []           # allowlist 本紅了、但理由不是記載那條（病灶已換）
    for p in paths:
        slug = Path(p).parent.name
        R = yaml.safe_load(Path(p).read_text())
        rc, out = _validate_with_stub(slug, R)
        if rc != 0:
            if slug in KNOWN_RULES_DRIFT:
                # 不只「仍紅」——必須仍是**記載的那條病灶**。理由換了＝記載 bug 可能已修、
                # 另冒新 drift，allowlist 不該再無條件吞它。
                if KNOWN_RULES_DRIFT[slug] in out:
                    drift_still_red.add(slug)
                else:
                    wrong_reason.append(
                        f'  [{slug}] 仍 RED 但理由非記載的「{KNOWN_RULES_DRIFT[slug]}」：\n{out.strip()}')
            else:
                failures.append((slug, out.strip()))

    # (a) 非 allowlist 的全綠：任一新紅 = 新 drift 流進 git，須當場攔下並印全文
    assert not failures, '非 allowlist 的 extract_rules 出現 drift：\n' + '\n'.join(
        f'  [{s}]\n{o}' for s, o in failures)

    # (a2) allowlist 本紅了但病灶換了：記載 bug 恐已修，卻被另一條新 drift 撐住「仍紅」假象。
    assert not wrong_reason, ('allowlist 本的失敗理由已偏離記載病灶（記載 bug 恐已修、'
                              '另有新 drift）：\n' + '\n'.join(wrong_reason))

    # (b) allowlist 過期偵測：若某本被修好（以記載理由不再 RED），逼 writer 把它移出 allowlist
    #     —— 不讓 allowlist 變成「永遠通過」的套套邏輯掩蓋已修好的事實。
    healed = set(KNOWN_RULES_DRIFT) - drift_still_red
    assert not healed, (f'這些 allowlist 本已不再以記載理由 drift：{sorted(healed)} → '
                        f'請從 KNOWN_RULES_DRIFT 移除（測試不該繼續假設它們壞）')

    print(f'✓ extract_rules × {len(paths)}：{len(paths) - len(KNOWN_RULES_DRIFT)} 本綠、'
          f'{len(drift_still_red)} 本已知 drift 仍以記載理由被偵測 RED（{sorted(drift_still_red)}）')


# ════════════════════════════════════════════════════════════════════════════
# 2. stub 機制本身的自檢：守門員的守門員
# ════════════════════════════════════════════════════════════════════════════
def test_validate_rules_stub_isolation_self_check():
    """釘住 stub 注入**不會放水**：乾淨 rules → rc 0、壞 group 數 rules → rc 1。

    為何高含金量：若 stub 的合成 block 注入意外讓「所有本都通過」，整個 artifact-guard
    就偽綠失效（med 機率 × high blast）。此自檢確保 group-count／REQUIRED-key 這類純
    schema 檢查（只讀 block list 長度、不讀內容）在 stub 下仍真正生效。"""
    clean_src = DATA_DIR / 'sakurai_mqm3' / 'extract_rules.yaml'
    assert clean_src.is_file(), '基準乾淨本 sakurai_mqm3 不存在'
    clean_rules = yaml.safe_load(clean_src.read_text())

    with tempfile.TemporaryDirectory(prefix='artifact_selfcheck_') as d:
        tmp = Path(d)
        # (a) 乾淨本：原樣寫進 tmp DATA_DIR → 應 rc 0
        (tmp / 'clean').mkdir()
        (tmp / 'clean' / 'extract_rules.yaml').write_text(yaml.safe_dump(clean_rules))
        rc_clean, out_clean = _validate_with_stub('clean', clean_rules, data_dir=tmp)
        assert rc_clean == 0, f'乾淨 rules 在 stub 下竟不過（stub 放水反了）：\n{out_clean}'

        # (b) 壞 group 本：把 problem_start_re 改成 2 個 group（契約要求恰 1）→ 應 rc 1。
        #     這違規**只讀 regex 編譯結果、完全不碰 content_list** → 純驗 stub 沒掩蓋 schema 檢查。
        bad_rules = dict(clean_rules)
        bad_rules['problem_start_re'] = r'^(\d+)\.(\d+)\s+'   # 2 group，原本 1
        (tmp / 'bad').mkdir()
        (tmp / 'bad' / 'extract_rules.yaml').write_text(yaml.safe_dump(bad_rules))
        rc_bad, out_bad = _validate_with_stub('bad', bad_rules, data_dir=tmp)
        assert rc_bad == 1, '壞 group rules 竟通過 → stub 把 schema 檢查放水了'
        assert 'problem_start_re' in out_bad and 'capture group' in out_bad, \
            f'未指出 group 違規（stub 可能誤吞）：\n{out_bad}'

    print('✓ stub 自檢：乾淨本 rc0 / 壞 group 本 rc1（隔離手法不放水）')


# ════════════════════════════════════════════════════════════════════════════
# 3. sol_rules.yaml × 6 → sol_extract.load_sol_rules
# ════════════════════════════════════════════════════════════════════════════
# load_sol_rules 用 sys.exit（非 raise）硬退：_pending 標記、group 數違約都會 SystemExit。
# standby merge 解答時若 group 違約 → 整個 daemon process 被殺、連坐當前 tick。故此檔把
# live 本（須順利載入）與 _pending 本（須被擋）兩種「正當 exit」嚴格區分，避免把後者的
# 「正確跳過」誤當成 group 違約、或反之放掉真正的 group drift。
_PENDING_SOL = {'boas_mp_sol', 'kittel_ssp_sol'}   # 主書品質不足、標 _pending、不該 merge


def test_all_live_sol_rules_load_without_sysexit():
    """4 本 live sol_rules 須無 SystemExit 載入；2 本 _pending 須確實 SystemExit（訊息含 _pending）。

    為何高含金量：load_sol_rules 用 sys.exit 硬退 → sol agent 多包/少包 group 時整個
    daemon tick 被殺（crash 連坐）。此測釘住「live 本載得進、_pending 本被擋、且兩者的
    『正當 exit』不混淆」。

    ⚠ 設計陷阱（避免套套邏輯）：load_sol_rules 自己已用 sys.exit 擋掉 group 違約 →
    對它回傳的 dict 再 assert `cg==1 / pg>=1` 是**死碼**（違約者根本走不到 return）。
    故 group 契約改在**artifact 層**直接編譯 raw yaml 的 regex 來驗（不靠那道 gate），
    才能在「gate 邏輯被誰改鬆/刪掉」時仍抓到 sol 產物的真 group 漂移。gate 本身會不會
    開火則由 test_sol_group_gate_actually_fires 另證。"""
    paths = sorted(glob.glob(str(DATA_DIR / '*_sol' / 'sol_rules.yaml')))
    assert len(paths) >= 6, f'sol_rules 數量異常（{len(paths)}）'

    failures: list[str] = []
    n_live = n_pending = 0
    for p in paths:
        slug = Path(p).parent.name
        raw = yaml.safe_load(Path(p).read_text()) or {}
        if slug in _PENDING_SOL:
            # _pending 本必須被 sys.exit 擋下，且理由含 _pending（而非 group 違約等其他原因）
            try:
                SE.load_sol_rules(slug)
                failures.append(f'{slug}: 標 _pending 卻未被擋（應 SystemExit）')
            except SystemExit as e:
                if '_pending' not in str(e):
                    failures.append(f'{slug}: SystemExit 但非 _pending 原因：{e}')
                else:
                    n_pending += 1
            continue
        # live 本：絕不可 SystemExit；回傳須是編譯好 pattern 的 dict
        try:
            r = SE.load_sol_rules(slug)
        except SystemExit as e:
            failures.append(f'{slug}: live 本竟 SystemExit（daemon 會被殺）：{e}')
            continue
        if not isinstance(r, dict) or 'chapter_re' not in r or 'problem_re' not in r:
            failures.append(f'{slug}: 回傳非預期 dict：{type(r)}')
            continue
        # group 契約：直接編譯 yaml 內的 raw pattern（缺欄則退回 DEFAULTS 對應值），
        # **不**讀 load_sol_rules 已過濾的 r —— 避免「驗它剛把關過的同一件事」套套邏輯。
        chap_src = raw.get('chapter_re', SE.DEFAULTS['chapter_re'])
        prob_src = raw.get('problem_re', SE.DEFAULTS['problem_re'])
        cg = re.compile(chap_src).groups
        pg = re.compile(prob_src).groups
        if cg != 1:
            failures.append(f'{slug}: chapter_re raw 須恰 1 group，得 {cg}（{chap_src!r}）')
        if pg < 1:
            failures.append(f'{slug}: problem_re raw 須 ≥1 group，得 {pg}（{prob_src!r}）')
        # 同時釘住 load_sol_rules 確實把這兩條 raw pattern 編進回傳 dict（型別契約），
        # 但不再對其 .groups 重複斷言（那才是套套邏輯）。
        if not all(hasattr(r[k], 'match') for k in ('chapter_re', 'problem_re')):
            failures.append(f'{slug}: 回傳的 chapter_re/problem_re 非已編譯 pattern')
        n_live += 1

    assert not failures, 'sol_rules 契約違規：\n' + '\n'.join(f'  - {x}' for x in failures)
    assert n_live >= 4, f'live sol 本數異常（{n_live}）'
    assert n_pending == len(_PENDING_SOL), f'_pending 本數異常（{n_pending}）'
    print(f'✓ sol_rules × {len(paths)}：{n_live} live 本無 SystemExit 且 raw group 合契約、'
          f'{n_pending} _pending 本正確被擋')


def test_sol_group_gate_actually_fires():
    """gate 自檢（守門員的守門員）：把一本 group 違約的 sol_rules 餵進 load_sol_rules，
    必須 SystemExit 且理由是 group（非 _pending），證明那道 crash-guard 真會開火。

    為何必要：上測把 group 契約挪到 artifact 層後，『load_sol_rules 真的會擋 group 違約』
    這條 invariant 失去驗證點。若哪天有人把 `chap.groups != 1` 那段 sys.exit 拔掉，
    違約 rules 就會帶 0/2 group 流進 extract_sol_chapters，`m.group(1)` 取錯欄／IndexError
    在 standby merge 時炸整 tick。此測直接釘住 gate 仍在。"""
    orig_dir = SE.DATA_DIR
    with tempfile.TemporaryDirectory(prefix='sol_gate_') as d:
        tmp = Path(d)
        SE.DATA_DIR = tmp
        try:
            # (a) chapter_re 0 group（須恰 1）→ 必 sys.exit，理由含 chapter_re
            (tmp / 'bad_chap_sol').mkdir()
            (tmp / 'bad_chap_sol' / 'sol_rules.yaml').write_text(yaml.safe_dump({
                'chapter_re': r'^Chapter\s+\d+\s*$',          # 0 group（少了括號）
                'problem_re': r'^Problem\s+(\d+\.\d+)',
            }))
            try:
                SE.load_sol_rules('bad_chap_sol')
                assert False, 'chapter_re 0 group 竟未被 gate 擋下（crash-guard 失效）'
            except SystemExit as e:
                assert 'chapter_re' in str(e), f'gate 開火但理由非 chapter_re：{e}'

            # (b) problem_re 0 group（須 ≥1）→ 必 sys.exit，理由含 problem_re
            (tmp / 'bad_prob_sol').mkdir()
            (tmp / 'bad_prob_sol' / 'sol_rules.yaml').write_text(yaml.safe_dump({
                'chapter_re': r'^Chapter\s+(\d+)\s*$',
                'problem_re': r'^Problem\s+\d+\.\d+',          # 0 group
            }))
            try:
                SE.load_sol_rules('bad_prob_sol')
                assert False, 'problem_re 0 group 竟未被 gate 擋下（crash-guard 失效）'
            except SystemExit as e:
                assert 'problem_re' in str(e), f'gate 開火但理由非 problem_re：{e}'

            # (c) 反證：乾淨 rules 在同一 tmp 機制下**不**該 SystemExit（gate 沒過度殺）
            (tmp / 'ok_sol').mkdir()
            (tmp / 'ok_sol' / 'sol_rules.yaml').write_text(yaml.safe_dump({
                'chapter_re': r'^Chapter\s+(\d+)\s*$',
                'problem_re': r'^Problem\s+(\d+\.\d+)',
            }))
            r = SE.load_sol_rules('ok_sol')           # 不該丟 SystemExit
            assert hasattr(r['chapter_re'], 'match'), '乾淨 rules 未回編譯 pattern'
        finally:
            SE.DATA_DIR = orig_dir
    print('✓ sol gate 自檢：chapter_re/problem_re group 違約各自被擋且理由正確、乾淨本放行')


# ════════════════════════════════════════════════════════════════════════════
# 4. *.zh.json overlay × 366 → corpus._patch_blocks 的輸入契約
# ════════════════════════════════════════════════════════════════════════════
# _patch_blocks 吃 patch list：每 patch = {'i': idx, 'a'?: anchor, <TRANSLATABLE_FIELD>: 譯文}。
# 結構漂移會讓 _patch_blocks 誤接受/誤拒絕：
#   - 混入 tex/t/src 等非 TRANSLATABLE 欄 → 翻譯內容塞錯欄位 / 被靜默忽略
#   - anchor 截斷成 7 碼或大寫 → overlay_anchor 比對恆不符 → 整段譯文靜默退化純英文
#   - i 為負/浮點 → 比界判斷邏輯外（_patch_blocks 雖會跳過，但代表 agent 產物已壞）
# anchor 是防中譯錯置的命脈（防 parser 重跑後 i 漂移把中文掛到錯 block、甚至「中文先於英文」）。
_HEX8 = re.compile(r'^[0-9a-f]{8}$')


def test_all_zh_overlay_patches_well_formed():
    """掃全 366 zh.json 的每個 patch：key⊆{i,a}∪TRANSLATABLE_FIELDS、i 為 ≥0 int、
    a（若有）恰 8 碼小寫 hex。現況全綠（55247 帶 a 全合法、47559 legacy 無 a、0 違規），
    此 guard 是低維護成本的回歸鎖：純防 translate agent 未來結構漂移流進 overlay 合併。

    為何高含金量：anchor 漂移／非法欄 → 中文掛錯段、視覺上中文先於英文（silent-corruption）。"""
    # _HEX8 是這份 guard 對 anchor 的硬期望，但它須**與消費端真實的 hash 寬度綁定**：
    # 若哪天 overlay_anchor 改成 [:10] 而舊 overlay 仍 8 碼，_patch_blocks 會整批比對不符、
    # 譯文靜默退化純英文，而本 guard 卻仍綠（因為它只認 8 碼）。先在此釘死「消費端產的
    # anchor 形狀 == 本 guard 認的形狀」，讓兩者不會悄悄脫鉤。
    from book_pipeline.translate import overlay_anchor
    probe = overlay_anchor({'md': 'sample-source-text', 'title': 'x'})
    assert _HEX8.match(probe), (f'overlay_anchor 產的 anchor 不再是 8 碼小寫 hex（得 {probe!r}）→ '
                                f'本 guard 的 _HEX8 期望已與消費端脫鉤，須同步更新')

    allowed = {'i', 'a'} | set(corpus.TRANSLATABLE_FIELDS)
    paths = sorted(glob.glob(str(DATA_DIR / '*' / 'parsed' / '*.zh.json')))
    assert len(paths) >= 366, f'zh.json 數量異常（{len(paths)}）'

    violations: list[str] = []
    n_with_a = n_without_a = n_patch = 0

    def check_patch(patch: dict, where: str) -> None:
        nonlocal n_with_a, n_without_a, n_patch
        n_patch += 1
        if not isinstance(patch, dict):
            violations.append(f'{where}: patch 非 dict'); return
        extra = set(patch) - allowed
        if extra:
            violations.append(f'{where}: 非法欄 {sorted(extra)}（只准 {sorted(allowed)}）')
        i = patch.get('i')
        if not (isinstance(i, int) and not isinstance(i, bool) and i >= 0):
            violations.append(f'{where}: i 須 ≥0 int，得 {i!r}')
        if 'a' in patch:
            n_with_a += 1
            a = patch['a']
            if not (isinstance(a, str) and _HEX8.match(a)):
                violations.append(f'{where}: anchor 須 8 碼小寫 hex，得 {a!r}')
        else:
            n_without_a += 1
        # 至少要有一個可翻欄位才是有意義的 patch（純 {i,a} 無譯文 = agent 漏吐）
        if not (set(patch) & set(corpus.TRANSLATABLE_FIELDS)):
            violations.append(f'{where}: patch 無任何 TRANSLATABLE 欄（{sorted(patch)}）')

    for p in paths:
        slug, fn = Path(p).parent.parent.name, Path(p).name
        d = json.loads(Path(p).read_text())
        for k, patch in enumerate(d.get('body') or []):
            check_patch(patch, f'{slug}/{fn} body[{k}]')
        for pi, prob in enumerate(d.get('problems') or []):
            for k, patch in enumerate(prob.get('body') or []):
                check_patch(patch, f'{slug}/{fn} prob{pi}.body[{k}]')
            for k, patch in enumerate(prob.get('solution') or []):
                check_patch(patch, f'{slug}/{fn} prob{pi}.solution[{k}]')

    assert not violations, (f'zh.json overlay 結構漂移（{len(violations)} 條）：\n' +
                            '\n'.join(f'  - {v}' for v in violations[:40]))
    print(f'✓ zh.json × {len(paths)}：{n_patch} patch（{n_with_a} 帶 anchor 全合法、'
          f'{n_without_a} legacy 無 a）、0 結構違規')


# ════════════════════════════════════════════════════════════════════════════
# 5. catalog_overrides/*.json × 35 → apply_catalog_overrides 的輸入契約
# ════════════════════════════════════════════════════════════════════════════
# consumer（_apply_*）只在 daemon 實跑 catalog 階段 + 真實 parsed 在場才驗（worktree 無
# parsed → ValueError 觸不到）。故壞 override 可長躺 git，本靜態 guard 是 deploy 前唯一防線。
# 契約（對照 _apply_set_fields / _apply_replace_text / _apply_pdf_crop_insert 源碼）：
#   action ∈ {set_fields, replace_text, pdf_crop_insert, copy_solution_images}
#   selector：`^body\[\d+\]$` 或 `^problem:...:\w+\[\d+\]$`（後者帶 num，可選 #OCC）
#   set_fields：set/unset 的 key ⊆ allowed 欄白名單
#   replace_text：field ∈ {md,caption,tex} 且有 old/new
#   pdf_crop_insert：clip 長 4 且有 block + page
_SEL_BODY = re.compile(r'^body\[\d+\]$')
_SEL_PROB = re.compile(r'^problem:.+:\w+\[\d+\]$')   # num 含 #OCC 也被 `.+` 涵蓋
_OV_ALLOWED_FIELDS = {
    'id', 'caption', 'src', 'kind', 'aspect',
    'catalog_exclude_reason', 'catalog_repair_source', 'catalog_aliases',
}
_OV_TEXT_FIELDS = {'md', 'caption', 'tex'}
_OV_ACTIONS = {'set_fields', 'replace_text', 'pdf_crop_insert', 'copy_solution_images'}


def test_all_catalog_overrides_structural_contract():
    """掃全 35 檔 / 722 override 的純結構契約（不碰任何 parsed）。現況全合法 → 純防
    catalog-audit agent 漂移在 standby 套用時才炸。

    為何高含金量：consumer 的 ValueError 只在真實 parsed 在場才觸發 → 壞 override 可長躺
    git；套用時炸 = crash 整 tick（P=med × crash）。靜態 guard 是唯一在 deploy 前的防線。"""
    paths = sorted(glob.glob(str(OVERRIDE_DIR / '*.json')))
    assert len(paths) >= 35, f'catalog_overrides 數量異常（{len(paths)}）'

    violations: list[str] = []
    n_ov = 0
    for p in paths:
        slug = Path(p).stem
        spec = json.loads(Path(p).read_text())
        for ov in spec.get('overrides') or []:
            n_ov += 1
            oid = ov.get('id', '<no-id>')
            tag = f'{slug}/{oid}'
            action = ov.get('action')
            if action not in _OV_ACTIONS:
                violations.append(f'{tag}: 未知 action {action!r}（consumer 會 raise）')
                continue

            # selector + chunk 契約（copy_solution_images 不帶 selector/chunk）。
            # chunk 是 consumer 首句 `_chunk_path(slug, override['chunk'])` 的必需鍵：缺它
            # → KeyError 在 set_fields/replace_text/pdf_crop_insert 三大宗炸 → crash 整 tick。
            # 原測完全沒驗 chunk（盲點：721/722 條都吃 chunk 卻無人把關）。
            if action in ('set_fields', 'replace_text', 'pdf_crop_insert'):
                sel = ov.get('selector')
                if not (isinstance(sel, str) and (_SEL_BODY.match(sel) or _SEL_PROB.match(sel))):
                    violations.append(f'{tag}: selector 不符契約：{sel!r}')
                ch = ov.get('chunk')
                if not (isinstance(ch, str) and ch):
                    violations.append(f'{tag}: 缺/壞 chunk（consumer 首句即 KeyError 炸）：{ch!r}')

            if action == 'set_fields':
                set_keys = set((ov.get('set') or {}))
                unset_keys = set(ov.get('unset') or [])
                bad = (set_keys | unset_keys) - _OV_ALLOWED_FIELDS
                if bad:
                    # consumer 對非白名單欄 raise ValueError("unsupported field")
                    violations.append(f'{tag}: set_fields 非法欄 {sorted(bad)}')
                if not (set_keys or unset_keys):
                    violations.append(f'{tag}: set_fields 無 set 也無 unset（空操作）')

            elif action == 'replace_text':
                field = ov.get('field', 'md')
                if field not in _OV_TEXT_FIELDS:
                    violations.append(f'{tag}: replace_text field {field!r} 不在 {_OV_TEXT_FIELDS}')
                if 'old' not in ov or 'new' not in ov:
                    violations.append(f'{tag}: replace_text 缺 old/new')
                else:
                    # consumer 做 `old not in value`（value 為 str）→ old 非 str 會 TypeError；
                    # new 非 str 則 str.replace 炸。兩者皆須 str。
                    if not isinstance(ov.get('old'), str) or not isinstance(ov.get('new'), str):
                        violations.append(f'{tag}: replace_text old/new 須為 str，得 '
                                          f'{type(ov.get("old")).__name__}/{type(ov.get("new")).__name__}')

            elif action == 'pdf_crop_insert':
                clip = ov.get('clip')
                if not (isinstance(clip, list) and len(clip) == 4):
                    violations.append(f'{tag}: pdf_crop_insert clip 須長 4，得 {clip!r}')
                block = ov.get('block')
                if not isinstance(block, dict):
                    violations.append(f'{tag}: pdf_crop_insert 缺 block dict')
                # consumer：image_id = override.get('image_id') or override['block']['id']
                # → image_id 與 block.id 兩者皆缺 → KeyError 炸。至少一個須在。
                elif not (ov.get('image_id') or block.get('id')):
                    violations.append(f'{tag}: pdf_crop_insert 須有 image_id 或 block.id'
                                      f'（否則 consumer 取 image_id 時 KeyError）')
                # consumer 做 `int(override['page'])`：缺 page → KeyError；非 int-able → ValueError。
                if 'page' not in ov:
                    violations.append(f'{tag}: pdf_crop_insert 缺 page')
                else:
                    try:
                        int(ov['page'])
                    except (TypeError, ValueError):
                        violations.append(f'{tag}: pdf_crop_insert page 非 int-able：{ov["page"]!r}')

            elif action == 'copy_solution_images':
                if not ov.get('from_slug'):
                    violations.append(f'{tag}: copy_solution_images 缺 from_slug')

    assert not violations, (f'catalog_overrides 結構漂移（{len(violations)} 條）：\n' +
                            '\n'.join(f'  - {v}' for v in violations[:40]))
    print(f'✓ catalog_overrides × {len(paths)}：{n_ov} override 結構全合法')


if __name__ == '__main__':
    test_all_extract_rules_pass_validate()
    test_validate_rules_stub_isolation_self_check()
    test_all_live_sol_rules_load_without_sysexit()
    test_sol_group_gate_actually_fires()
    test_all_zh_overlay_patches_well_formed()
    test_all_catalog_overrides_structural_contract()
    print('\n全部通過 ✅')
