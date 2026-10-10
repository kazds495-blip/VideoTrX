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


def _save_cache(model, prompt, resp_content, resp_type, resp, message=None, log_title="default"):
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
                "resp_content": resp_content,
                "resp_type": resp_type,
                "resp": resp,
                "message": message,
            }
        )
        with open(file, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=4)


def _load_cache(prompt, resp_type, log_title, model=None):
    with LOCK:
        file = os.path.join(paths.GPT_LOG_DIR, f"{log_title}.json")
        if os.path.exists(file):
            with open(file, "r", encoding="utf-8") as f:
                for item in json.load(f):
                    if item["prompt"] == prompt and item["resp_type"] == resp_type:
                        if model is not None and item.get("model") != model:
                            continue
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


def _ask_gpt_chat(
    prompt,
    resp_type,
    model,
    base_url,
    api_key,
    llm_support_json,
    timeout,
    json_schema=None,
    reasoning_effort="",
):
    base_url = _normalize_openai_base_url(base_url)
    client = OpenAI(api_key=api_key, base_url=base_url)
    response_format = None
    if resp_type == "json" and llm_support_json:
        if json_schema is not None:
            # 实测（2026-10-09）：中转站对 gemini-3.1-pro-preview 接受该字段但不强制 schema，
            # 回复仍可能带代码块或不符合结构，因此保留 json_repair 解析，并依赖 valid_def 校验
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": json_schema, "strict": True},
            }
        else:
            response_format = {"type": "json_object"}

    params = dict(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format=response_format,
        timeout=timeout,
    )
    # 取值原样透传，SDK 不在运行时校验
    if reasoning_effort:
        params["reasoning_effort"] = reasoning_effort
    resp_raw = client.chat.completions.create(**params)
    resp_content = resp_raw.choices[0].message.content
    if resp_type == "json":
        resp = json_repair.loads(resp_content)
    else:
        resp = resp_content
    return resp_content, resp


def _ask_openai_responses(
    prompt,
    resp_type,
    model,
    base_url,
    api_key,
    llm_support_json,
    timeout,
    json_schema=None,
    reasoning_effort="",
):
    client = OpenAI(api_key=api_key, base_url=base_url)

    params = dict(model=model, input=prompt, timeout=timeout)
    if resp_type == "json" and llm_support_json:
        if json_schema is not None:
            params["text"] = {
                "format": {"type": "json_schema", "name": "output", "schema": json_schema, "strict": True}
            }
        else:
            params["text"] = {"format": {"type": "json_object"}}
    if reasoning_effort:
        params["reasoning"] = {"effort": reasoning_effort}

    resp_raw = client.responses.create(**params)
    resp_content = resp_raw.output_text
    if not resp_content:
        raise ValueError("OpenAI Responses returned empty output_text")
    if resp_type == "json":
        resp = json_repair.loads(resp_content)
    else:
        resp = resp_content
    return resp_content, resp


def _ask_claude_messages(
    prompt,
    resp_type,
    model,
    base_url,
    api_key,
    llm_support_json,
    timeout,
    json_schema=None,
    reasoning_effort="",
):
    url = _normalize_claude_messages_url(base_url)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        # 开启思考后，思考消耗的 token 也计入 max_tokens
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": prompt}],
    }
    output_config = {}
    structured_output = resp_type == "json" and llm_support_json and json_schema is not None
    if structured_output:
        # 原生结构化输出：回复为按 schema 约束的 text 块，且不影响模型思考
        output_config["format"] = {"type": "json_schema", "schema": json_schema}
    elif resp_type == "json" and llm_support_json:
        # 兜底：未提供 schema 的调用仍用强制工具调用取 JSON。
        # 注意 Opus 5.5 / Sonnet 5.5 / Fable 5.1 对强制 tool_choice 直接返回 400，
        # Haiku 5.5 在强制工具调用时不思考；所有调用补齐 schema 后应删除此路径。
        payload["tools"] = [
            {
                "name": "output_json",
                "description": "Return final answer as a JSON object only.",
                "input_schema": {"type": "object", "additionalProperties": True},
            }
        ]
        payload["tool_choice"] = {"type": "tool", "name": "output_json"}
    if reasoning_effort:
        output_config["effort"] = reasoning_effort
    if output_config:
        payload["output_config"] = output_config

    resp_raw = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp_raw.raise_for_status()
    data = resp_raw.json()
    blocks = data.get("content", []) if isinstance(data, dict) else []

    if structured_output:
        # refusal / max_tokens 时输出可能不完整、不符合 schema，抛异常交给重试
        stop_reason = data.get("stop_reason") if isinstance(data, dict) else None
        if stop_reason in ("refusal", "max_tokens"):
            raise ValueError(f"Claude structured output stopped early: stop_reason={stop_reason}")
        text_content = "\n".join(
            block.get("text", "") for block in blocks if block.get("type") == "text"
        ).strip()
        return text_content, json.loads(text_content)

    if resp_type == "json":
        for block in blocks:
            if block.get("type") == "tool_use" and "input" in block:
                tool_input = block["input"]
                if isinstance(tool_input, dict):
                    return _safe_json(tool_input), tool_input
                return str(tool_input), json_repair.loads(str(tool_input))

        text_content = "\n".join(
            block.get("text", "") for block in blocks if block.get("type") == "text"
        ).strip()
        if not text_content:
            text_content = _safe_json(data)
        return text_content, json_repair.loads(text_content)

    text_content = "\n".join(
        block.get("text", "") for block in blocks if block.get("type") == "text"
    ).strip()
    if not text_content:
        for block in blocks:
            if block.get("type") == "tool_use" and "input" in block:
                tool_input = block["input"]
                text_content = (
                    _safe_json(tool_input) if isinstance(tool_input, (dict, list)) else str(tool_input)
                )
                break
    if not text_content:
        text_content = _safe_json(data)
    return text_content, text_content


