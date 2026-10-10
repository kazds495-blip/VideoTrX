"""MAI-Transcribe-2 转写 + 静音贴边。

流程：音频文件 → MAI API（词级时间戳）→ ffmpeg silencedetect 量出静音区
→ 保守护栏贴边（只把首词起点提前、末词终点延后，偏移有上限、不越过相邻词）
→ output/log/transcript.json（检测到的语言 + 词级时间戳）
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import requests

from videotrx import config, paths
from videotrx.utils import write_json

API_VERSION = "2025-10-15"
SUPPORTED_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".webm"}


def load_mai_config() -> dict:
    missing = [k for k in ("key", "endpoint") if not config.get(f"mai.{k}", "")]
    if missing:
        raise RuntimeError(f"config.yaml 缺少 mai.{missing[0]} 配置")
    return {
        "key": str(config.get("mai.key")),
        "endpoint": str(config.get("mai.endpoint")).rstrip("/"),
        "model": str(config.get("mai.model", "") or "MAI-Transcribe-2"),
        "lead_ms": int(config.get("mai.snap_lead_ms", 20)),
        "tail_ms": int(config.get("mai.snap_tail_ms", 10)),
        "threshold_db": int(config.get("mai.silence_threshold_db", -20)),
        "min_silence": float(config.get("mai.silence_min_dur", 0.1)),
        "max_adjust_ms": int(config.get("mai.snap_max_adjust_ms", 500)),
    }


def ensure_mp3(audio: Path, log=print) -> Path:
    """mp3 原样返回；其他格式转成 16k 单声道 mp3（paths.MAI_INPUT）后再上传。"""
    if audio.suffix.lower() == ".mp3":
        return audio
    target = paths.MAI_INPUT
    log(f"MAI：转码 {audio.name} → {target.name}")
    subprocess.run(
        [paths.tool("ffmpeg"), "-y", "-i", str(audio), "-vn", "-c:a", "libmp3lame", "-b:a", "32k",
         "-ar", "16000", "-ac", "1", str(target)],
        check=True, capture_output=True,
    )
    return target


def transcribe(audio: Path, cfg: dict, log=print) -> dict:
    definition = {
        "enhancedMode": {
            "enabled": True,
            "model": cfg["model"],
            "modelOptions": {"timestamps": "word"},
        }
    }
    url = f"{cfg['endpoint']}/speechtotext/transcriptions:transcribe?api-version={API_VERSION}"
    headers = {"Ocp-Apim-Subscription-Key": cfg["key"], "Accept": "application/json"}
    log(f"MAI：上传音频 {audio.name}（{audio.stat().st_size / 1e6:.1f} MB）")
    with audio.open("rb") as handle:
        resp = requests.post(
            url, headers=headers, timeout=1800,
            files={"audio": (audio.name, handle, "audio/mpeg")},
            data={"definition": json.dumps(definition)},
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"MAI API {resp.status_code}：{resp.text[:300]}")
    return resp.json()


def extract_words(data: dict) -> list[dict]:
    words = []
    for phrase in data.get("phrases") or []:
        for word in phrase.get("words") or []:
            text = str(word.get("text") or "").strip()
            if not text:
                continue
            start = word.get("offsetMilliseconds", 0) / 1000.0
            end = start + word.get("durationMilliseconds", 0) / 1000.0
            words.append({"text": text, "start": round(start, 3), "end": round(max(end, start), 3)})
    return words


def detect_language(data: dict) -> str:
    for phrase in data.get("phrases") or []:
        locale = str(phrase.get("locale") or "").strip()
        if locale:
            return locale.split("-")[0]
    return ""


def run_silencedetect(audio: Path, cfg: dict, log=print) -> list[tuple[float, float]]:
    proc = subprocess.run(
        [paths.tool("ffmpeg"), "-i", str(audio), "-af",
         f"silencedetect=noise={cfg['threshold_db']}dB:d={cfg['min_silence']}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    pairs = re.findall(r"silence_(start|end):\s*(-?[\d.]+)", proc.stderr or "")
    silences: list[tuple[float, float]] = []
    pending: float | None = None
    for kind, value in pairs:
        moment = float(value)
        if kind == "start":
            pending = moment
        elif pending is not None:
            silences.append((pending, moment))
            pending = None
    if pending is not None:
        silences.append((pending, 10 ** 9))  # 音频结尾仍是静音
    log(f"MAI：静音检测完成，共 {len(silences)} 段静音")
    return silences


def speech_regions(silences: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    """静音的补集即发声区，丢弃 <50ms 的碎片。"""
    regions: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in silences:
        if start > cursor:
            regions.append((cursor, start))
        cursor = max(cursor, end)
    if duration > cursor:
        regions.append((cursor, duration))
    return [(s, e) for s, e in regions if e - s > 0.05]


def snap_boundaries(words: list[dict], regions: list[tuple[float, float]], cfg: dict, log=print) -> list[dict]:
    """贴边：把发声区间首/末词的边界贴到实测波形边缘。
    选词用贴边前的原始时间段：与发声区间重叠的词，首词改起点、末词改终点；
    改写只允许起点提前、终点延后，不越过相邻词、不超过最大偏移，否则保留原值。"""
    lead = cfg["lead_ms"] / 1000.0
    tail = cfg["tail_ms"] / 1000.0
    cap = cfg["max_adjust_ms"] / 1000.0
    orig = [(w["start"], w["end"]) for w in words]  # 原始时间段，选词用，不随改写变化
    snapped = 0
    for r0, r1 in regions:
        ov = [k for k, (s, e) in enumerate(orig) if s < r1 and e > r0]
        if not ov:
            continue
        k = ov[0]
        tgt = max(r0 - lead, 0.0)
        if 0 <= words[k]["start"] - tgt <= cap and (k == 0 or tgt >= words[k - 1]["end"]):
            words[k]["start"] = round(tgt, 3)
            snapped += 1
        k = ov[-1]
        tgt = r1 + tail
        if 0 <= tgt - words[k]["end"] <= cap and (k == len(words) - 1 or tgt <= words[k + 1]["start"]):
            words[k]["end"] = round(tgt, 3)
            snapped += 1
    log(f"MAI：贴边完成，调整 {snapped} 处边界")
    return words


def write_outputs(words: list[dict], language: str) -> Path:
    entries = []
    for w in words:
        text = w["text"].strip().strip('"').strip()
        if text:
            entries.append({"text": text, "start": w["start"], "end": w["end"]})
    transcript = {"language": language, "source": "mai-transcribe-2", "words": entries}
    write_json(paths.TRANSCRIPT, transcript)
    return paths.TRANSCRIPT


def run(audio_path: str, log=print, progress=lambda value: None) -> Path:
    cfg = load_mai_config()
    paths.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    audio = ensure_mp3(Path(audio_path), log)
    progress(25)
    data = transcribe(audio, cfg, log)
    words = extract_words(data)
    if not words:
        raise RuntimeError("MAI 未返回任何词")
    log(f"MAI：返回 {len(words)} 个词")
    progress(55)
    silences = run_silencedetect(audio, cfg, log)
    progress(70)
    duration = data.get("durationMilliseconds", 0) / 1000.0
    words = snap_boundaries(words, speech_regions(silences, duration), cfg, log)
    progress(85)
    transcript = write_outputs(words, detect_language(data))
    log(f"MAI：已输出 {transcript}")
    progress(100)
    return transcript
