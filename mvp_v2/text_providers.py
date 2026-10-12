from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TextProvider = Callable[[str, str, int], str]
StreamingTextProvider = Callable[[str, str, int, Callable[[str], None]], str]
TEXT_TIMEOUT_SECONDS = 600
PROVIDER_RETRY_DELAYS_SECONDS = (5, 15, 30)


class TextProviderError(RuntimeError):
    def __init__(self, message: str, *, diagnostics: dict | None = None) -> None:
        super().__init__(message)
        self.diagnostics = deepcopy(diagnostics or {})


class TextProviderResponse(str):
    """A normal text result with optional private transport evidence."""

    def __new__(cls, value: str, *, diagnostics: dict | None = None):
        result = super().__new__(cls, value)
        result._diagnostics = deepcopy(diagnostics or {})
        return result

    @property
    def diagnostics(self) -> dict:
        return deepcopy(self._diagnostics)


@dataclass(frozen=True)
class TextRoute:
    provider: str
    model: str


def build_text_provider(provider: str, *, command: str = "mmx") -> TextProvider:
    normalized = str(provider or "").strip().casefold()
    if normalized in {"minimax", "minimax-cli", "mmx"}:
        return minimax_cli_provider(command)
    if normalized == "minimax-http":
        return minimax_http_provider()
    if normalized in {"deepseek", "deepseek-http"}:
        return deepseek_http_provider()
    raise TextProviderError(f"不支持的文本 Provider: {provider}")


def provider_credentials_available(provider: str, *, command: str = "mmx") -> bool:
    normalized = str(provider or "").strip().casefold()
    if normalized in {"minimax", "minimax-cli", "mmx"}:
        executable = shlex.split(command)[0] if command else ""
        command_available = bool(executable and _command_available(executable))
        if os.environ.get("MPLUS_EXECUTION_BACKEND", "").strip().casefold() == "cloud":
            return command_available and bool(os.environ.get("MINIMAX_API_KEY"))
        return command_available
    if normalized == "minimax-http":
        return bool(os.environ.get("MINIMAX_API_KEY"))
    if normalized in {"deepseek", "deepseek-http"}:
        return bool(os.environ.get("DEEPSEEK_API_KEY"))
    return False