# ------------
# ask gpt with retry
# ------------

def ask_gpt(prompt, resp_type=None, valid_def=None, log_title="default", api_settings=None, json_schema=None):
    global_api_key = config.get("api.key", "")
    global_model = config.get("api.model", "")
    global_base_url = config.get("api.base_url", "")
    global_protocol = config.get("api.protocol", "")
    global_llm_support_json = config.get_bool("api.llm_support_json", True)
    global_timeout = config.get("api.request_timeout_sec", 300)
    global_retries = config.get("api.request_retries", 5)
    global_retry_delay = config.get("api.request_retry_delay_sec", 1)

    api_key = _pick_setting(api_settings, "key", global_api_key)
    if not api_key:
        raise ValueError("API key is not set")
    model = _pick_setting(api_settings, "model", global_model)

    # 协议在缓存查找之前解析：配置非法时直接报错，不静默回放缓存
    protocol = _resolve_protocol(api_settings, global_protocol)

    # check cache
    cached = _load_cache(prompt, resp_type, log_title, model=model)
    if cached:
        rprint("use cache response")
        return cached

    base_url = _pick_setting(api_settings, "base_url", global_base_url)
    llm_support_json = _pick_setting(api_settings, "llm_support_json", global_llm_support_json)
    timeout = config.to_int(_pick_setting(api_settings, "request_timeout_sec", global_timeout), 300, min_value=1)
    retries = config.to_int(_pick_setting(api_settings, "request_retries", global_retries), 5, min_value=0)
    retry_delay = config.to_float(
        _pick_setting(api_settings, "request_retry_delay_sec", global_retry_delay),
        1,
        min_value=0,
    )
    # 思考等级与模型绑定，不从全局继承：子模块只读自己 api 块里的值
    if api_settings is None:
        reasoning_effort = config.get("api.reasoning_effort", "")
    else:
        reasoning_effort = api_settings.get("reasoning_effort", "")
    reasoning_effort = str(reasoning_effort or "").strip()
    route = protocol

    total_attempts = retries + 1
    last_exception = None

    for attempt in range(1, total_attempts + 1):
        req_start = time.time()
        rprint(
            f"[cyan]LLM request start[/cyan] model={model} route={route} timeout={timeout}s "
            f"effort={reasoning_effort or '-'} "
            f"log={log_title} attempt={attempt}/{total_attempts}"
        )
        try:
            if protocol == "messages":
                resp_content, resp = _ask_claude_messages(
                    prompt=prompt,
                    resp_type=resp_type,
                    model=model,
                    base_url=base_url,
                    api_key=api_key,
                    llm_support_json=llm_support_json,
                    timeout=timeout,
                    json_schema=json_schema,
                    reasoning_effort=reasoning_effort,
                )
            elif protocol == "responses":
                resp_content, resp = _ask_openai_responses(
                    prompt=prompt,
                    resp_type=resp_type,
                    model=model,
                    base_url=base_url,
                    api_key=api_key,
                    llm_support_json=llm_support_json,
                    timeout=timeout,
                    json_schema=json_schema,
                    reasoning_effort=reasoning_effort,
                )
            else:
                resp_content, resp = _ask_gpt_chat(
                    prompt=prompt,
                    resp_type=resp_type,
                    model=model,
                    base_url=base_url,
                    api_key=api_key,
                    llm_support_json=llm_support_json,
                    timeout=timeout,
                    json_schema=json_schema,
                    reasoning_effort=reasoning_effort,
                )

            req_cost = round(time.time() - req_start, 2)
            rprint(
                f"[green]LLM request done[/green] model={model} route={route} cost={req_cost}s "
                f"log={log_title} attempt={attempt}/{total_attempts}"
            )

            # check if the response format is valid
            if valid_def:
                valid_resp = valid_def(resp)
                if valid_resp["status"] != "success":
                    _save_cache(
                        model,
                        prompt,
                        resp_content,
                        resp_type,
                        resp,
                        log_title="error",
                        message=valid_resp["message"],
                    )
                    raise ValueError(f"API response error: {valid_resp['message']}")

            _save_cache(model, prompt, resp_content, resp_type, resp, log_title=log_title)
            return resp
        except Exception as e:
            req_cost = round(time.time() - req_start, 2)
            last_exception = e
            rprint(
                f"[red]LLM request failed[/red] model={model} route={route} cost={req_cost}s "
                f"log={log_title} attempt={attempt}/{total_attempts} error={e}"
            )
            if attempt < total_attempts and retry_delay > 0:
                rprint(f"[yellow]Retrying in {retry_delay}s...[/yellow]")
                time.sleep(retry_delay)

    if last_exception:
        raise last_exception
    raise RuntimeError("LLM request failed with unknown error")
