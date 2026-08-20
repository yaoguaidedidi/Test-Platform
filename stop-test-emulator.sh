#!/bin/bash
set -u

AVD_NAME="HuTest_Standalone"
SDK_ROOT="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Library/Android/sdk}}"
if [ -x "/opt/homebrew/bin/adb" ]; then
    ADB_BIN="/opt/homebrew/bin/adb"
elif command -v adb >/dev/null 2>&1; then
    ADB_BIN="$(command -v adb)"
else
    ADB_BIN="$SDK_ROOT/platform-tools/adb"
fi

if [ ! -x "$ADB_BIN" ]; then
    echo "❌ 未找到 adb，请设置 ANDROID_SDK_ROOT"
    exit 1
fi

found=0
for serial in $("$ADB_BIN" devices | awk 'NR>1 && $2=="device" {print $1}'); do
    running_name=$("$ADB_BIN" -s "$serial" emu avd name 2>/dev/null | head -1 | tr -d '\r')
    if [ "$running_name" = "$AVD_NAME" ]; then
        found=1
        echo "🛑 正在停止虎测专用模拟器：$serial"
        "$ADB_BIN" -s "$serial" emu kill >/dev/null
    fi
done

if [ "$found" -eq 0 ]; then
    echo "ℹ️ 虎测专用模拟器当前未运行"
else
    echo "✅ 已发送停止命令"
fi
