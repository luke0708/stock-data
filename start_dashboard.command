#!/bin/bash
# 切换到脚本所在的目录，确保相对路径可用
cd "$(dirname "$0")"

echo "========================================================="
echo "📈 正在启动 StockDB 本地数据库可视化监控面板..."
echo "========================================================="

# 1. 检查并清理占用 5000 端口的旧 Flask 进程
PORT=5000
PID_LIST=$(lsof -t -i:$PORT)
if [ -n "$PID_LIST" ]; then
    echo "⚠️ 检查到端口 $PORT 已被旧进程占用，正在自动清理..."
    for pid in $PID_LIST; do
        if kill -0 $pid 2>/dev/null; then
            echo "正在关闭进程: $pid"
            kill $pid 2>/dev/null
            sleep 0.2
            if kill -0 $pid 2>/dev/null; then
                kill -9 $pid 2>/dev/null
            fi
        fi
    done
    echo "✅ 旧进程清理完成。"
    sleep 0.5
fi

# 2. 在后台等待1.5秒，等 Flask 服务器拉起后在浏览器中自动打开网页
(sleep 1.5 && open "http://127.0.0.1:5000") &

# 3. 检测虚拟环境并启动 Flask 开发服务器
if [ -f "./.venv/bin/python" ]; then
    echo "⚡ 检查到虚拟环境，正在启动服务..."
    ./.venv/bin/python dashboard/app.py
else
    echo "⚠️ 未检查到虚拟环境，尝试使用系统默认 python3 启动..."
    python3 dashboard/app.py
fi
