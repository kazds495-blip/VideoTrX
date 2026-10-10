#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
    echo "未找到 .venv，请先运行 bash install.sh"
    exit 1
fi
exec .venv/bin/python app.py "$@"
