#!/bin/bash
set -u

AVD_NAME="HuTest_HighEnd_5G"
SDK_ROOT="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Library/Android/sdk}}"
EMULATOR_BIN="$SDK_ROOT/emulator/emulator"
if [ -x "/opt/homebrew/bin/adb" ]; then
    ADB_BIN="/opt/homebrew/bin/adb"
elif command -v adb >/dev/null 2>&1; then
    ADB_BIN="$(command -v adb)"
else
    ADB_BIN="$SDK_ROOT/platform-tools/adb"
fi
LOG_PATH="/tmp/hutest-highend-5g-emulator.log"

if [ ! -x "$EMULATOR_BIN" ] || [ ! -x "$ADB_BIN" ]; then
    echo "❌ 未找到 Android SDK，请设置 ANDROID_SDK_ROOT"
    exit 1
fi

for serial in $("$ADB_BIN" devices | awk 'NR>1 && $2=="device" {print $1}'); do
    running_name=$("$ADB_BIN" -s "$serial" emu avd name 2>/dev/null | head -1 | tr -d '\r')
    if [ "$running_name" = "$AVD_NAME" ]; then
        echo "✅ 虎测高端机模拟器已启动：$serial"
        exit 0
    fi
done

echo "🚀 正在启动虎测高端机模拟器（5GB · 1080×2400）..."
nohup "$EMULATOR_BIN" \
    -avd "$AVD_NAME" \
    -memory 5120 \
    -gpu host \
    -no-audio \
    -no-snapshot \
    -no-boot-anim \
    >"$LOG_PATH" 2>&1 &

for _attempt in $(seq 1 120); do
    for serial in $("$ADB_BIN" devices | awk 'NR>1 && $2=="device" {print $1}'); do
        running_name=$("$ADB_BIN" -s "$serial" emu avd name 2>/dev/null | head -1 | tr -d '\r')
        booted=$("$ADB_BIN" -s "$serial" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
        if [ "$running_name" = "$AVD_NAME" ] && [ "$booted" = "1" ]; then
            echo "✅ 虎测高端机模拟器已就绪：$serial"
            echo "   内存：5GB；虎测平台刷新设备后即可使用。"
            exit 0
        fi
    done
    sleep 1
done

echo "❌ 模拟器启动超时，日志：$LOG_PATH"
exit 1
