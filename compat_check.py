#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compat_check.py —— 换 base_url 之后，用 5 个请求把兼容性问题一次问出来。

它不依赖 openai SDK，只用标准库直接打 HTTP，目的就是看见「SDK 帮你藏起来的那一层」：
真实请求 URL、HTTP 状态码、响应头、流的首尾、错误体的形状。

用法：
  python3 compat_check.py --base-url https://api.example.com/v1 --api-key sk-xxx --model gpt-4o-mini

检查项：
  1. GET  /models            鉴权是否通过、模型清单能否读到
  2. POST /chat/completions  非流式是否正常、usage 字段是否齐全
  3. POST 同上 + stream      流是否有 [DONE]、usage 是否在最后一个 chunk
  4. POST 同上 + tools       工具调用是否真的支持（还是被静默忽略）
  5. POST 一个不存在的模型    错误是 4xx，还是 200 里包一个 error（最坑）

退出码：0 全通过；1 有失败项。
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 60


def request(method, url, key, payload=None, timeout=TIMEOUT):
    data = None
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, dict(resp.headers), body, time.time() - started
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, dict(e.headers or {}), body, time.time() - started
    except Exception as e:  # 连接层错误
        return 0, {}, f"{type(e).__name__}: {e}", time.time() - started


def try_json(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def brief(text, n=160):
    text = " ".join(text.split())
    return text[:n] + ("…" if len(text) > n else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True, help="例如 https://api.example.com/v1")
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    warn = []
    if not base.endswith("/v1"):
        warn.append("base_url 没有以 /v1 结尾 —— OpenAI SDK 会自己拼 /chat/completions，多数网关会 404")

    results = []

    # 1. /models
    status, headers, body, cost = request("GET", f"{base}/models", args.api_key, timeout=args.timeout)
    data = try_json(body)
    n_models = len((data or {}).get("data") or []) if isinstance(data, dict) else 0
    ok1 = status == 200 and n_models > 0
    results.append(("1. /models 鉴权+清单", ok1,
                    f"HTTP {status} · 模型 {n_models} 个 · {brief(body) if not ok1 else '正常'}"))

    # 2. 非流式
    payload = {"model": args.model, "messages": [{"role": "user", "content": "reply with the single word: pong"}],
               "max_tokens": 16, "temperature": 0}
    status, headers, body, cost = request("POST", f"{base}/chat/completions", args.api_key, payload, timeout=args.timeout)
    d = try_json(body) or {}
    choices = d.get("choices") or []
    text = ""
    if choices:
        text = ((choices[0].get("message") or {}).get("content") or "")
    usage = d.get("usage") or {}
    usage_missing = [k for k in ("prompt_tokens", "completion_tokens", "total_tokens") if k not in usage]
    ok2 = status == 200 and bool(choices) and bool(text)
    results.append(("2. 非流式对话", ok2,
                    f"HTTP {status} · {cost:.1f}s · 回复 {brief(text, 40) or '(空)'} · "
                    f"usage 缺 {','.join(usage_missing) if usage_missing else '无'}"))

    # 3. 流式
    stream_payload = dict(payload)
    stream_payload["stream"] = True
    stream_payload["stream_options"] = {"include_usage": True}
    status, headers, body, cost = request("POST", f"{base}/chat/completions", args.api_key, stream_payload, timeout=args.timeout)
    has_done = "[DONE]" in body
    chunks = [ln for ln in body.splitlines() if ln.startswith("data:")]
    last = chunks[-1] if chunks else ""
    has_usage_in_stream = '"usage"' in body and '"usage":null' not in body.replace(" ", "")
    ok3 = status == 200 and len(chunks) > 0 and has_done
    note3 = f"HTTP {status} · chunk {len(chunks)} · [DONE] {'有' if has_done else '**缺**'} · 末条 {brief(last, 60)}"
    results.append(("3. 流式 + 结束标志", ok3, note3))

    # 4. tools
    tool_payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": "What is the weather in Shanghai? Use the tool."}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather for a city",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                               "required": ["city"]},
            },
        }],
        "tool_choice": "auto",
        "max_tokens": 64,
    }
    status, headers, body, cost = request("POST", f"{base}/chat/completions", args.api_key, tool_payload, timeout=args.timeout)
    d = try_json(body) or {}
    ch = (d.get("choices") or [{}])[0]
    tool_calls = (ch.get("message") or {}).get("tool_calls") or []
    ok4 = status == 200 and bool(tool_calls)
    results.append(("4. tools / function calling", ok4,
                    f"HTTP {status} · tool_calls {'有' if tool_calls else '无（可能被静默忽略）'}"))

    # 5. 错误形状（故意用不存在的模型）
    bad_payload = {"model": f"__no_such_model_{int(time.time())}", "messages": [{"role": "user", "content": "hi"}],
                   "max_tokens": 8}
    status, headers, body, cost = request("POST", f"{base}/chat/completions", args.api_key, bad_payload, timeout=args.timeout)
    d = try_json(body) or {}
    has_error_field = isinstance(d, dict) and ("error" in d)
    ok5 = status >= 400 and has_error_field
    shape = "4xx + error 体（正常）" if ok5 else (
        "200 里包 error（**重试逻辑会失效**）" if status == 200 and has_error_field else f"HTTP {status} 非标准错误体")
    results.append(("5. 错误形状", ok5, f"{shape} · {brief(body, 110)}"))

    width = max(len(r[0]) for r in results) + 2
    print(f"base_url = {base}")
    print(f"model    = {args.model}")
    print("-" * (width + 70))
    failed = 0
    for name, ok, note in results:
        if not ok:
            failed += 1
        print(f"{name.ljust(width)} {'PASS' if ok else 'FAIL'}  {note}")
    print("-" * (width + 70))
    if warn:
        for w in warn:
            print(f"提示：{w}")
    print(f"结果：{len(results) - failed}/{len(results)} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
