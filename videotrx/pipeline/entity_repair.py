import concurrent.futures
import json
from datetime import datetime
from typing import Dict, List, Tuple

from rich import print as rprint

from videotrx import config, paths
from videotrx.language import get_joiner, source_language
from videotrx.llm import ask_json
from videotrx.prompts import get_rough_split_entity_repair_prompt
from videotrx.utils import append_jsonl, write_json

LOG_TITLE = "rough_split_entity_repair"


def _normalize_space(text: str) -> str:
    return " ".join(str(text).split()).strip()


def _normalize_alnum(text: str) -> str:
    chars = [ch.lower() for ch in str(text) if ch.isalnum()]
    return "".join(chars)


def _build_audit_row(run_id: str, status: str = "skipped", skip_reason: str = "", **overrides):
    row = {
        "run_id": run_id,
        "status": status,
        "skip_reason": skip_reason,
        "pair_id": "",
        "left_words": "",
        "right_words": "",
        "entity": "",
        "type": "",
        "confidence": "",
        "reason": "",
        "direction": "",
        "before_left": "",
        "before_right": "",
        "after_left": "",
        "after_right": "",
    }
    for key, value in overrides.items():
        if key in row:
            row[key] = value
    return row


def _chunk_list(items: List[Dict], chunk_size: int):
    for i in range(0, len(items), chunk_size):
        yield items[i : i + chunk_size]


def _build_boundary_pairs(lines: List[str], window_words: int) -> List[Dict]:
    pairs = []
    for i in range(len(lines) - 1):
        left = _normalize_space(lines[i])
        right = _normalize_space(lines[i + 1])
        if not left or not right:
            continue
        left_tokens = left.split()
        right_tokens = right.split()
        pairs.append(
            {
                "pair_id": i,
                "left_line": left,
                "right_line": right,
                "left_tail": " ".join(left_tokens[-window_words:]),
                "right_head": " ".join(right_tokens[:window_words]),
                "left_len": len(left_tokens),
                "right_len": len(right_tokens),
            }
        )
    return pairs


REPAIR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair_id": {"type": "integer"},
                    "left_words": {"type": "integer"},
                    "right_words": {"type": "integer"},
                    "entity": {"type": "string"},
                    "type": {"type": "string"},
                    "confidence": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": [
                    "pair_id", "left_words", "right_words", "entity", "type", "confidence", "reason",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["analysis", "corrections"],
    "additionalProperties": False,
}


def _valid_repair_response(response_data):
    if "corrections" not in response_data:
        return {"status": "error", "message": "Missing required key: `corrections`"}
    if not isinstance(response_data["corrections"], list):
        return {"status": "error", "message": "`corrections` must be a list"}

    for i, item in enumerate(response_data["corrections"]):
        if not isinstance(item, dict):
            return {"status": "error", "message": f"`corrections[{i}]` must be an object"}
        for key in ("pair_id", "left_words", "right_words", "entity"):
            if key not in item:
                return {"status": "error", "message": f"Missing key in `corrections[{i}]`: `{key}`"}
    return {"status": "success", "message": "Rough split entity repair response validated"}


def _confidence_rank(value: str) -> int:
    normalized = str(value).strip().lower().replace(" ", "_")
    ranking = {
        "very_high": 4,
        "high": 3,
        "medium": 2,
        "low": 1,
    }
    return ranking.get(normalized, 0)


def _score_lengths(left_len: int, right_len: int, max_split_length: int) -> Tuple[int, int, int]:
    overflow = max(0, left_len - max_split_length) + max(0, right_len - max_split_length)
    return overflow, max(left_len, right_len), abs(left_len - right_len)


def _choose_direction(
    left_len: int,
    right_len: int,
    left_words: int,
    right_words: int,
    max_split_length: int,
) -> str:
    options = []

    # Move right prefix to the left line tail.
    append_left_len = left_len + right_words
    append_right_len = right_len - right_words
    if append_right_len > 0:
        options.append(
            (
                "append_right_to_left",
                _score_lengths(append_left_len, append_right_len, max_split_length),
            )
        )

    # Move left suffix to the right line head.
    prepend_left_len = left_len - left_words
    prepend_right_len = right_len + left_words
    if prepend_left_len > 0:
        options.append(
            (
                "prepend_left_to_right",
                _score_lengths(prepend_left_len, prepend_right_len, max_split_length),
            )
        )

    if not options:
        return ""
    options.sort(key=lambda item: item[1])
    return options[0][0]


