import json
from datetime import datetime
from typing import Dict, List

import pandas as pd
from rich import print as rprint

from videotrx import config, paths
from videotrx.language import source_language
from videotrx.llm import ask_json
from videotrx.prompts import get_english_correction_prompt
from videotrx.utils import append_jsonl, read_json, write_json

_COLLOQUIAL_FORMS = {
    "gonna",
    "wanna",
    "gotta",
    "kinda",
    "sorta",
    "ain't",
    "y'all",
}


def _start_key(value) -> str:
    try:
        return f"{float(value):.6f}"
    except Exception:
        return str(value)


def _normalize_word(text: str) -> str:
    return str(text).strip()


def _build_audit_row(run_id: str, status: str = "skipped", skip_reason: str = "", **overrides):
    row = {
        "run_id": run_id,
        "status": status,
        "skip_reason": skip_reason,
        "start_key": "",
        "source": "",
        "target": "",
        "confidence": "",
        "type": "",
        "reason": "",
        "row_index": "",
        "row_start": "",
        "before": "",
        "after": "",
    }
    for key, value in overrides.items():
        if key in row:
            row[key] = value
    return row


def _build_tokens(df: pd.DataFrame) -> List[Dict]:
    tokens = []
    for _, row in df.iterrows():
        word = _normalize_word(row["text"])
        if not word:
            continue
        tokens.append(
            {
                "start_key": _start_key(row["start"]),
                "start": float(row["start"]),
                "word": word,
            }
        )
    return tokens


CORRECTION_ITEM_FIELDS = ("start_key", "source", "target", "type", "confidence", "reason")
CORRECTION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {field: {"type": "string"} for field in CORRECTION_ITEM_FIELDS},
                "required": list(CORRECTION_ITEM_FIELDS),
                "additionalProperties": False,
            },
        },
    },
    "required": ["analysis", "corrections"],
    "additionalProperties": False,
}


def _valid_correction_response(response_data):
    if "corrections" not in response_data:
        return {"status": "error", "message": "Missing required key: `corrections`"}
    if not isinstance(response_data["corrections"], list):
        return {"status": "error", "message": "`corrections` must be a list"}

    for i, item in enumerate(response_data["corrections"]):
        if not isinstance(item, dict):
            return {"status": "error", "message": f"`corrections[{i}]` must be an object"}
        for key in ("start_key", "source", "target"):
            if key not in item:
                return {"status": "error", "message": f"Missing key in `corrections[{i}]`: `{key}`"}
    return {"status": "success", "message": "English correction response validated"}


def _apply_corrections(df: pd.DataFrame, corrections: List[Dict], run_id: str):
    start_to_indices = {}
    for idx, row in df.iterrows():
        key = _start_key(row["start"])
        start_to_indices.setdefault(key, []).append(idx)

    used_indices = set()
    applied_count = 0
    audit_rows = []
    for item in corrections:
        start_key = str(item.get("start_key", "")).strip()
        source = _normalize_word(item.get("source", ""))
        target = _normalize_word(item.get("target", ""))
        confidence = str(item.get("confidence", "")).strip().lower()
        reason = str(item.get("reason", "")).strip()
        corr_type = str(item.get("type", "")).strip()

        record = _build_audit_row(
            run_id,
            status="skipped",
            skip_reason="",
            start_key=start_key,
            source=source,
            target=target,
            confidence=confidence,
            type=corr_type,
            reason=reason,
        )

        if not start_key or not source or not target:
            record["skip_reason"] = "missing_required_fields"
            audit_rows.append(record)
            continue
        if source == target:
            record["skip_reason"] = "source_equals_target"
            audit_rows.append(record)
            continue
        if " " in source or " " in target:
            # Enforce token-level replacement only.
            record["skip_reason"] = "non_token_replacement"
            audit_rows.append(record)
            continue
        if source.lower() in _COLLOQUIAL_FORMS:
            # Hard guard: do not normalize spoken colloquial forms.
            record["skip_reason"] = "colloquial_form_guard"
            audit_rows.append(record)
            continue
        # If confidence is provided, apply only high confidence corrections.
        if confidence and confidence not in {"high", "very_high", "very high"}:
            record["skip_reason"] = "low_confidence"
            audit_rows.append(record)
            continue

        candidates = start_to_indices.get(start_key, [])
        idx = next((x for x in candidates if x not in used_indices), None)
        if idx is None:
            record["skip_reason"] = "start_key_not_found_or_already_used"
            audit_rows.append(record)
            continue

        current = _normalize_word(df.at[idx, "text"])
        if current != source:
            # Safety gate: only replace exact source token.
            record["skip_reason"] = "source_mismatch_with_current_token"
            record["row_index"] = int(idx)
            record["row_start"] = _start_key(df.at[idx, "start"])
            record["before"] = current
            audit_rows.append(record)
            continue

        df.at[idx, "text"] = target
        used_indices.add(idx)
        applied_count += 1
        record["status"] = "applied"
        record["row_index"] = int(idx)
        record["row_start"] = _start_key(df.at[idx, "start"])
        record["before"] = current
        record["after"] = target
        audit_rows.append(record)

    return applied_count, audit_rows


