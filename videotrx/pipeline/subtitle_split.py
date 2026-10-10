from typing import List, Tuple
import concurrent.futures
import json
import math

from videotrx import paths
from videotrx.pipeline.meaning_split import split_sentence
from videotrx.prompts import get_align_prompt, get_target_semantic_alignment_prompt
from videotrx.pipeline.subtitle_render import filter_chinese_punctuation
from rich.panel import Panel
from rich.console import Console
from rich.table import Table
from videotrx import config
from videotrx.llm import ask_gpt
from videotrx.utils import append_jsonl, except_handler, read_json, write_json
console = Console()

TARGET_ALIGNMENT_BATCH_SIZE = 5
TARGET_BOUNDARY_PUNCTUATION = set("，。！？；：,.!?;:…")
TARGET_BOUNDARY_CLOSERS = set("」』】）》〉〕”’)]}")
TARGET_ALIGNMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "target_parts": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "target_parts"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

# Aa研宋 中 ASCII 字符宽度，单位为汉字宽度（由字体 hmtx 字宽 / 汉字字宽得出）
TARGET_ASCII_WIDTHS = {
    ' ': 0.336, '!': 0.302, '"': 0.438, '#': 0.642, '$': 0.584, '%': 0.811, '&': 0.768, "'": 0.32,
    '(': 0.395, ')': 0.395, '*': 0.439, '+': 0.609, ',': 0.307, '-': 0.743, '.': 0.408, '/': 0.586,
    '0': 0.615, '1': 0.515, '2': 0.582, '3': 0.6, '4': 0.551, '5': 0.559, '6': 0.573, '7': 0.556,
    '8': 0.631, '9': 0.589, ':': 0.359, ';': 0.345, '<': 0.499, '=': 0.682, '>': 0.623, '?': 0.566,
    '@': 0.933, 'A': 0.597, 'B': 0.585, 'C': 0.616, 'D': 0.672, 'E': 0.556, 'F': 0.535, 'G': 0.644,
    'H': 0.72, 'I': 0.302, 'J': 0.34, 'K': 0.609, 'L': 0.551, 'M': 0.834, 'N': 0.665, 'O': 0.665,
    'P': 0.553, 'Q': 0.692, 'R': 0.582, 'S': 0.52, 'T': 0.618, 'U': 0.618, 'V': 0.627, 'W': 0.865,
    'X': 0.608, 'Y': 0.641, 'Z': 0.55, '[': 0.375, '\\': 0.751, ']': 0.375, '^': 0.634, '_': 0.582,
    '`': 0.72, 'a': 0.472, 'b': 0.547, 'c': 0.451, 'd': 0.535, 'e': 0.465, 'f': 0.274, 'g': 0.508,
    'h': 0.551, 'i': 0.248, 'j': 0.275, 'k': 0.49, 'l': 0.244, 'm': 0.85, 'n': 0.555, 'o': 0.485,
    'p': 0.526, 'q': 0.551, 'r': 0.342, 's': 0.405, 't': 0.33, 'u': 0.534, 'v': 0.445, 'w': 0.742,
    'x': 0.483, 'y': 0.48, 'z': 0.445, '{': 0.416, '|': 0.29, '}': 0.416, '~': 0.605,
}


def target_display_length(text) -> float:
    """按最终显示文本计算宽度（单位：汉字宽度）；ASCII 查表，其余字符（汉字、全角符号）算 1。"""
    text = filter_chinese_punctuation(str(text))
    return sum(TARGET_ASCII_WIDTHS.get(char, 1.0) for char in text)


