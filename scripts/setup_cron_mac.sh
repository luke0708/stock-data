#!/bin/bash
# setup_cron_mac.sh — 一键配置 Mac 每日自动更新
# 运行方式：bash scripts/setup_cron_mac.sh

set -e

# 找到 stock-data 根目录（本脚本所在目录的上一级）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# 检测 Python 路径（优先用项目 venv）
if [ -f "$ROOT_DIR/.venv/bin/python" ]; then
    PYTHON="$ROOT_DIR/.venv/bin/python"
elif [ -f "$ROOT_DIR/venv/bin/python" ]; then
    PYTHON="$ROOT_DIR/venv/bin/python"
else
    PYTHON="$(which python3)"
fi

UPDATE_SCRIPT="$ROOT_DIR/scripts/daily_update.py"

echo "================================================"
echo "stockdb — Mac 定时任务配置"
echo "================================================"
echo "根目录:   $ROOT_DIR"
echo "Python:   $PYTHON"
echo "脚本:     $UPDATE_SCRIPT"
echo ""

# 验证 Python 和脚本存在
if [ ! -f "$PYTHON" ]; then
    echo "❌ Python 未找到: $PYTHON"
    exit 1
fi
if [ ! -f "$UPDATE_SCRIPT" ]; then
    echo "❌ 脚本未找到: $UPDATE_SCRIPT"
    exit 1
fi

# 构建 cron 任务（每个工作日 16:35 执行）
CRON_JOB="35 16 * * 1-5 $PYTHON $UPDATE_SCRIPT >> $ROOT_DIR/logs/cron.log 2>&1"

echo "即将添加 cron 任务："
echo "  $CRON_JOB"
echo ""

# 检查是否已存在
EXISTING=$(crontab -l 2>/dev/null | grep -F "$UPDATE_SCRIPT" || true)
if [ -n "$EXISTING" ]; then
    echo "⚠️  已存在相同任务，跳过添加："
    echo "  $EXISTING"
else
    # 追加到 crontab
    (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
    echo "✅ cron 任务已添加！"
fi

echo ""
echo "当前全部 cron 任务："
crontab -l 2>/dev/null || echo "(空)"
echo ""
echo "================================================"
echo "验证：手动触发一次更新"
echo "  $PYTHON $UPDATE_SCRIPT"
echo "================================================"
