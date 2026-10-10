import hashlib
import json
import os
import re
import time

import requests
from openai import OpenAI
from rich.console import Console

from videotrx import config, paths
from videotrx.llm import (
    _normalize_claude_messages_url,
    _normalize_openai_base_url,
    _pick_setting,
    _resolve_protocol,
    _save_cache,
)
from videotrx.prompts import get_prompt_single_pass_full_polish
from videotrx.utils import append_jsonl

console = Console()

POLISH_LINE_PATTERN = re.compile(r"^\s*\[(\d+)\]\s*:?[ \t]*(.*?)\s*$")
POLISH_PROGRESS_REPORT_STEP = 20
POLISH_PROGRESS_STATE_VERSION = 2
POLISH_TAIL_PLACEHOLDER = "拜拜"
POLISH_STREAM_SNAPSHOT_INTERVAL_SEC = 0.5
POLISH_SAFE_RESERVE_LINES = 10
POLISH_TAIL_RESERVE_LINES = 1

def _load_sub_module_api_settings(prefix: str):
    """读取子模块 api.* 覆盖项；空值由上游继承全局配置。"""
    defaults = {
        "key": "",
        "base_url": "",
        "model": "",
        "protocol": "",
        "llm_support_json": True,
        "request_timeout_sec": 120,
        "request_retries": 2,
        "request_retry_delay_sec": 1,
        "reasoning_effort": "",
    }
    resolved = {}
    for k, v in defaults.items():
        key = f"{prefix}.api.{k}"
        resolved[k] = config.get_bool(key, v) if k == "llm_support_json" else config.get(key, v)
    return resolved


def _load_single_pass_full_polish_api_settings():
    return _load_sub_module_api_settings("polish")


def _sanitize_single_line_text(value):
    return str(value).replace("\n", " ").replace("\r", " ").strip()

def _hash_lines(lines):
    payload = "\n".join(_sanitize_single_line_text(line) for line in lines)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def _hash_text(text):
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()

def _atomic_write_json(file_path, payload):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    tmp_path = f"{file_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, file_path)

def _atomic_write_text(file_path, content):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    tmp_path = f"{file_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp_path, file_path)

def _load_single_pass_full_polish_progress_state():
    if not os.path.exists(paths.POLISH_PROGRESS):
        return None

    with open(paths.POLISH_PROGRESS, "r", encoding="utf-8") as f:
        return json.load(f)

def _render_single_pass_full_polish_preview(
    *,
    confirmed_lines,
    line_count,
    status,
    parsed_max_line,
    confirmed_max_line,
    confirm_cap_line,
    final_line_placeholder_active,
    final_line_placeholder_text,
    completion_reason,
):
    next_line = (
        line_count + 1
        if status in {"completed", "completed_with_placeholder"}
        else min(confirmed_max_line + 1, line_count + 1)
    )
    header_lines = [
        f"# status: {status}",
        f"# parsed_max_line: {parsed_max_line}",
        f"# confirmed_max_line: {confirmed_max_line}",
        f"# confirm_cap_line: {confirm_cap_line}",
        f"# next_line: {next_line}",
        f"# final_line_placeholder_active: {str(bool(final_line_placeholder_active)).lower()}",
        f"# completion_reason: {completion_reason or 'none'}",
        "# confirmed_lines:",
    ]
    body_lines = [
        f"[{i}] {_sanitize_single_line_text(line)}"
        for i, line in enumerate(confirmed_lines, 1)
    ]
    if final_line_placeholder_active and line_count > 0:
        body_lines.append(
            f"[{line_count}] {_sanitize_single_line_text(final_line_placeholder_text or POLISH_TAIL_PLACEHOLDER)}"
        )
    return "\n".join(header_lines + body_lines)

def _persist_single_pass_full_polish_stream_snapshot(
    *,
    start_line,
    line_count,
    parsed_max_line,
    confirmed_max_line,
    confirm_cap_line,
    final_line_placeholder_active,
    raw_content,
):
    header_lines = [
        f"# start_line: {start_line}",
        f"# parsed_max_line: {parsed_max_line}",
        f"# confirmed_max_line: {confirmed_max_line}",
        f"# confirm_cap_line: {confirm_cap_line}",
        f"# target_end_line: {line_count}",
        f"# final_line_placeholder_active: {str(bool(final_line_placeholder_active)).lower()}",
        "# raw_stream:",
    ]
    content = "\n".join(header_lines)
    if raw_content:
        content += "\n" + raw_content
    _atomic_write_text(paths.POLISH_STREAM, content)