def _is_han_character(char: str) -> bool:
    if not char:
        return False
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def detect_target_boundary_candidates(target: str) -> List[str]:
    """把目标字幕按可靠候选边界切成可相邻合并的原子单元。"""
    text = str(target).strip()
    if not text:
        return []

    boundaries = []
    i = 0
    text_length = len(text)

    while i < text_length:
        char = text[i]

        # 中文字符之间的空白可以作为边界；英文词组内部空格会保留在同一单元。
        if char.isspace():
            whitespace_end = i + 1
            while whitespace_end < text_length and text[whitespace_end].isspace():
                whitespace_end += 1
            if (
                i > 0
                and whitespace_end < text_length
                and _is_han_character(text[i - 1])
                and _is_han_character(text[whitespace_end])
            ):
                boundaries.append((i, whitespace_end))
            i = whitespace_end
            continue

        if char in TARGET_BOUNDARY_PUNCTUATION:
            punctuation_start = i
            punctuation_end = i + 1
            while (
                punctuation_end < text_length
                and text[punctuation_end] in TARGET_BOUNDARY_PUNCTUATION
            ):
                punctuation_end += 1

            # 小数、版本号、时间等内部 ASCII 标点保留在当前单元。
            punctuation_text = text[punctuation_start:punctuation_end]
            if (
                len(punctuation_text) == 1
                and punctuation_text in ".,:"
                and punctuation_start > 0
                and punctuation_end < text_length
                and text[punctuation_start - 1].isascii()
                and text[punctuation_start - 1].isalnum()
                and text[punctuation_end].isascii()
                and text[punctuation_end].isalnum()
            ):
                i = punctuation_end
                continue

            while (
                punctuation_end < text_length
                and text[punctuation_end] in TARGET_BOUNDARY_CLOSERS
            ):
                punctuation_end += 1

            next_content = punctuation_end
            while next_content < text_length and text[next_content].isspace():
                next_content += 1

            if (
                len(punctuation_text) == 1
                and punctuation_text in ".,:，。："
                and punctuation_start > 0
                and next_content < text_length
                and text[punctuation_start - 1].isdigit()
                and text[next_content].isdigit()
            ):
                i = next_content
                continue

            # 英文术语、型号、时间和 URL 内部的 ASCII 标点保留在当前单元。
            ascii_punctuation = all(mark in ".,!?;:" for mark in punctuation_text)
            if (
                ascii_punctuation
                and punctuation_start > 0
                and next_content < text_length
                and text[punctuation_start - 1].isascii()
                and text[punctuation_start - 1].isalnum()
                and text[next_content].isascii()
                and (
                    text[next_content].isalnum()
                    or text[next_content] in "/\\@#%&=_+-"
                )
            ):
                i = next_content
                continue

            if text[:punctuation_start].strip() and text[next_content:].strip():
                boundaries.append((punctuation_end, next_content))
            i = next_content
            continue

        i += 1

    if not boundaries:
        return []

    units = []
    cursor = 0
    for left_end, right_start in boundaries:
        if left_end < cursor:
            continue
        unit = text[cursor:left_end].strip()
        if unit:
            units.append(unit)
        cursor = max(cursor, right_start)

    tail = text[cursor:].strip()
    if tail:
        units.append(tail)

    return units if len(units) > 1 else []


def _locate_candidate_unit_spans(candidate_units: List[str], original_target: str):
    spans = []
    cursor = 0
    for unit in candidate_units:
        start = original_target.find(unit, cursor)
        if start < 0:
            return []
        end = start + len(unit)
        spans.append((start, end))
        cursor = end
    return spans


def _match_adjacent_candidate_units(
    target_parts: List[str],
    candidate_units: List[str],
    original_target: str,
) -> bool:
    """校验每段由相邻单元组成，并按顺序完整覆盖；连续时间段可重复同一组单元。"""
    if not target_parts or not candidate_units:
        return False

    original_target = str(original_target).strip()
    unit_spans = _locate_candidate_unit_spans(candidate_units, original_target)
    matching_ranges = []
    for target_part in target_parts:
        part_ranges = []
        for start in range(len(candidate_units)):
            combined = ""
            for end in range(start + 1, len(candidate_units) + 1):
                combined += candidate_units[end - 1]
                allowed_texts = {combined}
                if unit_spans:
                    original_slice = original_target[
                        unit_spans[start][0]:unit_spans[end - 1][1]
                    ].strip()
                    allowed_texts.add(original_slice)
                if target_part in allowed_texts:
                    part_ranges.append((start, end))
                if len(combined) > len(target_part):
                    break
        if not part_ranges:
            return False
        matching_ranges.append(part_ranges)

    states = {(start, end) for start, end in matching_ranges[0] if start == 0}
    for part_ranges in matching_ranges[1:]:
        next_states = set()
        for start, end in part_ranges:
            for previous_start, previous_end in states:
                repeats_same_units = (
                    start == previous_start and end == previous_end
                )
                continues_from_previous = start == previous_end
                if repeats_same_units or continues_from_previous:
                    next_states.add((start, end))
                    break
        states = next_states
        if not states:
            return False

    return any(end == len(candidate_units) for _, end in states)


