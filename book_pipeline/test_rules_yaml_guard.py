"""extract_rules.yaml 語法守護：uv run python -m book_pipeline.test_rules_yaml_guard

守 2026-06-24 dogfood 坑：audit worker 產出語法壞的 extract_rules.yaml（章標含未引號冒號，
如 `title: Transmission Lines: Steady-State Operation`）→ parser.load_rules 的 yaml.safe_load
拋 ScannerError → 書卡 待parse、parser 每 cycle 崩、**無 R 狀態無提案的靜默 crash-loop**。
_yaml_parse_error 在 audit 接受時擋下（→ mark_audit_blocked，actionable），不放進 parser 無限崩。"""

from book_pipeline import pipeline_tick as pt


def test_good_yaml_passes():
    assert pt._yaml_parse_error("slug: x\nchapters:\n  - num: 1\n    title: Intro\n") is None
    # 含冒號但**有引號** → 合法
    assert pt._yaml_parse_error('title: "Transmission Lines: Steady-State Operation"\n') is None


def test_unquoted_colon_title_caught():
    # glover_overbye 實際炸法：list item 內 title 值含未引號冒號 → mapping values not allowed
    bad = ("chapters:\n"
           "  - num: 5\n"
           "    title: Transmission Lines: Steady-State Operation\n"
           "    page_start: 256\n")
    reason = pt._yaml_parse_error(bad)
    assert reason is not None, '未引號冒號的 title 必須被偵測為壞 yaml'
    assert 'mapping values' in reason.lower() or 'scanner' in reason.lower() or reason, reason
    # 錯因單行、截斷（不噴整段 traceback）
    assert '\n' not in reason and len(reason) <= 160


def test_malformed_rules_reason_missing_file():
    # 不存在的 slug → None（不存在不算壞，交既有「無 yaml」分支處理）
    assert pt._malformed_rules_reason('__no_such_slug_xyz__') is None


if __name__ == '__main__':
    test_good_yaml_passes()
    test_unquoted_colon_title_caught()
    test_malformed_rules_reason_missing_file()
    print('全部通過 ✅')
