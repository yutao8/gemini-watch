#!/usr/bin/env bash
# GeminiWatch 打包脚本（macOS）
# 用法：chmod +x build_mac.sh && ./build_mac.sh
set -euo pipefail

cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
ICON_FLAG=""

echo "============================================"
echo "  GeminiWatch 打包脚本 (macOS)"
echo "============================================"
echo

echo "[1/6] 检查 Python..."
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "    未找到 $PY，请安装 Python 3.9+"
    exit 1
fi

echo "[2/6] 检查资源文件..."
if [ ! -f gemini_watch.py ]; then
    echo "    缺少 gemini_watch.py，请在项目根目录运行本脚本"
    exit 1
fi
if [ ! -f panel.html ]; then
    echo "    缺少 panel.html，面板将无法显示"
    exit 1
fi

echo "[3/6] 安装/更新依赖..."
"$PY" -m pip install --quiet --disable-pip-version-check pyinstaller requests pillow pystray

echo "[4/6] 关闭正在运行的实例..."
pkill -f "GeminiWatch.app/Contents/MacOS/GeminiWatch" 2>/dev/null || true
pkill -f "gemini_watch.py" 2>/dev/null || true
sleep 1

echo "[5/6] 生成 .icns 图标..."
if [ -f assets/app.ico ] && command -v iconutil >/dev/null 2>&1; then
    rm -rf build/app.iconset
    mkdir -p build/app.iconset
    if "$PY" - <<'PY'
from PIL import Image

src = Image.open("assets/app.ico").convert("RGBA")
items = [
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
]
for size, name in items:
    src.resize((size, size), Image.LANCZOS).save("build/app.iconset/" + name)
PY
    then
        if iconutil -c icns build/app.iconset -o assets/app.icns 2>/dev/null; then
            ICON_FLAG="--icon assets/app.icns"
        else
            echo "    iconutil 转换失败，将使用默认图标"
        fi
    else
        echo "    图标转换失败，将使用默认图标"
    fi
else
    echo "    跳过（缺少 assets/app.ico 或 iconutil）"
fi

echo "[6/6] 打包..."
# --onedir（默认）比 --onefile 冷启动快，适合常驻应用
$PY -m PyInstaller --noconfirm --clean --windowed --name GeminiWatch \
  --add-data "panel.html:." \
  --collect-submodules pystray \
  $ICON_FLAG \
  gemini_watch.py

if [ ! -d "dist/GeminiWatch.app" ]; then
    echo "    未生成 dist/GeminiWatch.app"
    exit 1
fi

rm -rf build GeminiWatch.spec

echo
echo "============================================"
echo "  完成: dist/GeminiWatch.app"
echo "  配置与日志: ~/Library/Application Support/GeminiWatch"
echo "  面板地址: http://127.0.0.1:47131"
echo "============================================"
echo
echo "首次运行如被 Gatekeeper 拦截："
echo "  xattr -dr com.apple.quarantine dist/GeminiWatch.app"