def _validate_target_alignment_response(response_data, expected_items):
    if not isinstance(response_data, dict) or not isinstance(response_data.get("items"), list):
        return False, "Missing required list: `items`", {}
    if set(response_data) != {"items"}:
        return False, "Unexpected fields in target alignment response", {}

    response_items = response_data["items"]
    if len(response_items) != len(expected_items):
        return False, "Target alignment item count mismatch", {}

    response_ids = [item.get("id") if isinstance(item, dict) else None for item in response_items]
    expected_ids = [item["id"] for item in expected_items]
    if response_ids != expected_ids:
        return False, "Target alignment item order or ids mismatch", {}

    expected_by_id = {item["id"]: item for item in expected_items}
    parts_by_id = {}

    for item in response_items:
        if not isinstance(item, dict):
            return False, "Target alignment item must be an object", {}
        if set(item) != {"id", "target_parts"}:
            return False, "Unexpected fields in target alignment item", {}

        item_id = item.get("id")
        if not isinstance(item_id, int) or isinstance(item_id, bool):
            return False, "Target alignment id must be an integer", {}
        if item_id not in expected_by_id or item_id in parts_by_id:
            return False, f"Unexpected or duplicate target alignment id: {item_id}", {}

        target_parts = item.get("target_parts")
        expected = expected_by_id[item_id]
        if not isinstance(target_parts, list):
            return False, f"target_parts must be a list for id {item_id}", {}
        if len(target_parts) != len(expected["source_parts"]):
            return False, f"target_parts count mismatch for id {item_id}", {}
        if any(not isinstance(part, str) or not part.strip() for part in target_parts):
            return False, f"target_parts contains an empty or non-string value for id {item_id}", {}

        clean_parts = [part.strip() for part in target_parts]
        if not _match_adjacent_candidate_units(
            clean_parts,
            expected["target_boundary_candidates"],
            expected["target"],
        ):
            return False, f"target_parts changed candidate order or content for id {item_id}", {}
        parts_by_id[item_id] = clean_parts

    if set(parts_by_id) != set(expected_by_id):
        return False, "Target alignment ids mismatch", {}

    return True, "Target semantic alignment completed", parts_by_id

def align_subs(src_sub: str, tr_sub: str, src_part: str, target_max_length: int) -> Tuple[List[str], List[str]]:
    align_prompt = get_align_prompt(src_sub, tr_sub, src_part, target_max_length)
    src_parts = src_part.split('\n')
    expected_part_count = len(src_parts)

    if expected_part_count < 2:
        raise ValueError("Source subtitle split must contain at least two parts")
    
    def valid_align(response_data):
        if not isinstance(response_data, dict) or 'align' not in response_data:
            return {"status": "error", "message": "Missing required key: `align`"}
        align_items = response_data['align']
        if not isinstance(align_items, list):
            return {"status": "error", "message": "`align` must be a list"}
        if len(align_items) != expected_part_count:
            return {
                "status": "error",
                "message": (
                    "Aligned target part count mismatch: "
                    f"expected {expected_part_count}, got {len(align_items)}"
                ),
            }

        for index, item in enumerate(align_items, start=1):
            target_key = f'target_part_{index}'
            if not isinstance(item, dict) or target_key not in item:
                return {
                    "status": "error",
                    "message": f"Missing required key: `{target_key}`",
                }
            if not isinstance(item[target_key], str) or not item[target_key].strip():
                return {
                    "status": "error",
                    "message": f"`{target_key}` must be a non-empty string",
                }

        return {"status": "success", "message": "Align completed"}

    # 局部导入以避免模块级循环依赖，超宽重译沿用全文润色设置。
    from videotrx.pipeline.polish import _load_single_pass_full_polish_api_settings

    # 每段的键名随序号变化（src_part_i / target_part_i），用 anyOf 列出各序号的固定结构；
    # 数组长度与顺序由 valid_align 校验
    align_item_variants = [
        {
            "type": "object",
            "properties": {
                f"src_part_{i}": {"type": "string"},
                f"target_part_{i}": {"type": "string"},
            },
            "required": [f"src_part_{i}", f"target_part_{i}"],
            "additionalProperties": False,
        }
        for i in range(1, expected_part_count + 1)
    ]
    align_schema = {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "align": {"type": "array", "items": {"anyOf": align_item_variants}},
        },
        "required": ["analysis", "align"],
        "additionalProperties": False,
    }

    parsed = ask_gpt(
        align_prompt,
        resp_type='json',
        valid_def=valid_align,
        log_title='align_subs',
        api_settings=_load_single_pass_full_polish_api_settings(),
        json_schema=align_schema,
    )
    align_data = parsed['align']
    tr_parts = [item[f'target_part_{i+1}'].strip() for i, item in enumerate(align_data)]
    
    table = Table(title="🔗 Aligned parts")
    table.add_column("Language", style="cyan")
    table.add_column("Parts", style="magenta")
    table.add_row("SRC_LANG", "\n".join(src_parts))
    table.add_row("TARGET_LANG", "\n".join(tr_parts))
    console.print(table)
    
    return src_parts, tr_parts

