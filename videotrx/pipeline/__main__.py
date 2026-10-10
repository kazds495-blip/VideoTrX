"""字幕流程入口：python -m videotrx.pipeline [--audio PATH]

从最后一个输出已存在的阶段之后继续运行。stdout 每行一条：
@progress <transcribe|pipeline> <0-100>、@stage <文本>、@done <文本>、@error <文本>，其余行是日志。
"""
from __future__ import annotations

import argparse
import importlib
import sys
import traceback
from pathlib import Path

from videotrx import paths

# (阶段, 输出检查点, 显示名, 流程进度)
STAGES = (
    ("transcribe", paths.TRANSCRIPT, "转写", None),
    ("english_correction", paths.ENGLISH_CORRECTION_DONE, "英文校正", 10),
    ("nlp_split", paths.NLP_SPLIT, "NLP 断句", 25),
    ("meaning_split", paths.MEANING_SPLIT, "语义断句", 40),
    ("terminology", paths.TERMINOLOGY, "摘要与术语", 55),
    ("translate", paths.TRANSLATION, "翻译与润色", 70),
    ("subtitle_split", paths.SUBTITLE_LINES, "字幕切分", 85),
    ("render", paths.FINAL_ASS, "生成字幕", 95),
)

# 阶段 → (模块, 入口函数)；运行到该阶段时才 import
ENTRY_POINTS = {
    "english_correction": ("english_correction", "correct_english_asr_tokens"),
    "nlp_split": ("nlp_split", "split_by_spacy"),
    "meaning_split": ("meaning_split", "split_sentences_by_meaning"),
    "terminology": ("terminology", "get_summary"),
    "translate": ("translate", "translate_all"),
    "subtitle_split": ("subtitle_split", "split_for_sub_main"),
    "render": ("subtitle_render", "align_timestamp_main"),
}


def emit(kind: str, text) -> None:
    print(f"@{kind} {text}", flush=True)


def first_pending_stage() -> int:
    """从后往前找最后一个输出已存在的阶段，返回它之后那个阶段的下标。"""
    for index in range(len(STAGES) - 1, -1, -1):
        if STAGES[index][1].exists():
            return index + 1
    return 0


def find_audio(audio: str | None) -> Path:
    from videotrx.pipeline import transcribe

    if audio:
        return Path(audio)
    candidates = [
        path
        for path in paths.OUTPUT_DIR.glob("*")
        if path.is_file()
        and path.stem.endswith("_audio")
        and path.suffix.lower() in transcribe.SUPPORTED_SUFFIXES
    ]
    if not candidates:
        raise RuntimeError("未找到音频文件，请先点开始")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def run_stage(name: str, audio: str | None) -> None:
    if name == "transcribe":
        from videotrx.pipeline import transcribe

        transcribe.run(find_audio(audio), progress=lambda value: emit("progress", f"transcribe {value}"))
        return
    module_name, function_name = ENTRY_POINTS[name]
    module = importlib.import_module(f"videotrx.pipeline.{module_name}")
    getattr(module, function_name)()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m videotrx.pipeline")
    parser.add_argument("--audio", help="要转写的音频文件；不给时取 output 下最新的 *_audio 文件")
    args = parser.parse_args(argv)

    start = first_pending_stage()
    if start > 0:
        emit("progress", "transcribe 100")
    if start == len(STAGES):
        emit("progress", "pipeline 100")
        emit("done", "字幕已存在，无需重新生成")
        return 0

    print(f"从「{STAGES[start][2]}」开始", flush=True)
    for name, _checkpoint, label, progress in STAGES[start:]:
        emit("stage", label)
        if progress is not None:
            emit("progress", f"pipeline {progress}")
        run_stage(name, args.audio)

    emit("progress", "pipeline 100")
    emit("done", "字幕生成完成")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        traceback.print_exc(file=sys.stdout)
        message = " ".join(str(error).split()) or type(error).__name__
        emit("error", message)
        sys.exit(1)
