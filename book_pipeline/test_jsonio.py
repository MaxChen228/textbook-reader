"""jsonio 單元測試（無外部依賴）：uv run python -m book_pipeline.test_jsonio

覆蓋：roundtrip、不存在→default、毀損→改名保全 .corrupt + 回 default、字面 null→default、
原子寫覆蓋既有檔。狀態檔『單次寫不留半截』是整條 pipeline 防 SIGKILL 截斷的地基。"""

import json
import os
import tempfile

from book_pipeline import jsonio


def _tmp():
    d = tempfile.mkdtemp(prefix='jsonio_test_')
    return os.path.join(d, 'state.json')


def test_roundtrip():
    p = _tmp()
    jsonio.atomic_write_json(p, {'a': 1, 'b': ['x', 'y']}, indent=2)
    assert jsonio.read_json(p, {}) == {'a': 1, 'b': ['x', 'y']}
    assert not os.path.exists(f'{p}.tmp{os.getpid()}'), 'tmp 應已 rename 掉'
    print('✓ roundtrip + tmp 清乾淨')


def test_missing_returns_default():
    assert jsonio.read_json(_tmp(), {'d': 1}) == {'d': 1}
    print('✓ 不存在 → default')


def test_corrupt_preserved_and_default():
    p = _tmp()
    with open(p, 'w') as f:
        f.write('{not valid json,,,')  # 模擬 SIGKILL 截斷的壞檔
    assert jsonio.read_json(p, {}) == {}, '毀損 → 回 default'
    assert not os.path.exists(p), '壞檔應被改名走（不留原位讓下次又炸）'
    corrupt = [fn for fn in os.listdir(os.path.dirname(p)) if '.corrupt-' in fn]
    assert corrupt, '壞檔應保全成 .corrupt-<ts> 供搶救'
    print('✓ 毀損 → 改名 .corrupt 保全 + 回 default（不靜默清空）')


def test_literal_null_is_default():
    p = _tmp()
    with open(p, 'w') as f:
        json.dump(None, f)
    assert jsonio.read_json(p, {}) == {}
    print('✓ 字面 null → default（沿用舊 `or {}` 語義）')


def test_overwrite_atomic():
    p = _tmp()
    jsonio.atomic_write_json(p, {'v': 1})
    jsonio.atomic_write_json(p, {'v': 2})
    assert jsonio.read_json(p, {}) == {'v': 2}
    print('✓ 覆寫既有檔（os.replace）')


if __name__ == '__main__':
    test_roundtrip()
    test_missing_returns_default()
    test_corrupt_preserved_and_default()
    test_literal_null_is_default()
    test_overwrite_atomic()
    print('\n全部通過 ✅')