def _align_target_semantics_batch(batch_items):
    def valid_target_alignment(response_data):
        valid, message, _ = _validate_target_alignment_response(response_data, batch_items)
        return {
            "status": "success" if valid else "error",
            "message": message,
        }

    try:
        prompt = get_target_semantic_alignment_prompt(batch_items)
        response_data = ask_gpt(
            prompt,
            resp_type='json',
            valid_def=valid_target_alignment,
            log_title='align_target_semantics',
            json_schema=TARGET_ALIGNMENT_SCHEMA,
        )
        valid, message, parts_by_id = _validate_target_alignment_response(
            response_data,
            batch_items,
        )
        if not valid:
            return {}, message
        return parts_by_id, ""
    except Exception as error:
        return {}, f"{type(error).__name__}: {error}"


def _serialize_decision_parts(parts) -> str:
    return json.dumps(parts, ensure_ascii=False)


def _append_target_rewrite_changelog(records):
    """按原始行号汇总本轮采用的片段，沿用润色日志中的初译基线。"""
    try:
        rewritten_rows = {record["row"] for record in records if record["stage"] == "target_rewrite"}
        if not rewritten_rows:
            return

        with open(paths.POLISH_CHANGELOG, "r", encoding="utf-8") as f:
            baseline = [json.loads(line) for line in f if line.strip()]
        baseline_by_row = {
            int(record["row"]): record
            for record in baseline
            if record["stage"] in {"polish", "polish_fallback"}
        }
        parts_by_row = {}
        for record in records:
            if record["row"] in rewritten_rows:
                parts_by_row.setdefault(record["row"], []).extend(json.loads(record["target_parts"]))

        updates = []
        for row, parts in parts_by_row.items():
            base = baseline_by_row[row]
            final = "\n".join(parts)
            updates.append({
                "row": row,
                "source": base["source"],
                "draft": base["draft"],
                "final": final,
                "stage": "target_rewrite",
                "changed": final != base["draft"],
                "status": base["status"],
                "note": "; ".join(filter(None, [
                    base["note"],
                    f"拆分第 {records[0]['attempt']} 轮，采用 {len(parts)} 个片段",
                ])),
            })
        append_jsonl(paths.POLISH_CHANGELOG, updates)
    except Exception as error:
        console.print(f"[yellow]Warning: Failed to append target rewrite changelog: {error}[/yellow]")