def _persist_single_pass_full_polish_progress_state(
    *,
    src_lines,
    draft_lines,
    polished_lines,
    summary_prompt,
    parsed_max_line,
    confirmed_max_line,
    confirm_cap_line,
    status="in_progress",
    final_line_placeholder_active=False,
    final_line_placeholder_text=None,
    completion_reason=None,
):
    normalized_polished_lines = [
        _sanitize_single_line_text(line) for line in polished_lines
    ]
    draft_lines = [_sanitize_single_line_text(line) for line in draft_lines]
    confirmed_max_line = max(0, min(int(confirmed_max_line), len(src_lines)))
    parsed_max_line = max(0, min(int(parsed_max_line), len(src_lines)))
    confirm_cap_line = max(0, min(int(confirm_cap_line), len(src_lines)))
    confirmed_lines = normalized_polished_lines[:confirmed_max_line]
    state = {
        "version": POLISH_PROGRESS_STATE_VERSION,
        "status": status,
        "line_count": len(src_lines),
        "source_hash": _hash_lines(src_lines),
        "draft_hash": _hash_lines(draft_lines),
        "summary_hash": _hash_text(summary_prompt),
        "updated_at": int(time.time()),
        "parsed_max_line": parsed_max_line,
        "confirmed_max_line": confirmed_max_line,
        "confirm_cap_line": confirm_cap_line,
        "base_draft_lines": draft_lines,
        "confirmed_lines": confirmed_lines,
        "final_line_placeholder_active": bool(final_line_placeholder_active),
        "final_line_placeholder_text": _sanitize_single_line_text(
            final_line_placeholder_text or POLISH_TAIL_PLACEHOLDER
        ),
        "completion_reason": completion_reason,
    }
    _atomic_write_json(paths.POLISH_PROGRESS, state)
    _atomic_write_text(
        paths.POLISH_PREVIEW,
        _render_single_pass_full_polish_preview(
            confirmed_lines=confirmed_lines,
            line_count=len(src_lines),
            status=status,
            parsed_max_line=parsed_max_line,
            confirmed_max_line=confirmed_max_line,
            confirm_cap_line=confirm_cap_line,
            final_line_placeholder_active=final_line_placeholder_active,
            final_line_placeholder_text=final_line_placeholder_text,
            completion_reason=completion_reason,
        ),
    )

def _cleanup_single_pass_full_polish_progress():
    for file_path in (
        paths.POLISH_PROGRESS,
        paths.POLISH_PREVIEW,
        paths.POLISH_STREAM,
    ):
        if os.path.exists(file_path):
            os.remove(file_path)