def _collect_suggestions(boundary_pairs: List[Dict]) -> List[Dict]:
    max_pairs_per_request = config.to_int(
        config.get("entity_repair.max_pairs_per_request", 120),
        default=120,
        min_value=10,
    )
    max_workers = config.to_int(
        config.get(
            "entity_repair.max_workers",
            config.get("max_workers", 4),
        ),
        default=1,
        min_value=1,
    )
    chunks = list(_chunk_list(boundary_pairs, max_pairs_per_request))

    def fetch_chunk_suggestions(chunk):
        prompt = get_rough_split_entity_repair_prompt(
            json.dumps(chunk, ensure_ascii=False, indent=2)
        )
        try:
            response = ask_json(
                prompt,
                REPAIR_RESPONSE_SCHEMA,
                valid_def=_valid_repair_response,
                log_title=LOG_TITLE,
                section="entity_repair",
            )
        except Exception as e:
            rprint(f"[yellow]Entity repair chunk skipped due to LLM error: {e}[/yellow]")
            return []
        return response.get("corrections", [])

    if max_workers <= 1 or len(chunks) <= 1:
        chunk_results = [fetch_chunk_suggestions(chunk) for chunk in chunks]
    else:
        # 仅并发请求；executor.map 保证结果与 chunk 原顺序一一对应
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            chunk_results = list(executor.map(fetch_chunk_suggestions, chunks))

    suggestions = []
    for chunk_corrections in chunk_results:
        if chunk_corrections:
            suggestions.extend(chunk_corrections)
    return suggestions


def _deduplicate_by_pair(suggestions: List[Dict]) -> List[Dict]:
    best = {}
    for item in suggestions:
        try:
            pair_id = int(item.get("pair_id"))
        except Exception:
            continue
        score = _confidence_rank(item.get("confidence", ""))
        prev = best.get(pair_id)
        if prev is None or score > prev["score"]:
            best[pair_id] = {"score": score, "item": item}
    return [best[k]["item"] for k in sorted(best.keys())]


def _apply_suggestions(lines: List[str], suggestions: List[Dict], run_id: str):
    max_fragment_words = config.to_int(
        config.get("entity_repair.max_fragment_words", 4),
        default=4,
        min_value=1,
        max_value=8,
    )
    max_split_length = config.to_int(
        config.get("entity_repair.score_base_words", 20),
        default=20,
        min_value=5,
    )

    audit_rows = []
    applied = 0

    for item in _deduplicate_by_pair(suggestions):
        record = _build_audit_row(
            run_id,
            status="skipped",
            skip_reason="",
            pair_id=item.get("pair_id"),
            left_words=item.get("left_words"),
            right_words=item.get("right_words"),
            entity=_normalize_space(item.get("entity", "")),
            type=str(item.get("type", "")).strip(),
            confidence=str(item.get("confidence", "")).strip().lower(),
            reason=str(item.get("reason", "")).strip(),
        )

        try:
            pair_id = int(item.get("pair_id"))
            left_words = int(item.get("left_words"))
            right_words = int(item.get("right_words"))
        except Exception:
            record["skip_reason"] = "invalid_numeric_fields"
            audit_rows.append(record)
            continue

        if not (0 <= pair_id < len(lines) - 1):
            record["skip_reason"] = "pair_id_out_of_range"
            audit_rows.append(record)
            continue
        if left_words <= 0 or right_words <= 0:
            record["skip_reason"] = "non_positive_fragment_size"
            audit_rows.append(record)
            continue
        if left_words > max_fragment_words or right_words > max_fragment_words:
            record["skip_reason"] = "fragment_size_exceeds_limit"
            audit_rows.append(record)
            continue
        if _confidence_rank(item.get("confidence", "")) < _confidence_rank("high"):
            record["skip_reason"] = "low_confidence"
            audit_rows.append(record)
            continue

        left_tokens = _normalize_space(lines[pair_id]).split()
        right_tokens = _normalize_space(lines[pair_id + 1]).split()
        if len(left_tokens) < left_words or len(right_tokens) < right_words:
            record["skip_reason"] = "line_too_short_for_fragment"
            audit_rows.append(record)
            continue

        left_fragment = left_tokens[-left_words:]
        right_fragment = right_tokens[:right_words]
        entity_boundary = " ".join(left_fragment + right_fragment).strip()
        entity_target = _normalize_space(item.get("entity", ""))
        if entity_target:
            # Hard guard: ensure returned entity matches boundary fragments.
            if _normalize_space(entity_target).lower() != entity_boundary.lower():
                if _normalize_alnum(entity_target) != _normalize_alnum(entity_boundary):
                    record["skip_reason"] = "entity_mismatch_with_boundary"
                    audit_rows.append(record)
                    continue

        direction = _choose_direction(
            left_len=len(left_tokens),
            right_len=len(right_tokens),
            left_words=left_words,
            right_words=right_words,
            max_split_length=max_split_length,
        )
        if not direction:
            record["skip_reason"] = "no_safe_direction"
            audit_rows.append(record)
            continue

        before_left = " ".join(left_tokens)
        before_right = " ".join(right_tokens)

        if direction == "append_right_to_left":
            new_left_tokens = left_tokens + right_tokens[:right_words]
            new_right_tokens = right_tokens[right_words:]
        else:
            new_left_tokens = left_tokens[:-left_words]
            new_right_tokens = left_tokens[-left_words:] + right_tokens

        if not new_left_tokens or not new_right_tokens:
            record["skip_reason"] = "empty_line_after_move"
            audit_rows.append(record)
            continue

        lines[pair_id] = " ".join(new_left_tokens)
        lines[pair_id + 1] = " ".join(new_right_tokens)

        record["status"] = "applied"
        record["direction"] = direction
        record["before_left"] = before_left
        record["before_right"] = before_right
        record["after_left"] = lines[pair_id]
        record["after_right"] = lines[pair_id + 1]
        audit_rows.append(record)
        applied += 1

    return applied, audit_rows