def split_align_subs(
    src_lines: List[str],
    tr_lines: List[str],
    attempt: int = 1,
    decision_records=None,
    original_row_ids=None,
):
    subtitle_set = config.get("subtitle")
    SOURCE_MAX_LENGTH = subtitle_set["source_max_length"]
    TARGET_MAX_LENGTH = subtitle_set["target_max_length"]

    if original_row_ids is None:
        original_row_ids = list(range(1, len(src_lines) + 1))
    if len(original_row_ids) != len(src_lines):
        raise ValueError("Original row id count must match source line count")

    result_src_lines = list(src_lines)
    result_tr_lines = list(tr_lines)
    to_split = []
    round_records = []
    for i, (src, tr) in enumerate(zip(src_lines, tr_lines)):
        src, tr = str(src), str(tr)
        src_over = len(src) > SOURCE_MAX_LENGTH
        target_length = target_display_length(tr)
        # 小数部分不计，25.x 视为 25
        tr_over = math.floor(target_length) > TARGET_MAX_LENGTH
        mode = "keep"
        if src_over or tr_over:
            # 翻译超长时继续使用原有的拆分与对齐流程。
            mode = "split+align_translation" if tr_over else "split_source_only"

        round_records.append(
            {
                "attempt": attempt,
                "line_index": i,
                "row": original_row_ids[i],
                "stage": "target_rewrite" if tr_over else "",
                "source": src,
                "target": tr,
                "source_length": len(src),
                "target_display_length": target_length,
                "source_max_length": SOURCE_MAX_LENGTH,
                "target_max_length": TARGET_MAX_LENGTH,
                "source_over": src_over,
                "target_over": tr_over,
                "mode": mode,
                "target_boundary_candidates": _serialize_decision_parts([]),
                "decision": "keep",
                "fallback_reason": "",
                "source_parts": _serialize_decision_parts([src]),
                "target_parts": _serialize_decision_parts([tr]),
            }
        )

        if src_over or tr_over:
            to_split.append(
                {
                    "index": i,
                    "mode": mode,
                    "src_over": src_over,
                    "tr_over": tr_over,
                    "source": src,
                    "target": tr,
                }
            )
            table = Table(title=f"📏 Line {i} needs to be split")
            table.add_column("Type", style="cyan")
            table.add_column("Content", style="magenta")
            table.add_row("Source Line", src)
            table.add_row("Target Line", tr)
            table.add_row("Trigger", f"src_over={src_over}, tr_over={tr_over}, mode={mode}")
            console.print(table)
    
    @except_handler("Error in split_align_subs")
    def process(split_item):
        i = split_item["index"]
        mode = split_item["mode"]
        source = split_item["source"]
        target = split_item["target"]
        split_src = split_sentence(source, num_parts=2, char_limit=SOURCE_MAX_LENGTH).strip()

        if mode == "split+align_translation":
            src_parts, tr_parts = align_subs(source, target, split_src, TARGET_MAX_LENGTH)
            return {
                "index": i,
                "mode": mode,
                "source_parts": src_parts,
                "target_parts": tr_parts,
                "target_boundary_candidates": [],
                "decision": "split+align_translation",
                "fallback_reason": "",
            }
        elif mode == "split_source_only":
            src_parts = [part.strip() for part in split_src.split("\n") if str(part).strip()]
            if not src_parts:
                src_parts = [source.strip()]
            return {
                "index": i,
                "mode": mode,
                "source_parts": src_parts,
                "target": target.strip(),
                "target_boundary_candidates": detect_target_boundary_candidates(target),
            }
        else:
            raise ValueError(f"Unsupported split mode: {mode}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.get("max_workers")) as executor:
        processed_items = list(executor.map(process, to_split))

    semantic_items = [
        {
            "id": item["index"],
            "source": str(src_lines[item["index"]]),
            "source_parts": item["source_parts"],
            "target": item["target"],
            "target_boundary_candidates": item["target_boundary_candidates"],
        }
        for item in processed_items
        if (
            item["mode"] == "split_source_only"
            and len(item["source_parts"]) > 1
            and item["target_boundary_candidates"]
        )
    ]
    semantic_batches = [
        semantic_items[start:start + TARGET_ALIGNMENT_BATCH_SIZE]
        for start in range(0, len(semantic_items), TARGET_ALIGNMENT_BATCH_SIZE)
    ]

    semantic_parts_by_id = {}
    semantic_errors_by_id = {}
    if semantic_batches:
        with concurrent.futures.ThreadPoolExecutor(max_workers=config.get("max_workers")) as executor:
            batch_results = list(executor.map(_align_target_semantics_batch, semantic_batches))

        for batch_items, (parts_by_id, error_message) in zip(semantic_batches, batch_results):
            if error_message:
                for batch_item in batch_items:
                    semantic_errors_by_id[batch_item["id"]] = error_message
                console.print(
                    "[yellow]Target semantic alignment failed for batch; "
                    "using the complete target for each source part.[/yellow]"
                )
            else:
                semantic_parts_by_id.update(parts_by_id)

    for item in processed_items:
        i = item["index"]
        source_parts = item["source_parts"]

        if item["mode"] == "split_source_only":
            target = item["target"]
            candidates = item["target_boundary_candidates"]
            if len(source_parts) <= 1:
                target_parts = [target]
                decision = "repeat_target_without_source_split"
                fallback_reason = "source split produced one part"
            elif i in semantic_parts_by_id:
                target_parts = semantic_parts_by_id[i]
                decision = "align_target_semantics"
                fallback_reason = ""
            else:
                target_parts = [target] * len(source_parts)
                if candidates:
                    decision = "repeat_target_after_alignment_fallback"
                    fallback_reason = semantic_errors_by_id.get(i, "missing validated batch item")
                else:
                    decision = "repeat_target_without_candidates"
                    fallback_reason = "no target boundary candidates"
        else:
            target_parts = item["target_parts"]
            candidates = item["target_boundary_candidates"]
            decision = item["decision"]
            fallback_reason = item["fallback_reason"]

        result_src_lines[i] = source_parts
        result_tr_lines[i] = target_parts

        record = round_records[i]
        record["target_boundary_candidates"] = _serialize_decision_parts(candidates)
        record["decision"] = decision
        record["fallback_reason"] = fallback_reason
        record["source_parts"] = _serialize_decision_parts(source_parts)
        record["target_parts"] = _serialize_decision_parts(target_parts)

    if decision_records is not None:
        decision_records.extend(round_records)
    _append_target_rewrite_changelog(round_records)

    # 原始行号随片段展开，原地更新列表以保留现有二元返回接口。
    original_row_ids[:] = [
        row
        for row, parts in zip(original_row_ids, result_src_lines)
        for _ in range(len(parts) if isinstance(parts, list) else 1)
    ]

    # Flatten `src_lines` and `tr_lines`
    src_lines = [item for sublist in result_src_lines for item in (sublist if isinstance(sublist, list) else [sublist])]
    tr_lines = [item for sublist in result_tr_lines for item in (sublist if isinstance(sublist, list) else [sublist])]
    
    return src_lines, tr_lines

def split_for_sub_main():
    console.print("[bold green]🚀 Start splitting subtitles...[/bold green]")
    
    rows = read_json(paths.TRANSLATION)
    src = [row['source'] for row in rows]
    trans = [row['translation'] for row in rows]
    
    subtitle_set = config.get("subtitle")
    SOURCE_MAX_LENGTH = subtitle_set["source_max_length"]
    TARGET_MAX_LENGTH = subtitle_set["target_max_length"]
    decision_records = []
    original_row_ids = list(range(1, len(src) + 1))

    for attempt in range(3):  # 多次切割
        console.print(Panel(f"🔄 Split attempt {attempt + 1}", expand=False))
        split_src, split_trans = split_align_subs(
            src.copy(),
            trans,
            attempt=attempt + 1,
            decision_records=decision_records,
            original_row_ids=original_row_ids,
        )
        
        # 检查是否所有字幕都符合长度要求
        if all(len(src) <= SOURCE_MAX_LENGTH for src in split_src) and \
           all(math.floor(target_display_length(tr)) <= TARGET_MAX_LENGTH for tr in split_trans):
            break
        
        # 更新源数据继续下一轮分割
        src, trans = split_src, split_trans

    write_json(
        paths.SUBTITLE_LINES,
        [{'source': s, 'translation': tr} for s, tr in zip(split_src, split_trans)],
    )
    write_json(paths.SUBTITLE_SPLIT_DECISIONS, decision_records)