def _restore_single_pass_full_polish_progress(src_lines, draft_lines, summary_prompt):
    state = _load_single_pass_full_polish_progress_state()
    line_count = len(src_lines)
    clean_draft_lines = [_sanitize_single_line_text(line) for line in draft_lines]
    source_hash = _hash_lines(src_lines)

    if not state:
        _persist_single_pass_full_polish_progress_state(
            src_lines=src_lines,
            draft_lines=clean_draft_lines,
            polished_lines=clean_draft_lines,
            summary_prompt=summary_prompt,
            parsed_max_line=0,
            confirmed_max_line=0,
            confirm_cap_line=0,
            status="in_progress",
            final_line_placeholder_active=False,
        )
        return {
            "draft_lines": clean_draft_lines,
            "polished_lines": clean_draft_lines.copy(),
            "next_line": 1,
            "resumed": False,
            "completed": False,
            "parsed_max_line": 0,
            "confirmed_max_line": 0,
            "confirm_cap_line": 0,
            "final_line_placeholder_active": False,
            "completion_reason": None,
        }

    if state.get("version") != POLISH_PROGRESS_STATE_VERSION:
        console.print("[yellow]Single-pass polish progress version mismatch; starting a new session.[/yellow]")
    elif state.get("line_count") != line_count:
        console.print("[yellow]Single-pass polish progress line count mismatch; starting a new session.[/yellow]")
    elif state.get("source_hash") != source_hash:
        console.print("[yellow]Single-pass polish progress source mismatch; starting a new session.[/yellow]")
    else:
        saved_draft_lines = state.get("base_draft_lines") or []
        saved_confirmed_lines = state.get("confirmed_lines") or []
        saved_confirmed_lines = [
            _sanitize_single_line_text(line) for line in saved_confirmed_lines[:line_count]
        ]
        saved_draft_lines = [
            _sanitize_single_line_text(line) for line in saved_draft_lines[:line_count]
        ]
        if len(saved_draft_lines) == line_count:
            polished_lines = saved_draft_lines.copy()
            confirmed_max_line = max(
                0,
                min(
                    int(state.get("confirmed_max_line", len(saved_confirmed_lines))),
                    len(saved_confirmed_lines),
                    line_count,
                ),
            )
            polished_lines[:confirmed_max_line] = saved_confirmed_lines[:confirmed_max_line]
            parsed_max_line = max(
                confirmed_max_line,
                min(int(state.get("parsed_max_line", confirmed_max_line)), line_count),
            )
            confirm_cap_line = max(
                confirmed_max_line,
                min(int(state.get("confirm_cap_line", confirmed_max_line)), line_count),
            )
            final_line_placeholder_active = bool(state.get("final_line_placeholder_active"))
            final_line_placeholder_text = _sanitize_single_line_text(
                state.get("final_line_placeholder_text", POLISH_TAIL_PLACEHOLDER)
            )
            completion_reason = state.get("completion_reason")
            status = state.get("status", "in_progress")

            if final_line_placeholder_active and line_count > 0:
                polished_lines[-1] = final_line_placeholder_text

            if _hash_lines(clean_draft_lines) != state.get("draft_hash"):
                console.print(
                    "[yellow]Detected existing single-pass polish progress. "
                    "Resuming from the saved draft instead of the newly generated draft.[/yellow]"
                )

            completed = status in {"completed", "completed_with_placeholder"}
            if completed:
                if status == "completed_with_placeholder":
                    console.print(
                        "[yellow]Single-pass full polish progress already completed with placeholder final line. "
                        "Reusing saved result.[/yellow]"
                    )
                return {
                    "draft_lines": saved_draft_lines,
                    "polished_lines": polished_lines,
                    "next_line": line_count + 1,
                    "resumed": True,
                    "completed": True,
                    "parsed_max_line": parsed_max_line,
                    "confirmed_max_line": confirmed_max_line,
                    "confirm_cap_line": confirm_cap_line,
                    "final_line_placeholder_active": final_line_placeholder_active,
                    "completion_reason": completion_reason,
                }

            next_line = confirmed_max_line + 1
            console.print(
                f"[cyan]Resuming single-pass full polish with confirmed={confirmed_max_line}/{line_count}, "
                f"parsed={parsed_max_line}, cap={confirm_cap_line}. "
                f"Continuing from line {next_line} "
                f"using {paths.POLISH_PREVIEW}.[/cyan]"
            )
            return {
                "draft_lines": saved_draft_lines,
                "polished_lines": polished_lines,
                "next_line": next_line,
                "resumed": True,
                "completed": False,
                "parsed_max_line": parsed_max_line,
                "confirmed_max_line": confirmed_max_line,
                "confirm_cap_line": confirm_cap_line,
                "final_line_placeholder_active": final_line_placeholder_active,
                "completion_reason": completion_reason,
            }

        console.print("[yellow]Single-pass polish progress file is incomplete; starting a new session.[/yellow]")

    _persist_single_pass_full_polish_progress_state(
        src_lines=src_lines,
        draft_lines=clean_draft_lines,
        polished_lines=clean_draft_lines,
        summary_prompt=summary_prompt,
        parsed_max_line=0,
        confirmed_max_line=0,
        confirm_cap_line=0,
        status="in_progress",
        final_line_placeholder_active=False,
    )
    return {
        "draft_lines": clean_draft_lines,
        "polished_lines": clean_draft_lines.copy(),
        "next_line": 1,
        "resumed": False,
        "completed": False,
        "parsed_max_line": 0,
        "confirmed_max_line": 0,
        "confirm_cap_line": 0,
        "final_line_placeholder_active": False,
        "completion_reason": None,
    }

