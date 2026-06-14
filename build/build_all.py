#!/usr/bin/env python3
"""一鍵 build：bake JSON + convert images。

用法：
    QBANK_ROOT=~/project/qbank python3 build/build_all.py [slug ...]
"""
import sys

import bake_json
import convert_images

if __name__ == '__main__':
    args = sys.argv[1:]
    print('=== bake JSON ===')
    bake_json.main(args)
    print('=== convert images → WebP ===')
    convert_images.main(args)
