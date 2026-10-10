import threading

import yaml

from videotrx import paths

CONFIG_PATH = paths.ROOT / "config.yaml"
MISSING = object()

_lock = threading.Lock()
_cache = {"mtime": None, "data": None}


def _load() -> dict:
    """读取 config.yaml；文件 mtime 不变时复用上次的结果。"""
    with _lock:
        mtime = CONFIG_PATH.stat().st_mtime_ns
        if _cache["mtime"] != mtime:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                _cache["data"] = yaml.safe_load(f) or {}
            _cache["mtime"] = mtime
        return _cache["data"]


def get(key, default=MISSING):
    """按点号路径取值；键缺失时返回 default，未给 default 则抛 KeyError(key)。"""
    value = _load()
    for part in key.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif default is MISSING:
            raise KeyError(key)
        else:
            return default
    return value


def get_bool(key, default):
    """字符串 1/true/yes/on 视为真；键缺失或留空时返回 default。"""
    value = get(key, None)
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def to_int(value, default, min_value=None, max_value=None):
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def to_float(value, default, min_value=None):
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    if min_value is not None:
        parsed = max(min_value, parsed)
    return parsed
