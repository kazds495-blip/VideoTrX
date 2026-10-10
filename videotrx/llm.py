import os
import json
import time
from threading import Lock

import json_repair
import requests
from openai import OpenAI
from rich import print as rprint

from videotrx import config, paths

# ------------
# cache gpt response
# ------------

LOCK = Lock()


def _pick_setting(override: dict, key: str, default=None):
    if isinstance(override, dict):
        value = override.get(key, None)
        if value not in (None, ""):
            return value
    return default


def save_log(model, prompt, resp_content, resp, message=None, log_title="default", attempt=0):
    with LOCK:
        logs = []
        file = os.path.join(paths.GPT_LOG_DIR, f"{log_title}.json")
        os.makedirs(os.path.dirname(file), exist_ok=True)
        if os.path.exists(file):
            with open(file, "r", encoding="utf-8") as f:
                logs = json.load(f)
        logs.append(
            {
                "model": model,
                "prompt": prompt,
                "attempt": attempt,
                "resp_content": resp_content,
                "resp": resp,
                "message": message,
            }
        )
        with open(file, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=4)


def _load_cache(prompt, log_title, model, attempt):
    with LOCK:
        file = os.path.join(paths.GPT_LOG_DIR, f"{log_title}.json")
        if os.path.exists(file):
            with open(file, "r", encoding="utf-8") as f:
                for item in json.load(f):
                    # 没有 attempt 字段的旧记录视为第 0 次
                    if (
                        item["prompt"] == prompt
                        and item.get("model") == model
                        and item.get("attempt", 0) == attempt
                    ):
                        return item["resp"]
        return False


PROTOCOL_CHOICES = ("chat_completions", "responses", "messages")


def _resolve_protocol(api_settings, global_protocol):
    if global_protocol not in PROTOCOL_CHOICES:
        raise ValueError(
            f"Invalid api.protocol: {global_protocol!r}. "
            f"Valid values: {', '.join(PROTOCOL_CHOICES)}"
        )
    protocol = _pick_setting(api_settings, "protocol", global_protocol)
    if protocol not in PROTOCOL_CHOICES:
        raise ValueError(
            f"Invalid api.protocol override: {protocol!r}. "
            f"Valid values: {', '.join(PROTOCOL_CHOICES)}"
        )
    return protocol


def resolve_settings(section=None) -> dict:
    """api.* 为全局值；section 非空时读 <section>.api.*，非空的项覆盖全局，留空或缺省的继承全局。

    reasoning_effort 与模型绑定，不继承：section 为空时读 api.reasoning_effort，
    否则只读 <section>.api.reasoning_effort。
    """
    overrides = config.get(f"{section}.api", {}) if section else {}
    if not isinstance(overrides, dict):
        overrides = {}

    def pick(key, default):
        return _pick_setting(overrides, key, config.get(f"api.{key}", default))

    llm_support_json = config.get_bool("api.llm_support_json", True)
    if section:
        llm_support_json = config.get_bool(f"{section}.api.llm_support_json", llm_support_json)
        reasoning_effort = overrides.get("reasoning_effort", "")
    else:
        reasoning_effort = config.get("api.reasoning_effort", "")

    return {
        "api_key": pick("key", ""),
        "model": pick("model", ""),
        "base_url": pick("base_url", ""),
        "protocol": _resolve_protocol(overrides, config.get("api.protocol", "")),
        "llm_support_json": llm_support_json,
        "timeout": config.to_int(pick("request_timeout_sec", 300), 300, min_value=1),
        "retries": config.to_int(pick("request_retries", 5), 5, min_value=0),
        "retry_delay": config.to_float(pick("request_retry_delay_sec", 1), 1, min_value=0),
        "reasoning_effort": str(reasoning_effort or "").strip(),
    }


def _normalize_openai_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def _normalize_claude_messages_url(base_url: str) -> str:
    base_url = base_url.strip().rstrip("/")
    if base_url.endswith("/v1/messages"):
        return base_url
    if base_url.endswith("/v1"):
        return base_url + "/messages"
    return base_url + "/v1/messages"


def _safe_json(obj):
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)


def _ask_gpt_chat(prompt, schema, settings):
    base_url = _normalize_openai_base_url(settings["base_url"])
    client = OpenAI(api_key=settings["api_key"], base_url=base_url)
    params = dict(
        model=settings["model"],
        messages=[{"role": "user", "content": prompt}],
        timeout=settings["timeout"],
    )
    if settings["llm_support_json"]:
        # 实测（2026-10-09）：中转站对 gemini-3.1-pro-preview 接受该字段但不强制 schema，
        # 回复仍可能带代码块或不符合结构，因此保留 json_repair 解析，并依赖 valid_def 校验
        params["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "output", "schema": schema, "strict": True},
        }
    # 取值原样透传，SDK 不在运行时校验
    if settings["reasoning_effort"]:
        params["reasoning_effort"] = settings["reasoning_effort"]
    resp_raw = client.chat.completions.create(**params)
    resp_content = resp_raw.choices[0].message.content
    return resp_content, json_repair.loads(resp_content)


