#!/bin/bash
# 萌虎测试平台 v1.5 启动脚本（带自动重启）
cd "$(dirname "$0")"
echo "🐯 启动萌虎测试平台 v1.5..."

MAX_CRASHES=5
CRASH_COUNT=0
CRASH_WINDOW=60  # 秒
LAST_CRASH=0
PORT=8898

while true; do
    python3 server.py
    EXIT_CODE=$?
    NOW=$(date +%s)

    # 正常退出不重启
    if [ $EXIT_CODE -eq 0 ] || [ $EXIT_CODE -eq 130 ]; then
        echo "👋 萌虎测试平台 v1.5 已停止"
        exit 0
    fi

    # 防抖：短时间内反复崩溃则停止重启
    if [ $((NOW - LAST_CRASH)) -lt $CRASH_WINDOW ]; then
        CRASH_COUNT=$((CRASH_COUNT + 1))
    else
        CRASH_COUNT=1
    fi
    LAST_CRASH=$NOW

    if [ $CRASH_COUNT -gt $MAX_CRASHES ]; then
        echo "🚨 $CRASH_WINDOW 秒内崩溃 $MAX_CRASHES 次，停止自动重启。请手动排查后重新运行 start.sh"
        exit 1
    fi

    echo "⚠️  服务器异常退出 (code=$EXIT_CODE)，2秒后自动重启..."
    sleep 2
done
