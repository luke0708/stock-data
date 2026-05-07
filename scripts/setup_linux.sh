#!/bin/bash
# setup_linux.sh — Ubuntu/Linux 一键安装脚本
#
# 使用：
#   bash scripts/setup_linux.sh
#
# 完成后：
#   - 安装 Python 依赖
#   - 配置 cron 每日自动更新
#   - 首次初始化提示

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON=""

echo "========================================================"
echo "  stockdb Linux 安装配置"
echo "  项目目录: $PROJECT_DIR"
echo "========================================================"

# ── 1. 找 Python ─────────────────────────────────────
echo ""
echo "[1/4] 检查 Python 环境 ..."

for cmd in python3.11 python3.10 python3.9 python3; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" --version 2>&1)
        echo "  找到: $cmd ($VER)"
        PYTHON=$(command -v "$cmd")
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "  未找到 Python3，尝试安装 ..."
    sudo apt-get update -qq
    sudo apt-get install -y python3 python3-pip python3-venv
    PYTHON=$(command -v python3)
fi

echo "  使用: $PYTHON"

# ── 2. 创建或复用 venv ───────────────────────────────
echo ""
echo "[2/4] 配置虚拟环境 ..."

VENV_DIR="$PROJECT_DIR/.venv"
if [ -d "$VENV_DIR" ]; then
    echo "  已有 venv: $VENV_DIR"
else
    echo "  创建 venv: $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python3"
VENV_PIP="$VENV_DIR/bin/pip"

# 升级 pip
"$VENV_PIP" install --quiet --upgrade pip

# 安装依赖
echo "  安装依赖（requirements.txt）..."
"$VENV_PIP" install --quiet -r "$PROJECT_DIR/requirements.txt"

# 安装 stockdb 本身（editable）
echo "  安装 stockdb 包 ..."
"$VENV_PIP" install --quiet -e "$PROJECT_DIR"

echo "  ✅ 依赖安装完成"

# ── 3. 配置 cron ────────────────────────────────────
echo ""
echo "[3/4] 配置每日定时更新（cron）..."

CRON_LOG="$PROJECT_DIR/logs/cron.log"
mkdir -p "$PROJECT_DIR/logs"

# 每个工作日 16:35 运行（周一到周五）
CRON_CMD="35 16 * * 1-5 $VENV_PYTHON $PROJECT_DIR/scripts/daily_update.py >> $CRON_LOG 2>&1"

# 检查是否已添加
if crontab -l 2>/dev/null | grep -q "daily_update.py"; then
    echo "  cron 任务已存在，跳过"
else
    # 追加到现有 crontab
    (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
    echo "  ✅ 已添加 cron 任务: 每周一至五 16:35 自动更新"
fi

echo "  当前 crontab 中的 stockdb 任务:"
crontab -l 2>/dev/null | grep "daily_update" | sed 's/^/    /'

# ── 4. 首次初始化提示 ──────────────────────────────
echo ""
echo "[4/4] 初始化提示"

DAILY_DIR="$PROJECT_DIR/data/daily"
if [ -d "$DAILY_DIR" ] && [ "$(find "$DAILY_DIR" -name '*.parquet' | head -1)" ]; then
    PARQUET_COUNT=$(find "$DAILY_DIR" -name '*.parquet' | wc -l)
    echo "  已有 $PARQUET_COUNT 个 Parquet 文件，数据库已初始化"
    echo "  补全最新数据运行："
    echo "    $VENV_PYTHON $PROJECT_DIR/scripts/daily_update.py"
else
    echo "  ⚠️  未检测到本地数据，请运行首次初始化（约 30~60 分钟）："
    echo ""
    echo "    $VENV_PYTHON $PROJECT_DIR/scripts/init_full.py"
    echo ""
    echo "  注意：需要关闭代理（如有）"
fi

# ── 完成 ────────────────────────────────────────────
echo ""
echo "========================================================"
echo "  安装完成！"
echo ""
echo "  Python 路径: $VENV_PYTHON"
echo "  每日更新:    $VENV_PYTHON scripts/daily_update.py"
echo "  日志目录:    $PROJECT_DIR/logs/"
echo ""
echo "  在其他项目中使用:"
echo "    $VENV_PIP install -e $PROJECT_DIR"
echo "    # 或共享 venv："
echo "    source $VENV_DIR/bin/activate"
echo "========================================================"
