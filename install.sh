#!/usr/bin/env bash
# VideoTrX 一键安装（Linux x86_64 / aarch64）。
# 所有工具都装在项目目录内；可重复运行，已存在的项跳过，yt-dlp 每次升级。

set -u

FFMPEG_BRANCH="9.0"
UV_RELEASE="https://github.com/astral-sh/uv/releases/latest/download"
FFMPEG_RELEASE="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
DENO_RELEASE="https://github.com/denoland/deno/releases/latest/download"
SPACY_MODEL="https://github.com/explosion/spacy-models/releases/download/en_core_web_md-3.7.1/en_core_web_md-3.7.1-py3-none-any.whl"

fail() {
    echo "安装失败：$1" >&2
    exit 1
}

download() {
    curl -fL --progress-bar --retry 3 --connect-timeout 30 -o "$2" "$1"
}

# 1. 切换到脚本所在目录
cd "$(dirname "$0")" || fail "无法进入脚本所在目录"
ROOT="$(pwd)"

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)
        UV_ASSET="uv-x86_64-unknown-linux-gnu.tar.gz"
        FFMPEG_PLATFORM="linux64"
        DENO_ASSET="deno-x86_64-unknown-linux-gnu.zip"
        ;;
    Linux-aarch64)
        UV_ASSET="uv-aarch64-unknown-linux-gnu.tar.gz"
        FFMPEG_PLATFORM="linuxarm64"
        DENO_ASSET="deno-aarch64-unknown-linux-gnu.zip"
        ;;
    *)
        echo "暂不支持此平台：$(uname -s) $(uname -m)"
        exit 1
        ;;
esac

for cmd in curl tar xz; do
    command -v "$cmd" >/dev/null 2>&1 \
        || fail "缺少命令 $cmd，请先用系统包管理器安装（Debian/Ubuntu：sudo apt install curl tar xz-utils）"
done

TMP_DIR="$ROOT/tools/.download"
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR" bin || fail "无法在项目目录中创建文件夹，请检查目录权限"
trap 'rm -rf "$TMP_DIR"' EXIT

# 2. uv
UV="$ROOT/tools/uv/uv"
if [ -f "$UV" ]; then
    echo "==> uv 已存在，跳过"
else
    echo "==> 下载 uv"
    download "$UV_RELEASE/$UV_ASSET" "$TMP_DIR/uv.tar.gz" || fail "下载 uv 失败，请检查网络"
    mkdir -p tools/uv
    tar -xzf "$TMP_DIR/uv.tar.gz" -C tools/uv --strip-components=1 || fail "解压 uv 失败"
fi
chmod +x tools/uv/uv tools/uv/uvx || fail "无法给 uv 加可执行权限"

# 3. Python 与虚拟环境
export UV_PYTHON_INSTALL_DIR="$ROOT/tools/python"
if [ -d .venv ]; then
    echo "==> .venv 已存在，跳过"
else
    echo "==> 安装 Python 3.12 并创建 .venv"
    "$UV" venv .venv --python 3.12 --python-preference only-managed --seed \
        || fail "安装 Python 或创建虚拟环境失败"
fi

# 4. Python 依赖
echo "==> 安装 Python 依赖"
"$UV" pip install --python .venv -r requirements.txt || fail "安装 Python 依赖失败"
echo "==> 升级 yt-dlp"
"$UV" pip install --python .venv --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs "yt-dlp[default]" \
    || fail "升级 yt-dlp 失败"
echo "==> 安装 spaCy 英文模型"
"$UV" pip install --python .venv "$SPACY_MODEL" || fail "安装 spaCy 英文模型失败"

# 5. ffmpeg / ffprobe
if [ -f bin/ffmpeg ] && [ -f bin/ffprobe ]; then
    echo "==> ffmpeg 已存在，跳过"
else
    echo "==> 下载 ffmpeg $FFMPEG_BRANCH"
    name="ffmpeg-n${FFMPEG_BRANCH}-latest-${FFMPEG_PLATFORM}-gpl-${FFMPEG_BRANCH}"
    download "$FFMPEG_RELEASE/$name.tar.xz" "$TMP_DIR/ffmpeg.tar.xz" || fail "下载 ffmpeg 失败，请检查网络"
    tar -xJf "$TMP_DIR/ffmpeg.tar.xz" -C "$TMP_DIR" "$name/bin/ffmpeg" "$name/bin/ffprobe" \
        || fail "解压 ffmpeg 失败"
    mv -f "$TMP_DIR/$name/bin/ffmpeg" "$TMP_DIR/$name/bin/ffprobe" bin/ || fail "无法把 ffmpeg 放进 bin/"
fi

# 6. deno
if [ -f bin/deno ]; then
    echo "==> deno 已存在，跳过"
else
    echo "==> 下载 deno"
    download "$DENO_RELEASE/$DENO_ASSET" "$TMP_DIR/deno.zip" || fail "下载 deno 失败，请检查网络"
    .venv/bin/python -m zipfile -e "$TMP_DIR/deno.zip" bin/ || fail "解压 deno 失败"
fi
chmod +x bin/* || fail "无法给 bin/ 下的程序加可执行权限"

# 7. 配置文件
if [ -f config.yaml ]; then
    echo "==> config.yaml 已存在，保留不动"
else
    cp config.example.yaml config.yaml || fail "生成 config.yaml 失败"
    echo "==> 已从 config.example.yaml 生成 config.yaml"
    echo "请在 config.yaml 中填写 api、polish、mai 的 key"
fi

# 8. 启动方式
echo
echo "安装完成。启动：bash start.sh"