def _ask_openai_responses(prompt, schema, settings):
    client = OpenAI(api_key=settings["api_key"], base_url=settings["base_url"])

    params = dict(model=settings["model"], input=prompt, timeout=settings["timeout"])
    if settings["llm_support_json"]:
        params["text"] = {
            "format": {"type": "json_schema", "name": "output", "schema": schema, "strict": True}
        }
    if settings["reasoning_effort"]:
        params["reasoning"] = {"effort": settings["reasoning_effort"]}

    resp_raw = client.responses.create(**params)
    resp_content = resp_raw.output_text
    if not resp_content:
        raise ValueError("OpenAI Responses returned empty output_text")
    return resp_content, json_repair.loads(resp_content)


def _ask_claude_messages(prompt, schema, settings):
    url = _normalize_claude_messages_url(settings["base_url"])
    headers = {
        "x-api-key": settings["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": settings["model"],
        # 开启思考后，思考消耗的 token 也计入 max_tokens
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": prompt}],
    }
    output_config = {}
    structured_output = settings["llm_support_json"]
    if structured_output:
        # 原生结构化输出：回复为按 schema 约束的 text 块，且不影响模型思考
        output_config["format"] = {"type": "json_schema", "schema": schema}
    if settings["reasoning_effort"]:
        output_config["effort"] = settings["reasoning_effort"]
    if output_config:
        payload["output_config"] = output_config

    resp_raw = requests.post(url, headers=headers, json=payload, timeout=settings["timeout"])
    resp_raw.raise_for_status()
    data = resp_raw.json()
    blocks = data.get("content", []) if isinstance(data, dict) else []
    text_content = "\n".join(
        block.get("text", "") for block in blocks if block.get("type") == "text"
    ).strip()

    if structured_output:
        # refusal / max_tokens 时输出可能不完整、不符合 schema，抛异常交给重试
        stop_reason = data.get("stop_reason") if isinstance(data, dict) else None
        if stop_reason in ("refusal", "max_tokens"):
            raise ValueError(f"Claude structured output stopped early: stop_reason={stop_reason}")
        return text_content, json.loads(text_content)

    if not text_content:
        text_content = _safe_json(data)
    return text_content, json_repair.loads(text_content)


# ------------
# ask json with retry
# ------------

def ask_json(prompt, schema, *, valid_def=None, log_title="default", section=None, attempt=0) -> dict:
    """按 schema 请求 JSON 回复；attempt 区分同一 prompt 的第几次重试，只参与缓存匹配，不改动 prompt。"""
    # 协议在缓存查找之前解析：配置非法时直接报错，不静默回放缓存
    settings = resolve_settings(section)
    if not settings["api_key"]:
        raise ValueError("API key is not set")
    model = settings["model"]
    route = settings["protocol"]

    # check cache
    cached = _load_cache(prompt, log_title, model, attempt)
    if cached:
        rprint("use cache response")
        return cached

    timeout = settings["timeout"]
    retry_delay = settings["retry_delay"]
    reasoning_effort = settings["reasoning_effort"]
    total_attempts = settings["retries"] + 1
    last_exception = None

    for request_attempt in range(1, total_attempts + 1):
        req_start = time.time()
        rprint(
            f"[cyan]LLM request start[/cyan] model={model} route={route} timeout={timeout}s "
            f"effort={reasoning_effort or '-'} "
            f"log={log_title} attempt={request_attempt}/{total_attempts}"
        )
        try:
            if route == "messages":
                resp_content, resp = _ask_claude_messages(prompt, schema, settings)
            elif route == "responses":
                resp_content, resp = _ask_openai_responses(prompt, schema, settings)
            else:
                resp_content, resp = _ask_gpt_chat(prompt, schema, settings)

            req_cost = round(time.time() - req_start, 2)
            rprint(
                f"[green]LLM request done[/green] model={model} route={route} cost={req_cost}s "
                f"log={log_title} attempt={request_attempt}/{total_attempts}"
            )

            # check if the response format is valid
            if valid_def:
                valid_resp = valid_def(resp)
                if valid_resp["status"] != "success":
                    save_log(
                        model,
                        prompt,
                        resp_content,
                        resp,
                        log_title="error",
                        message=valid_resp["message"],
                        attempt=attempt,
                    )
                    raise ValueError(f"API response error: {valid_resp['message']}")

            save_log(model, prompt, resp_content, resp, log_title=log_title, attempt=attempt)
            return resp
        except Exception as e:
            req_cost = round(time.time() - req_start, 2)
            last_exception = e
            rprint(
                f"[red]LLM request failed[/red] model={model} route={route} cost={req_cost}s "
                f"log={log_title} attempt={request_attempt}/{total_attempts} error={e}"
            )
            if request_attempt < total_attempts and retry_delay > 0:
                rprint(f"[yellow]Retrying in {retry_delay}s...[/yellow]")
                time.sleep(retry_delay)

    if last_exception:
        raise last_exception
    raise RuntimeError("LLM request failed with unknown error")


# ------------
# stream text
# ------------

def stream_text(prompt, settings, on_delta):
    """流式请求纯文本，每收到一段文本调用一次 on_delta；返回 finish_reason。按 settings["protocol"] 分派。"""
    if settings["protocol"] == "messages":
        return _stream_claude_text(prompt, settings, on_delta)
    if settings["protocol"] == "responses":
        return _stream_openai_responses_text(prompt, settings, on_delta)
    return _stream_openai_text(prompt, settings, on_delta)


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
