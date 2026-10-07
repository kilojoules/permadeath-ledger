"""Model backends. Every backend returns the same record:
{content, reasoning, prompt_tokens, completion_tokens, done_reason, latency_ms, raw}.

VLLMBackend: OpenAI-compatible /v1/chat/completions on a vLLM server (docs/MODEL_OPTIONS.md). Streams by default
(the RunPod proxy drops responses that do not start within 100 s) and reassembles the deltas. Sends the full
sampling dict explicitly on every call and a json_schema response_format when structured output is on.
MockBackend: a scripted responder for tests and dry runs (no network).
"""
from __future__ import annotations

import json
import time
from typing import Callable

import requests


class BackendError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 524}


class Backend:
    name = "base"

    def __init__(self, base_url: str = "", model: str = "", timeout: int = 900, retries: int = 4):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.calls = 0

    def info(self) -> dict:
        return {"engine": self.name}

    def chat(self, messages: list[dict], schema: dict | None, seed: int, sampling: dict, max_tokens: int,
             extra: dict | None = None, structured: bool = True) -> dict:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                self.calls += 1
                return self._chat(messages, schema, seed, sampling, max_tokens, extra or {}, structured)
            except BackendError as e:
                last = e
                if e.status is not None and e.status not in RETRYABLE_STATUS:
                    raise
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
                last = e
            time.sleep(min(60, 2 ** attempt * 3))
        raise BackendError(f"backend failed after {self.retries} attempts: {last!r}")

    def _chat(self, messages, schema, seed, sampling, max_tokens, extra, structured) -> dict:
        raise NotImplementedError


class VLLMBackend(Backend):
    name = "vllm"

    def __init__(self, base_url: str, model: str, timeout: int = 900, retries: int = 4, stream: bool = True,
                 api_key: str | None = None):
        super().__init__(base_url, model, timeout, retries)
        self.stream = stream
        self.headers = {"content-type": "application/json"}
        if api_key:
            self.headers["authorization"] = f"Bearer {api_key}"

    def info(self) -> dict:
        out = {"engine": "vllm", "base_url": self.base_url, "model": self.model}
        try:
            out["engine_version"] = requests.get(self.base_url + "/version", timeout=30, headers=self.headers).json().get("version")
        except Exception as e:  # noqa: BLE001
            out["engine_version"] = f"unavailable: {type(e).__name__}"
        try:
            data = requests.get(self.base_url + "/v1/models", timeout=30, headers=self.headers).json().get("data", [])
            m = [x for x in data if x.get("id") == self.model]
            out["max_model_len"] = m[0].get("max_model_len") if m else None
            out["served_models"] = [x.get("id") for x in data]
        except Exception as e:  # noqa: BLE001
            out["models_error"] = f"{type(e).__name__}"
        return out

    def payload(self, messages, schema, seed, sampling, max_tokens, extra, structured) -> dict:
        p: dict = {"model": self.model, "messages": messages, "seed": int(seed), "max_tokens": int(max_tokens),
                   "temperature": float(sampling["temperature"]), "top_p": float(sampling["top_p"])}
        if sampling.get("top_k"):
            p["top_k"] = int(sampling["top_k"])
        if sampling.get("min_p"):
            p["min_p"] = float(sampling["min_p"])
        if sampling.get("repetition_penalty", 1.0) != 1.0:
            p["repetition_penalty"] = float(sampling["repetition_penalty"])
        if structured and schema is not None:
            p["response_format"] = {"type": "json_schema", "json_schema": {"name": "tool_call", "schema": schema}}
        for k, v in (extra or {}).items():
            p[k] = v
        if self.stream:
            p["stream"] = True
            p["stream_options"] = {"include_usage": True}
        return p

    def _chat(self, messages, schema, seed, sampling, max_tokens, extra, structured) -> dict:
        p = self.payload(messages, schema, seed, sampling, max_tokens, extra, structured)
        t0 = time.time()
        url = self.base_url + "/v1/chat/completions"
        if self.stream:
            r = requests.post(url, json=p, headers=self.headers, stream=True, timeout=(30, self.timeout))
            if r.status_code >= 400:
                raise BackendError(f"HTTP {r.status_code} chat: {r.text[:500]}", status=r.status_code)
            content, reasoning, finish, usage, chunks = [], [], None, {}, 0
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                chunks += 1
                if obj.get("usage"):
                    usage = obj["usage"]
                for ch in obj.get("choices") or []:
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        content.append(delta["content"])
                    rs = delta.get("reasoning") or delta.get("reasoning_content")
                    if rs:
                        reasoning.append(rs)
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
            ms = int((time.time() - t0) * 1000)
            if chunks == 0:
                raise BackendError("empty stream", status=502)
            return {"content": "".join(content), "reasoning": "".join(reasoning),
                    "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                    "done_reason": finish, "latency_ms": ms, "raw": {"usage": usage, "chunks": chunks}}
        r = requests.post(url, json=p, headers=self.headers, timeout=self.timeout)
        ms = int((time.time() - t0) * 1000)
        if r.status_code >= 400:
            raise BackendError(f"HTTP {r.status_code} chat: {r.text[:500]}", status=r.status_code)
        try:
            data = r.json()
        except ValueError:
            raise BackendError(f"non-JSON response: {r.text[:300]}")
        if isinstance(data, dict) and data.get("error"):
            raise BackendError(f"API error: {json.dumps(data['error'])[:500]}")
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        return {"content": msg.get("content") or "", "reasoning": msg.get("reasoning") or msg.get("reasoning_content") or "",
                "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                "done_reason": choice.get("finish_reason"), "latency_ms": ms, "raw": {"usage": usage}}


class MockBackend(Backend):
    """responder(messages, context) -> str (the assistant content) or a dict with content/reasoning/done_reason."""
    name = "mock"

    def __init__(self, responder: Callable[[list[dict], dict], str | dict], model: str = "mock"):
        super().__init__("", model, timeout=1, retries=1)
        self.responder = responder
        self.log: list[dict] = []

    def _chat(self, messages, schema, seed, sampling, max_tokens, extra, structured) -> dict:
        out = self.responder(messages, {"seed": seed, "sampling": sampling, "max_tokens": max_tokens, "extra": extra,
                                        "structured": structured, "schema": schema})
        if isinstance(out, str):
            out = {"content": out}
        rec = {"content": out.get("content", ""), "reasoning": out.get("reasoning", ""),
               "prompt_tokens": sum(len(m.get("content", "")) // 4 for m in messages),
               "completion_tokens": len(out.get("content", "")) // 4, "done_reason": out.get("done_reason", "stop"),
               "latency_ms": 0, "raw": {}}
        self.log.append({"messages": messages, "response": rec})
        return rec


def make_backend(kind: str, base_url: str = "", model: str = "", **kw) -> Backend:
    if kind == "vllm":
        return VLLMBackend(base_url, model, **kw)
    raise ValueError(f"unknown backend {kind!r}; MockBackend is constructed directly")
