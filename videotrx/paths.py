import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT / "bin"
OUTPUT_DIR = ROOT / "output"
LOG_DIR = OUTPUT_DIR / "log"
GPT_LOG_DIR = OUTPUT_DIR / "gpt_log"
ARCHIVE_DIR = ROOT / "archive"
CUSTOM_TERMS = ROOT / "custom_terms.json"
MAI_INPUT = OUTPUT_DIR / "mai_input.mp3"
TRANSCRIPT = LOG_DIR / "transcript.json"
TRANSCRIPT_BEFORE_CORRECTION = LOG_DIR / "transcript_before_english_correction.json"
ENGLISH_CORRECTION_DONE = LOG_DIR / "english_correction_done.json"
ENGLISH_CORRECTION_CHANGELOG = LOG_DIR / "english_correction_changelog.jsonl"
NLP_SPLIT = LOG_DIR / "split_by_nlp.json"
NLP_SPLIT_BEFORE_REPAIR = LOG_DIR / "split_by_nlp_before_entity_repair.json"
ENTITY_REPAIR_CHANGELOG = LOG_DIR / "entity_repair_changelog.jsonl"
MEANING_SPLIT = LOG_DIR / "split_by_meaning.json"
TERMINOLOGY = LOG_DIR / "terminology.json"
TRANSLATION = LOG_DIR / "translation.json"
POLISH_CHANGELOG = LOG_DIR / "polish_changelog.jsonl"
POLISH_PROGRESS = LOG_DIR / "polish_progress.json"
POLISH_PREVIEW = LOG_DIR / "polish_progress.txt"
POLISH_STREAM = LOG_DIR / "polish_stream.txt"
SUBTITLE_LINES = LOG_DIR / "subtitle_lines.json"
SUBTITLE_SPLIT_DECISIONS = LOG_DIR / "subtitle_split_decisions.json"
FINAL_ASS = OUTPUT_DIR / "src_trans.ass"


def tool(name: str) -> str:
    """bin/ 下有该工具（Windows 加 .exe）就返回其绝对路径，否则返回 name，交给 PATH 查找。"""
    local = BIN_DIR / (f"{name}.exe" if os.name == "nt" else name)
    return str(local) if local.is_file() else name
