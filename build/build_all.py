#!/usr/bin/env python3
"""一鍵 build：bake JSON + convert images。

用法：
    uv run python -m build.build_all [slug ...]
"""
import sys

from build import bake_json, convert_images

if __name__ == '__main__':
    args = sys.argv[1:]
    print('=== bake JSON ===')
    bake_json.main(args)
    print('=== convert images → WebP ===')
    convert_images.main(args)