def minimax_cli_provider(command: str) -> TextProvider:
    command_parts = shlex.split(command)
    if not command_parts:
        raise TextProviderError("MiniMax 文本命令不得为空")

    def invoke(prompt: str, model: str, max_tokens: int) -> str:
        messages = json.dumps([{"role": "user", "content": prompt}], ensure_ascii=False)
        diagnostics = {
            "provider": "minimax-cli",
            "request": {"model": model, "max_tokens": max_tokens, "temperature": 0.2,
                "timeout_seconds": TEXT_TIMEOUT_SECONDS, "prompt_sha256": _text_sha256(prompt)},
            "attempts": [],
        }
        with tempfile.TemporaryDirectory(prefix="mplus-text-") as tmp:
            messages_path = os.path.join(tmp, "messages.json")
            with open(messages_path, "w", encoding="utf-8") as handle:
                handle.write(messages)
            for attempt in range(len(PROVIDER_RETRY_DELAYS_SECONDS) + 1):
                try:
                    process = subprocess.run(
                        [
                            *command_parts,
                            "--timeout", str(TEXT_TIMEOUT_SECONDS),
                            "text", "chat",
                            "--model", model,
                            "--messages-file", messages_path,
                            "--output", "json",
                            "--max-tokens", str(max_tokens),
                            "--temperature", "0.2",
                            "--non-interactive",
                            "--no-color",
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=TEXT_TIMEOUT_SECONDS + 30,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    message = _redact_diagnostics(str(exc))
                    response = _response_diagnostics(getattr(exc, "stdout", "") or "")
                    diagnostics["response"] = response
                    diagnostics["attempts"].append({"attempt": attempt + 1, "status": "failed",
                        "error_type": type(exc).__name__, "error": message, "response": response,
                        "stderr": _redact_diagnostics(_diagnostic_text(getattr(exc, "stderr", "") or ""))})
                    if not _is_transient(message) or attempt >= len(PROVIDER_RETRY_DELAYS_SECONDS):
                        raise TextProviderError(f"MiniMax 文本调用异常: {message}", diagnostics=diagnostics) from exc
                else:
                    response = _response_diagnostics(process.stdout)
                    diagnostics["response"] = response
                    attempt_record = {"attempt": attempt + 1, "return_code": process.returncode,
                        "response": response, "stderr": _redact_diagnostics(process.stderr or "")}
                    if process.returncode == 0:
                        try:
                            text = _extract_text(process.stdout)
                        except TextProviderError as exc:
                            message = _redact_diagnostics(str(exc))
                            attempt_record.update(status="failed", error_type=type(exc).__name__, error=message)
                            diagnostics["attempts"].append(attempt_record)
                            raise TextProviderError(message, diagnostics=diagnostics) from exc
                        attempt_record["status"] = "succeeded"
                        diagnostics["attempts"].append(attempt_record)
                        return TextProviderResponse(_redact_diagnostics(text), diagnostics=diagnostics)
                    message = _redact_diagnostics((process.stderr or process.stdout or "").strip())
                    attempt_record.update(status="failed", error=message)
                    diagnostics["attempts"].append(attempt_record)
                    if not _is_transient(message) or attempt >= len(PROVIDER_RETRY_DELAYS_SECONDS):
                        raise TextProviderError(f"MiniMax 文本调用失败: {message[:500]}", diagnostics=diagnostics)
                time.sleep(PROVIDER_RETRY_DELAYS_SECONDS[attempt])
        raise TextProviderError("MiniMax 文本调用失败: 已耗尽自动重试")

    return invoke


def deepseek_http_provider(*, retry_delays: tuple[float, ...] | None = None) -> TextProvider:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise TextProviderError("缺少 DEEPSEEK_API_KEY")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    endpoint = f"{base_url}/chat/completions"

    def invoke(prompt: str, model: str, max_tokens: int) -> str:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "stream": False,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        }
        return _invoke_http_text(endpoint, api_key, payload, provider="deepseek-http", label="DeepSeek", retry_delays=retry_delays)

    return invoke


def deepseek_http_stream_provider() -> StreamingTextProvider:
    """One SSE attempt; callers persist deltas and decide how to resume failures."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise TextProviderError("缺少 DEEPSEEK_API_KEY")
    endpoint = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/") + "/chat/completions"

    def invoke(prompt: str, model: str, max_tokens: int, on_delta: Callable[[str], None]) -> str:
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0.2, "stream": True,
            "stream_options": {"include_usage": True}, "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"}}
        request = Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        pieces, usage, finish, done = [], {}, None, False
        started = time.monotonic()
        first_delta_ms = None
        for event in _http_sse_events(request, api_key=api_key):
            if event == "[DONE]":
                done = True
                break
            try:
                item = json.loads(event)
                if not isinstance(item, dict) or item.get("error"):
                    raise ValueError("流式响应返回错误")
                choices = item.get("choices", [])
                if not isinstance(choices, list) or len(choices) > 1:
                    raise ValueError("流式响应候选结构无效")
                if isinstance(item.get("usage"), dict):
                    usage = {key: value for key, value in item["usage"].items()
                        if key in {"prompt_tokens", "completion_tokens", "total_tokens"} and isinstance(value, int)}
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta", {})
                content = delta.get("content") or ""
                if not isinstance(content, str):
                    raise ValueError("流式正文必须为文字")
                finish = choice.get("finish_reason") or finish
            except (ValueError, AttributeError, TypeError) as exc:
                raise TextProviderError("DeepSeek 流式响应格式无效") from exc
            if content:
                if first_delta_ms is None:
                    first_delta_ms = round((time.monotonic() - started) * 1000)
                pieces.append(content)
                # Consumer conflicts must propagate, never become transport retries.
                on_delta(content)
        text = "".join(pieces)
        diagnostics = {"provider": "deepseek-http", "request": {
            "model": model, "max_tokens": max_tokens, "stream": True,
            "prompt_sha256": _text_sha256(prompt)}, "response": {
            "finish_reason": finish, "usage": usage, "first_delta_ms": first_delta_ms,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "raw_response_sha256": _text_sha256(text)},
            "attempts": [{"attempt": 1, "status": "succeeded" if done and finish == "stop" else "failed"}]}
        if not done or finish != "stop" or not text.strip():
            raise TextProviderError("初稿输出未完整结束，已收到的内容保留为待恢复片段", diagnostics=diagnostics)
        return TextProviderResponse(_redact_diagnostics(text, secrets=(api_key,)), diagnostics=diagnostics)

    return invoke


def _http_sse_events(request: Request, *, api_key: str):
    """Read event lines immediately, including multi-line data and keepalives."""
    try:
        with urlopen(request, timeout=TEXT_TIMEOUT_SECONDS) as response:
            data, size = [], 0
            for raw in response:
                size += len(raw)
                if size > 8 * 1024 * 1024:
                    raise TextProviderError("流式输出超出接收范围")
                line = raw.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data:
                        yield "\n".join(data)
                        data = []
                elif line.startswith("data:"):
                    data.append(line[5:].removeprefix(" "))
            # An unfinished event is deliberately not treated as a completed response.
    except (OSError, UnicodeError) as exc:
        message = _redact_diagnostics(_http_error_message(exc), secrets=(api_key,))
        raise TextProviderError(f"DeepSeek 流式连接中断: {message[:500]}") from exc


def minimax_http_provider() -> TextProvider:
    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        raise TextProviderError("缺少 MINIMAX_API_KEY")
    base_url = os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1").rstrip("/")
    endpoint = f"{base_url}/chat/completions"

    def invoke(prompt: str, model: str, max_tokens: int) -> str:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "stream": False,
        }
        return _invoke_http_text(endpoint, api_key, payload, provider="minimax-http", label="MiniMax HTTP")

    return invoke


def _invoke_http_text(endpoint: str, api_key: str, payload: dict, *, provider: str, label: str, retry_delays: tuple[float, ...] | None = None) -> str:
    request = Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    # This allowlist describes the actual outgoing payload; headers and endpoint
    # credentials must never enter the evidence handed back to a caller.
    parameters = {key: payload[key] for key in (
        "model", "max_tokens", "temperature", "stream", "thinking", "response_format",
    ) if key in payload}
    parameters.update(timeout_seconds=TEXT_TIMEOUT_SECONDS,
        prompt_sha256=_text_sha256(payload["messages"][0]["content"]))
    diagnostics = {"provider": provider, "request": parameters, "attempts": []}
    delays = PROVIDER_RETRY_DELAYS_SECONDS if retry_delays is None else retry_delays
    for attempt in range(len(delays) + 1):
        raw = ""
        try:
            with urlopen(request, timeout=TEXT_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise TextProviderError("文本 Provider 返回 JSON 必须为对象")
            text = _deepseek_content(parsed)
        except (OSError, KeyError, IndexError, UnicodeError,
                json.JSONDecodeError, TextProviderError) as exc:
            if isinstance(exc, HTTPError):
                try:
                    raw = exc.read().decode("utf-8", errors="replace")
                except OSError:
                    raw = ""
                message = f"HTTP {exc.code}: {raw or exc.reason}"
            else:
                message = str(exc)
            response_evidence = _response_diagnostics(raw, secrets=(api_key,))
            diagnostics["response"] = response_evidence
            diagnostics["attempts"].append({"attempt": attempt + 1, "status": "failed",
                "error_type": type(exc).__name__, "error": _redact_diagnostics(message, secrets=(api_key,)),
                "response": response_evidence})
            if isinstance(exc, TextProviderError) or not _is_transient(message) or attempt >= len(delays):
                raise TextProviderError(_redact_diagnostics(f"{label} 文本调用失败: {message[:500]}", secrets=(api_key,)),
                    diagnostics=_redact_diagnostics(diagnostics, secrets=(api_key,))) from exc
            time.sleep(delays[attempt])
            continue
        response_evidence = _response_diagnostics(raw, secrets=(api_key,))
        diagnostics["response"] = response_evidence
        diagnostics["attempts"].append({"attempt": attempt + 1, "status": "succeeded", "response": response_evidence})
        return TextProviderResponse(_redact_diagnostics(text, secrets=(api_key,)),
            diagnostics=_redact_diagnostics(diagnostics, secrets=(api_key,)))
    raise TextProviderError(f"{label} 文本调用失败: 已耗尽自动重试", diagnostics=diagnostics)


def _text_sha256(value: str) -> str:
    return sha256(str(value).encode("utf-8")).hexdigest()


def _diagnostic_text(value) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


def _response_diagnostics(raw: str, *, secrets: tuple[str, ...] = ()) -> dict:
    raw = _diagnostic_text(raw)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    choices = payload.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    result = {"raw_response": str(raw), "raw_response_sha256": _text_sha256(raw),
        "finish_reason": choice.get("finish_reason") or payload.get("stop_reason") or payload.get("finish_reason"),
        "usage": {key: usage[key] for key in (
            "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
            "prompt_tokens_details", "completion_tokens_details",
        ) if key in usage}}
    if isinstance(payload.get("model"), str):
        result["model"] = payload["model"]
    content = payload.get("content")
    if isinstance(content, list):
        result["content_types"] = [item.get("type") for item in content if isinstance(item, dict)]
    safe = _redact_diagnostics(result, secrets=secrets)
    safe["response_redacted"] = safe["raw_response"] != str(raw)
    safe["stored_response_sha256"] = _text_sha256(safe["raw_response"])
    return safe


def _transport_evidence(value) -> dict:
    diagnostics = getattr(value, "diagnostics", {})
    if not isinstance(diagnostics, dict) or not diagnostics:
        return {"availability": "unavailable"}
    def select(mapping, keys):
        return {key: mapping[key] for key in keys if key in mapping} if isinstance(mapping, dict) else {}
    response_keys = ("raw_response", "raw_response_sha256", "stored_response_sha256", "response_redacted",
        "finish_reason", "usage", "model", "content_types")
    result = select(diagnostics, ("provider",))
    result["request"] = select(diagnostics.get("request"), (
        "provider", "operation", "model", "max_tokens", "temperature", "stream", "thinking",
        "response_format", "timeout_seconds", "prompt_sha256", "model_selection", "parameters_available",
        "process_timeout_seconds",
    ))
    result["response"] = select(diagnostics.get("response"), response_keys)
    result["attempts"] = []
    for attempt in diagnostics.get("attempts", []):
        record = select(attempt, ("attempt", "status", "return_code", "stderr", "error_type", "error"))
        if isinstance(attempt, dict) and "response" in attempt:
            record["response"] = select(attempt["response"], response_keys)
        result["attempts"].append(record)
    return _redact_diagnostics(result)


def _redact_diagnostics(value, *, secrets: tuple[str, ...] = ()):
    known = tuple(dict.fromkeys(item for item in (*secrets, *(os.environ.get(key, "") for key in (
        "DEEPSEEK_API_KEY", "MINIMAX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    ))) if item))
    if isinstance(value, dict):
        return {key: _redact_diagnostics(item, secrets=known) for key, item in value.items()
            if not re.search(r"(?i)^(?:headers?|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)$", str(key))}
    if isinstance(value, (list, tuple)):
        return [_redact_diagnostics(item, secrets=known) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    for secret in sorted(set(known), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)\bBearer\s+[^\s\"',;}]+", "Bearer [REDACTED]", text)
    text = re.sub(r'''(?ix)(["']?(?:api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|password|secret)["']?\s*[:=]\s*)(["'])(.*?)\2''',
        lambda match: match.group(1) + match.group(2) + "[REDACTED]" + match.group(2), text)
    return re.sub(r'''(?ix)((?:api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*)(?!["']|\[REDACTED\])([^\s,;\]}]+)''',
        lambda match: match.group(1) + "[REDACTED]", text)


def _deepseek_content(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not isinstance(choices, list) or (choices and not isinstance(choices[0], dict)):
        raise TextProviderError("文本 Provider choices 结构无效")
    message = choices[0].get("message") if choices else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content
    finish_reason = choices[0].get("finish_reason") if choices else "unknown"
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    completion_tokens = usage.get("completion_tokens", "unknown")
    raise TextProviderError(
        "DeepSeek 返回中没有可读取正文: "
        f"finish_reason={finish_reason or 'unknown'}, "
        f"completion_tokens={completion_tokens}"
    )


def _extract_text(stdout: str) -> str:
    text = str(stdout or "").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        raise TextProviderError("文本 Provider 返回 JSON 必须为对象")
    content = payload.get("content")
    if isinstance(content, list):
        joined = "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if joined:
            return joined
    for key in ("text", "content", "response", "output"):
        if isinstance(payload.get(key), str):
            return payload[key]
    if "choices" in payload:
        return _deepseek_content(payload)
    if isinstance(content, list):
        block_types = [
            str(block.get("type") or "unknown")
            for block in content
            if isinstance(block, dict)
        ]
        stop_reason = payload.get("stop_reason") or payload.get("finish_reason") or "unknown"
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        output_tokens = usage.get("output_tokens") or usage.get("completion_tokens") or "unknown"
        raise TextProviderError(
            "文本 Provider 返回中没有可读取正文: "
            f"stop_reason={stop_reason}, content_types={block_types}, "
            f"output_tokens={output_tokens}"
        )
    raise TextProviderError("文本 Provider 返回中没有可读取正文")


def _http_error_message(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except OSError:
            body = ""
        return f"HTTP {exc.code}: {body or exc.reason}"
    return str(exc)


def _is_transient(message: str) -> bool:
    normalized = str(message).casefold()
    return any(marker in normalized for marker in (
        "429", "500", "502", "503", "504", "529", "overloaded",
        "rate limit", "timeout", "timed out", "temporarily unavailable",
    ))


def _command_available(executable: str) -> bool:
    from shutil import which

    return bool(which(executable))