def _resolve_single_pass_full_polish_request_settings(api_settings):
    global_api_key = config.get("api.key", "")
    global_model = config.get("api.model", "")
    global_base_url = config.get("api.base_url", "")
    global_protocol = config.get("api.protocol", "")
    global_timeout = config.get("api.request_timeout_sec", 300)
    global_retries = config.get("api.request_retries", 5)
    global_retry_delay = config.get("api.request_retry_delay_sec", 1)

    api_key = _pick_setting(api_settings, "key", global_api_key)
    if not api_key:
        raise ValueError("API key is not set")

    model = _pick_setting(api_settings, "model", global_model)
    if not model:
        raise ValueError("Model is not set")

    base_url = _pick_setting(api_settings, "base_url", global_base_url)
    timeout = config.to_int(
        _pick_setting(api_settings, "request_timeout_sec", global_timeout),
        300,
        min_value=1,
    )
    retries = config.to_int(
        _pick_setting(api_settings, "request_retries", global_retries),
        5,
        min_value=0,
    )
    retry_delay = config.to_float(
        _pick_setting(api_settings, "request_retry_delay_sec", global_retry_delay),
        1,
        min_value=0,
    )
    route = _resolve_protocol(api_settings, global_protocol)
    # 思考等级与模型绑定，不继承全局 api.reasoning_effort
    reasoning_effort = str((api_settings or {}).get("reasoning_effort", "") or "").strip()

    return {
        "api_key": api_key,
        "model": model,
        "base_url": base_url,
        "timeout": timeout,
        "retries": retries,
        "retry_delay": retry_delay,
        "route": route,
        "reasoning_effort": reasoning_effort,
    }

def _stream_openai_text(prompt, request_settings, on_delta):
    client_kwargs = {"api_key": request_settings["api_key"]}
    if request_settings["base_url"]:
        client_kwargs["base_url"] = _normalize_openai_base_url(request_settings["base_url"])

    client = OpenAI(**client_kwargs)
    params = dict(
        model=request_settings["model"],
        messages=[{"role": "user", "content": prompt}],
        timeout=request_settings["timeout"],
        stream=True,
    )
    if request_settings.get("reasoning_effort"):
        params["reasoning_effort"] = request_settings["reasoning_effort"]
    stream = client.chat.completions.create(**params)

    finish_reason = None
    try:
        for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue

            choice = choices[0]
            delta = getattr(choice, "delta", None)
            content = getattr(delta, "content", None) if delta else None
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            if content:
                on_delta(content)

            chunk_finish_reason = getattr(choice, "finish_reason", None)
            if chunk_finish_reason:
                finish_reason = chunk_finish_reason
    finally:
        close_stream = getattr(stream, "close", None)
        if callable(close_stream):
            close_stream()

    return finish_reason or "stop"

def _describe_responses_error(error_info):
    if error_info is None:
        return "unknown error"
    code = getattr(error_info, "code", None)
    message = getattr(error_info, "message", None)
    if code and message:
        return f"{code}: {message}"
    return str(message or code or "unknown error")

def _stream_openai_responses_text(prompt, request_settings, on_delta):
    client_kwargs = {"api_key": request_settings["api_key"]}
    if request_settings["base_url"]:
        client_kwargs["base_url"] = request_settings["base_url"]

    client = OpenAI(**client_kwargs)
    params = dict(
        model=request_settings["model"],
        input=prompt,
        timeout=request_settings["timeout"],
        stream=True,
    )
    if request_settings.get("reasoning_effort"):
        params["reasoning"] = {"effort": request_settings["reasoning_effort"]}
    stream = client.responses.create(**params)

    completed = False
    try:
        for event in stream:
            event_type = getattr(event, "type", None)
            if event_type == "response.output_text.delta":
                delta_text = getattr(event, "delta", None)
                if delta_text:
                    on_delta(delta_text)
            elif event_type == "response.completed":
                completed = True
                break
            elif event_type == "response.failed":
                failed_response = getattr(event, "response", None)
                raise ValueError(
                    "OpenAI Responses stream failed: "
                    f"{_describe_responses_error(getattr(failed_response, 'error', None))}"
                )
            elif event_type == "response.incomplete":
                incomplete_response = getattr(event, "response", None)
                details = getattr(incomplete_response, "incomplete_details", None)
                reason = getattr(details, "reason", None)
                raise ValueError(
                    f"OpenAI Responses stream incomplete: {reason or 'unknown reason'}"
                )
            elif event_type == "error":
                raise ValueError(
                    f"OpenAI Responses stream error: {_describe_responses_error(event)}"
                )
    finally:
        close_stream = getattr(stream, "close", None)
        if callable(close_stream):
            close_stream()

    if not completed:
        raise ValueError("OpenAI Responses stream ended without response.completed")

    return "stop"

