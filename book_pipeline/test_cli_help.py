"""CLI help should be side-effect free."""

import subprocess
import sys


HELP_MODULES = [
    'book_pipeline.backfill_math',
    'book_pipeline.extract_cover',
    'book_pipeline.smoke',
    'book_pipeline.validate_rules',
    'build.bake_json',
]


def test_cli_help_exits_cleanly():
    for mod in HELP_MODULES:
        proc = subprocess.run(
            [sys.executable, '-m', mod, '--help'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        assert proc.returncode == 0, (mod, proc.returncode, proc.stdout, proc.stderr)
        assert 'usage:' in proc.stdout.lower()