def _write_changelog(rows: List[Dict]):
    if not rows:
        return None
    logged_at = datetime.now().isoformat(timespec="seconds")
    append_jsonl(paths.ENTITY_REPAIR_CHANGELOG, [{"logged_at": logged_at, **row} for row in rows])
    return paths.ENTITY_REPAIR_CHANGELOG


def repair_rough_split_entities(lines: List[str]) -> List[str]:
    if not config.get_bool("entity_repair.enabled", False):
        rprint("[dim]Rough split entity repair disabled, skip.[/dim]")
        return lines

    language = ""
    try:
        language = source_language()
        joiner = get_joiner(language)
    except Exception:
        joiner = None

    if config.get_bool("entity_repair.only_when_space_joiner", True):
        if joiner != " ":
            rprint(
                f"[dim]Rough split entity repair skipped (language={language}, joiner='{joiner}').[/dim]"
            )
            return lines

    lines = list(lines)
    if len(lines) < 2:
        rprint("[dim]Rough split entity repair skipped (not enough lines).[/dim]")
        return lines

    boundary_window_words = config.to_int(
        config.get("entity_repair.boundary_window_words", 8),
        default=8,
        min_value=2,
        max_value=20,
    )
    boundary_pairs = _build_boundary_pairs(lines, window_words=boundary_window_words)
    if not boundary_pairs:
        rprint("[dim]Rough split entity repair skipped (no valid boundaries).[/dim]")
        return lines

    rprint(f"[cyan]Running rough split entity repair on {len(boundary_pairs)} boundaries...[/cyan]")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    suggestions = _collect_suggestions(boundary_pairs)
    if not suggestions:
        log_path = _write_changelog(
            [
                _build_audit_row(
                    run_id,
                    status="no_changes",
                    skip_reason="no_corrections_returned",
                    reason="LLM returned corrections=[]",
                )
            ]
        )
        if log_path:
            rprint(f"[dim]Entity repair changelog: {log_path}[/dim]")
        rprint("[green]No rough split entity repairs suggested.[/green]")
        return lines

    if not paths.NLP_SPLIT_BEFORE_REPAIR.exists():
        write_json(paths.NLP_SPLIT_BEFORE_REPAIR, lines)
        rprint(f"[dim]Backup created: {paths.NLP_SPLIT_BEFORE_REPAIR}[/dim]")

    applied, audit_rows = _apply_suggestions(lines, suggestions, run_id=run_id)

    log_path = _write_changelog(audit_rows)
    if log_path:
        rprint(f"[dim]Entity repair changelog: {log_path}[/dim]")

    if applied == 0:
        rprint("[yellow]Entity repairs returned, but no safe changes were applied.[/yellow]")
        return lines

    rprint(f"[green]Applied {applied} rough split entity repairs[/green]")
    return lines