def _stream_claude_text(prompt, request_settings, on_delta):
    if not request_settings["base_url"]:
        raise ValueError("Claude base_url is not set")

    url = _normalize_claude_messages_url(request_settings["base_url"])
    headers = {
        "x-api-key": request_settings["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": request_settings["model"],
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }
    if request_settings.get("reasoning_effort"):
        payload["output_config"] = {"effort": request_settings["reasoning_effort"]}

    finish_reason = None
    with requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=request_settings["timeout"],
        stream=True,
    ) as response:
        response.raise_for_status()
        event_type = None
        data_lines = []

        for raw_line in response.iter_lines(decode_unicode=True):
            if raw_line is None:
                continue

            line = raw_line.strip()
            if not line:
                if data_lines:
                    payload_text = "\n".join(data_lines)
                    payload_data = json.loads(payload_text)

                    if event_type == "content_block_delta":
                        delta = payload_data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                on_delta(text)
                    elif event_type == "message_delta":
                        finish_reason = (
                            payload_data.get("delta", {}).get("stop_reason") or finish_reason
                        )
                    elif event_type == "error":
                        error_info = payload_data.get("error", payload_data)
                        raise ValueError(f"Claude streaming error: {error_info}")
                    elif event_type == "message_stop":
                        finish_reason = finish_reason or "stop"
                        break

                event_type = None
                data_lines = []
                continue

            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())

    return finish_reason or "stop"

def _parse_polish_stream_lines(raw_content, start_line, end_line, allow_last_without_newline=False):
    if not raw_content:
        return []

    normalized = raw_content.replace("\r\n", "\n").replace("\r", "\n")
    physical_lines = normalized.splitlines(keepends=True)
    parsed_lines = {}

    for i, line in enumerate(physical_lines):
        is_last_line = i == len(physical_lines) - 1
        if is_last_line and not line.endswith("\n") and not allow_last_without_newline:
            continue

        stripped = line.strip()
        if not stripped or stripped.startswith("```"):
            continue

        match = POLISH_LINE_PATTERN.match(stripped)
        if not match:
            continue

        line_no = int(match.group(1))
        polished_text = match.group(2).strip()
        # 只有行号没有内容的行视为润色后保留为空
        if line_no < start_line or line_no > end_line:
            continue
        if line_no not in parsed_lines:
            parsed_lines[line_no] = polished_text

    contiguous_lines = []
    current_line = start_line
    while current_line in parsed_lines:
        contiguous_lines.append((current_line, parsed_lines[current_line]))
        current_line += 1

    return contiguous_lines

def _compute_confirm_cap_line(parsed_max_line, confirmed_max_line, line_count):
    if line_count <= 0:
        return 0

    tail_trigger_line = max(0, line_count - POLISH_SAFE_RESERVE_LINES)
    reserve = (
        POLISH_TAIL_RESERVE_LINES
        if confirmed_max_line >= tail_trigger_line
        else POLISH_SAFE_RESERVE_LINES
    )
    return max(0, min(line_count - 1, parsed_max_line - reserve))

