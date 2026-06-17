#!/bin/zsh
# 手動觸發一次 crawl 補貨：丟「強制補貨」marker，daemon 下個 observe cycle 無視水位/冷卻派
# crawl 小弟把書單補到 HIGH（消費即刪、恰跑一次）。= `devctl crawl-refill` 的便捷包裝。
# 用法：在 textbook-reader/ 直接跑 `./crawl-refill.sh`（cd 到腳本所在 repo 根，故任何 cwd 皆可）。
cd "$(dirname "$0")" || exit 1
exec uv run python -m book_pipeline.devctl crawl-refill