def _write_changelog(rows: List[Dict]):
    if not rows:
        return None
    logged_at = datetime.now().isoformat(timespec="seconds")
    append_jsonl(paths.ENGLISH_CORRECTION_CHANGELOG, [{"logged_at": logged_at, **row} for row in rows])
    return paths.ENGLISH_CORRECTION_CHANGELOG


def _write_completion_marker(run_id: str, outcome: str, suggested: int, applied: int):
    payload = {
        "run_id": run_id,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "outcome": outcome,
        "suggested": int(suggested),
        "applied": int(applied),
    }
    write_json(paths.ENGLISH_CORRECTION_DONE, payload)
    return paths.ENGLISH_CORRECTION_DONE


def correct_english_asr_tokens():
    if not config.get_bool("english_correction.enabled", False):
        rprint("[dim]English correction disabled, skip.[/dim]")
        _write_completion_marker(datetime.now().strftime("%Y%m%d_%H%M%S"), "disabled", 0, 0)
        return

    if config.get_bool("english_correction.only_when_english", True):
        language = source_language()
        if str(language).lower() != "en":
            rprint(f"[dim]English correction skipped (source_language={language}).[/dim]")
            _write_completion_marker(datetime.now().strftime("%Y%m%d_%H%M%S"), "language_skipped", 0, 0)
            return

    if not paths.TRANSCRIPT.exists():
        raise FileNotFoundError(f"Missing input file: {paths.TRANSCRIPT}")

    transcript = read_json(paths.TRANSCRIPT)
    df = pd.DataFrame(transcript["words"], columns=["text", "start", "end"])
    if len(df) == 0:
        rprint("[dim]English correction skipped (no rows).[/dim]")
        _write_completion_marker(datetime.now().strftime("%Y%m%d_%H%M%S"), "empty_input", 0, 0)
        return

    tokens = _build_tokens(df)
    if not tokens:
        rprint("[dim]English correction skipped (no valid tokens).[/dim]")
        _write_completion_marker(datetime.now().strftime("%Y%m%d_%H%M%S"), "empty_tokens", 0, 0)
        return

    tokens_json = json.dumps(tokens, ensure_ascii=False, indent=2)
    prompt = get_english_correction_prompt(tokens_json)
    rprint(f"[cyan]Running English ASR correction on {len(tokens)} tokens...[/cyan]")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    response = ask_json(
        prompt,
        CORRECTION_RESPONSE_SCHEMA,
        valid_def=_valid_correction_response,
        log_title="english_correction",
        section="english_correction",
    )
    corrections = response.get("corrections", [])
    if not corrections:
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
        _write_completion_marker(run_id, outcome="no_corrections", suggested=0, applied=0)
        if log_path:
            rprint(f"[dim]English correction changelog: {log_path} (run_id={run_id})[/dim]")
        rprint("[green]No English ASR corrections suggested.[/green]")
        return

    if not paths.TRANSCRIPT_BEFORE_CORRECTION.exists():
        write_json(paths.TRANSCRIPT_BEFORE_CORRECTION, transcript)
        rprint(f"[dim]Backup created: {paths.TRANSCRIPT_BEFORE_CORRECTION}[/dim]")

    applied, audit_rows = _apply_corrections(df, corrections, run_id=run_id)
    if applied > 0:
        transcript["words"] = [
            {"text": row.text, "start": float(row.start), "end": float(row.end)} for row in df.itertuples()
        ]
        write_json(paths.TRANSCRIPT, transcript)

    log_path = _write_changelog(audit_rows)
    outcome = "completed" if applied > 0 else "no_safe_replacements"
    _write_completion_marker(run_id, outcome=outcome, suggested=len(corrections), applied=applied)
    if log_path:
        rprint(f"[dim]English correction changelog: {log_path} (run_id={run_id})[/dim]")

    if applied == 0:
        rprint("[yellow]English corrections returned, but no safe replacements were applied.[/yellow]")
        return

    rprint(f"[green]Applied {applied} English token corrections to {paths.TRANSCRIPT}[/green]")