def _commit_polish_stream_progress(
    raw_content,
    polished_lines,
    draft_lines,
    summary_prompt,
    src_lines,
    start_line,
    end_line,
    committed_count,
    parsed_max_line_before,
    confirm_cap_line_before,
    final_line_placeholder_active,
    allow_last_without_newline=False,
):
    parsed_lines = _parse_polish_stream_lines(
        raw_content,
        start_line,
        end_line,
        allow_last_without_newline=allow_last_without_newline,
    )
    line_count = len(src_lines)
    parsed_count = len(parsed_lines)
    parsed_max_line = start_line + parsed_count - 1 if parsed_count else start_line - 1
    confirmed_max_line = start_line - 1 + committed_count
    confirm_cap_line = _compute_confirm_cap_line(
        parsed_max_line=parsed_max_line,
        confirmed_max_line=confirmed_max_line,
        line_count=line_count,
    )
    # 已连续解析到全文末行：保留区不应再挡住正文写回；末行仍由后面的分支处理
    if line_count > 0 and parsed_max_line >= line_count:
        confirm_cap_line = line_count - 1
    parsed_map = {line_no: polished_text for line_no, polished_text in parsed_lines}

    for line_no in range(confirmed_max_line + 1, confirm_cap_line + 1):
        polished_text = parsed_map.get(line_no)
        if polished_text is None:
            break
        polished_lines[line_no - 1] = polished_text
        console.print(f"[green][{line_no}][/green] {polished_text}")
        confirmed_max_line = line_no
        committed_count = max(0, confirmed_max_line - start_line + 1)
        _persist_single_pass_full_polish_progress_state(
            src_lines=src_lines,
            draft_lines=draft_lines,
            polished_lines=polished_lines,
            summary_prompt=summary_prompt,
            parsed_max_line=parsed_max_line,
            confirmed_max_line=confirmed_max_line,
            confirm_cap_line=confirm_cap_line,
            status="in_progress",
            final_line_placeholder_active=final_line_placeholder_active,
            completion_reason=None,
        )

    completion_reason = None
    status = "in_progress"

    if (
        line_count > 0
        and not final_line_placeholder_active
        and confirmed_max_line >= line_count - 1
    ):
        polished_lines[line_count - 1] = POLISH_TAIL_PLACEHOLDER
        final_line_placeholder_active = True
        status = "completed_with_placeholder"
        completion_reason = "final_line_placeholder"
        console.print(
            f"[yellow]Confirmed line {line_count - 1}. "
            f"Pre-wrote fallback for final line {line_count}: `{POLISH_TAIL_PLACEHOLDER}`.[/yellow]"
        )
        _persist_single_pass_full_polish_progress_state(
            src_lines=src_lines,
            draft_lines=draft_lines,
            polished_lines=polished_lines,
            summary_prompt=summary_prompt,
            parsed_max_line=parsed_max_line,
            confirmed_max_line=confirmed_max_line,
            confirm_cap_line=confirm_cap_line,
            status=status,
            final_line_placeholder_active=True,
            final_line_placeholder_text=POLISH_TAIL_PLACEHOLDER,
            completion_reason=completion_reason,
        )

    if line_count > 0 and parsed_max_line >= line_count:
        final_text = parsed_map.get(line_count)
        if final_text:
            polished_lines[line_count - 1] = final_text
            if final_line_placeholder_active:
                console.print(
                    f"[green][{line_count}][/green] {final_text} [dim](overrode placeholder)[/dim]"
                )
            else:
                console.print(f"[green][{line_count}][/green] {final_text}")
            final_line_placeholder_active = False
            confirmed_max_line = line_count
            committed_count = max(0, confirmed_max_line - start_line + 1)
            confirm_cap_line = max(confirm_cap_line, line_count)
            status = "completed"
            completion_reason = "completed"
            _persist_single_pass_full_polish_progress_state(
                src_lines=src_lines,
                draft_lines=draft_lines,
                polished_lines=polished_lines,
                summary_prompt=summary_prompt,
                parsed_max_line=parsed_max_line,
                confirmed_max_line=confirmed_max_line,
                confirm_cap_line=confirm_cap_line,
                status=status,
                final_line_placeholder_active=False,
                completion_reason=completion_reason,
            )

    if status == "in_progress":
        state_changed = (
            parsed_max_line != parsed_max_line_before
            or confirm_cap_line != confirm_cap_line_before
        )
        if state_changed:
            _persist_single_pass_full_polish_progress_state(
                src_lines=src_lines,
                draft_lines=draft_lines,
                polished_lines=polished_lines,
                summary_prompt=summary_prompt,
                parsed_max_line=parsed_max_line,
                confirmed_max_line=confirmed_max_line,
                confirm_cap_line=confirm_cap_line,
                status=status,
                final_line_placeholder_active=final_line_placeholder_active,
                completion_reason=None,
            )

    return {
        "committed": committed_count,
        "parsed_count": parsed_count,
        "parsed_max_line": parsed_max_line,
        "confirmed_max_line": confirmed_max_line,
        "confirm_cap_line": confirm_cap_line,
        "final_line_placeholder_active": final_line_placeholder_active,
        "status": status,
        "completion_reason": completion_reason,
    }

def _log_single_pass_full_polish_round(
    request_settings,
    prompt,
    raw_content,
    round_index,
    start_line,
    committed_count,
    finish_reason=None,
    error=None,
):
    message = (
        f"round={round_index} start_line={start_line} committed={committed_count} "
        f"finish_reason={finish_reason or 'unknown'}"
    )
    if error:
        message += f" error={error}"

    _save_cache(
        request_settings["model"],
        prompt,
        raw_content,
        "text",
        raw_content,
        message=message,
        log_title="single_pass_full_polish",
    )

