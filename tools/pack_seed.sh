#!/usr/bin/env bash
# 重打热更种子包 assets/seed.zip。
#
# 为什么需要这个脚本：CI 里打的是「构建产物里的 seed.zip」，仓库里这份是给
# 热更通道用的，两边各打各的会长期分叉（曾漏过 src/picker.py 导致 ImportError）。
# 约定：每次提交（改了 app_main.py / songgrab.kv / src / assets/fonts）之前跑一次，
# 然后把 assets/seed.zip 一起提交。
set -euo pipefail
cd "$(dirname "$0")/.."

rm -f assets/seed.zip
zip -rq assets/seed.zip app_main.py songgrab.kv src assets/fonts \
    -x "*/__pycache__/*" "*.pyc" "*.pyo"

echo "已重打 assets/seed.zip"
unzip -l assets/seed.zip | tail -2
echo "sha256: $(sha256sum assets/seed.zip | cut -d' ' -f1)"
echo "提醒：清单里必须同时出现 src/ 与 assets/fonts/（缺了会在热更后 ImportError / 无字体）"