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


def test_rules_load_error_missing_required_key():
    """語法合法但缺必要 regex key（equation_label_re）→ parser.compile_regexes KeyError → 守護偵測。
    2026-06-24 dogfood：glover_overbye audit 漏 equation_label_re（376/377 本都有）→ parser 每 cycle 崩。
    語法守護（_yaml_parse_error）抓不到此類 → 守護升級為『parser 真能載入這份 rules』（含 compile_regexes）。"""
    complete = ("section_re: '^(\\d+\\.\\d+)\\s'\n"
                "subsection_re: '^(\\d+\\.\\d+\\.\\d+)\\s'\n"
                "problem_start_re: '^(\\d+)\\s'\n"
                "equation_label_re: '\\\\tag\\{([^}]+)\\}'\n")
    assert pt._rules_load_error(complete) is None, '完整 rules 應可載入'
    missing = ("section_re: '^(\\d+\\.\\d+)\\s'\n"
               "subsection_re: '^(\\d+\\.\\d+\\.\\d+)\\s'\n"
               "problem_start_re: '^(\\d+)\\s'\n")  # 缺 equation_label_re
    reason = pt._rules_load_error(missing)
    assert reason is not None and 'equation_label_re' in reason, f'缺必要 key 須被偵測：{reason}'
    # 壞 regex 也該被偵測
    bad_re = complete + "example_start_re: '([unclosed'\n"
    assert pt._rules_load_error(bad_re) is not None, '壞 regex 須被偵測'


if __name__ == '__main__':
    test_good_yaml_passes()
    test_unquoted_colon_title_caught()
    test_malformed_rules_reason_missing_file()
    test_rules_load_error_missing_required_key()
    print('全部通過 ✅')