def _run_single_pass_full_polish_round(
    prompt,
    src_lines,
    draft_lines,
    summary_prompt,
    polished_lines,
    start_line,
    line_count,
    request_settings,
    round_index,
):
    raw_content = ""
    committed_count = 0
    last_reported_count = 0
    last_stream_snapshot_at = 0.0
    finish_reason = None
    error = None
    parsed_max_line = start_line - 1
    confirm_cap_line = start_line - 1
    final_line_placeholder_active = False

    def persist_stream_snapshot(force=False):
        nonlocal last_stream_snapshot_at
        now = time.time()
        if not force and now - last_stream_snapshot_at < POLISH_STREAM_SNAPSHOT_INTERVAL_SEC:
            return
        _persist_single_pass_full_polish_stream_snapshot(
            start_line=start_line,
            line_count=line_count,
            parsed_max_line=parsed_max_line,
            confirmed_max_line=start_line - 1 + committed_count,
            confirm_cap_line=confirm_cap_line,
            final_line_placeholder_active=final_line_placeholder_active,
            raw_content=raw_content,
        )
        last_stream_snapshot_at = now

    def report_progress(force=False):
        nonlocal last_reported_count
        if committed_count <= last_reported_count:
            return
        if not force and committed_count - last_reported_count < POLISH_PROGRESS_REPORT_STEP:
            return

        absolute_done = start_line + committed_count - 1
        console.print(
            f"[cyan]Full-text polish progress: {absolute_done}/{line_count} lines committed.[/cyan]"
        )
        last_reported_count = committed_count

    def on_delta(text):
        nonlocal raw_content, committed_count, parsed_max_line, confirm_cap_line, final_line_placeholder_active
        raw_content += text
        if "\n" not in text:
            persist_stream_snapshot(force=False)
            return

        progress_state = _commit_polish_stream_progress(
            raw_content,
            polished_lines,
            draft_lines,
            summary_prompt,
            src_lines,
            start_line,
            line_count,
            committed_count,
            parsed_max_line,
            confirm_cap_line,
            final_line_placeholder_active,
            allow_last_without_newline=False,
        )
        committed_count = progress_state["committed"]
        parsed_max_line = progress_state["parsed_max_line"]
        confirm_cap_line = progress_state["confirm_cap_line"]
        final_line_placeholder_active = progress_state["final_line_placeholder_active"]
        report_progress(force=False)
        persist_stream_snapshot(force=True)

    try:
        if request_settings["route"] == "messages":
            finish_reason = _stream_claude_text(prompt, request_settings, on_delta)
        elif request_settings["route"] == "responses":
            finish_reason = _stream_openai_responses_text(prompt, request_settings, on_delta)
        else:
            finish_reason = _stream_openai_text(prompt, request_settings, on_delta)
    except Exception as exc:
        error = exc
    finally:
        progress_state = _commit_polish_stream_progress(
            raw_content,
            polished_lines,
            draft_lines,
            summary_prompt,
            src_lines,
            start_line,
            line_count,
            committed_count,
            parsed_max_line,
            confirm_cap_line,
            final_line_placeholder_active,
            allow_last_without_newline=error is None,
        )
        committed_count = progress_state["committed"]
        parsed_max_line = progress_state["parsed_max_line"]
        confirm_cap_line = progress_state["confirm_cap_line"]
        final_line_placeholder_active = progress_state["final_line_placeholder_active"]
        persist_stream_snapshot(force=True)
        report_progress(force=True)
        _log_single_pass_full_polish_round(
            request_settings=request_settings,
            prompt=prompt,
            raw_content=raw_content,
            round_index=round_index,
            start_line=start_line,
            committed_count=committed_count,
            finish_reason=finish_reason,
            error=error,
        )

    if final_line_placeholder_active:
        committed_count = line_count - start_line + 1

    return {
        "committed": committed_count,
        "parsed_max_line": parsed_max_line,
        "confirm_cap_line": confirm_cap_line,
        "final_line_placeholder_active": final_line_placeholder_active,
        "finish_reason": finish_reason,
        "error": error,
    }

def _write_polish_changelog(
    src_lines,
    draft_lines,
    final_lines,
    *,
    stage="polish",
    final_line_placeholder_active=False,
    note="",
):
    """只记录实际采用的译文，日志失败不影响字幕产出。"""
    try:
        records = []
        for row, (source, draft, final) in enumerate(zip(src_lines, draft_lines, final_lines), 1):
            is_placeholder = final_line_placeholder_active and row == len(final_lines)
            records.append({
                "row": row,
                "source": source,
                "draft": draft,
                "final": final,
                "stage": stage,
                "changed": final != draft,
                "status": (
                    "fallback" if stage == "polish_fallback"
                    else "completed_with_placeholder" if is_placeholder
                    else "polished"
                ),
                "note": note or ("末行使用既有占位文本" if is_placeholder else ""),
            })
        paths.POLISH_CHANGELOG.unlink(missing_ok=True)
        append_jsonl(paths.POLISH_CHANGELOG, records)
    except Exception as error:
        console.print(f"[yellow]Warning: Failed to write polish changelog: {error}[/yellow]")


def polish_single_pass_full_text(src_lines, draft_lines, summary_prompt=None):
    if len(src_lines) != len(draft_lines):
        raise ValueError("Full polish input mismatch: source and translation line counts are different.")

    line_count = len(draft_lines)
    api_settings = _load_single_pass_full_polish_api_settings()
    request_settings = _resolve_single_pass_full_polish_request_settings(api_settings)
    resume_state = _restore_single_pass_full_polish_progress(
        src_lines=src_lines,
        draft_lines=draft_lines,
        summary_prompt=summary_prompt,
    )
    draft_lines = resume_state["draft_lines"]
    polished_lines = resume_state["polished_lines"]
    next_line = resume_state["next_line"]
    no_progress_attempts = 0
    round_index = 0
    final_line_placeholder_active = resume_state["final_line_placeholder_active"]

    if resume_state["completed"]:
        console.print("[green]Single-pass full polish already completed in progress file. Reusing saved result.[/green]")
        _write_polish_changelog(
            src_lines, draft_lines, polished_lines,
            final_line_placeholder_active=final_line_placeholder_active,
        )
        return polished_lines

    while next_line <= line_count:
        round_index += 1
        prompt = get_prompt_single_pass_full_polish(
            source_lines=src_lines,
            draft_lines=draft_lines,
            summary_prompt=summary_prompt,
            start_line=next_line,
            polished_prefix_lines=polished_lines[: next_line - 1],
        )
        console.print(
            f"[cyan]Full-text polish round {round_index}: streaming lines {next_line}-{line_count}...[/cyan]"
        )
        round_result = _run_single_pass_full_polish_round(
            prompt=prompt,
            src_lines=src_lines,
            draft_lines=draft_lines,
            summary_prompt=summary_prompt,
            polished_lines=polished_lines,
            start_line=next_line,
            line_count=line_count,
            request_settings=request_settings,
            round_index=round_index,
        )

        final_line_placeholder_active = round_result["final_line_placeholder_active"]
        committed = round_result["committed"]
        if committed > 0:
            next_line += committed
            no_progress_attempts = 0
            if next_line <= line_count:
                console.print(
                    "[yellow]Full-text polish stream ended before completion; "
                    f"resuming from line {next_line}. "
                    f"finish_reason={round_result['finish_reason'] or 'unknown'}[/yellow]"
                )
            continue

        no_progress_attempts += 1
        max_attempts = request_settings["retries"] + 1
        if no_progress_attempts >= max_attempts:
            last_reason = round_result["error"] or (
                f"finish_reason={round_result['finish_reason'] or 'unknown'}"
            )
            raise ValueError(
                f"Full polish made no progress from line {next_line} after {max_attempts} attempts. "
                f"Last reason: {last_reason}"
            )

        console.print(
            "[yellow]Full-text polish made no progress; "
            f"retrying from line {next_line} "
            f"({no_progress_attempts + 1}/{max_attempts})...[/yellow]"
        )
        if request_settings["retry_delay"] > 0:
            time.sleep(request_settings["retry_delay"])

    empty_lines = [
        i for i, polished in enumerate(polished_lines, 1)
        if not polished and str(src_lines[i - 1]).strip()
    ]
    if empty_lines:
        console.print(f"[yellow]Full polish kept empty lines: {empty_lines}[/yellow]")

    _write_polish_changelog(
        src_lines, draft_lines, polished_lines,
        final_line_placeholder_active=final_line_placeholder_active,
    )
    return polished_lines
