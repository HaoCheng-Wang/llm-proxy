#!/usr/bin/env python3
"""统计某个代理端口全部请求的 token 用量。

数据来源
----
requests 表的 response_body 字段。代理拦截到的每条响应都以 JSON 形式保存，
其中 usage 字段携带服务商返回的 token 计数，三种协议各有各的字段名：

- OpenAI      : usage.prompt_tokens / completion_tokens / total_tokens
                (+ prompt_tokens_details.cached_tokens,
                   completion_tokens_details.reasoning_tokens,
                   prompt_cache_hit_tokens / prompt_cache_miss_tokens)
- Anthropic   : usage.input_tokens / output_tokens
                (+ cache_read_input_tokens / cache_creation_input_tokens)
- Gemini      : usageMetadata.promptTokenCount / candidatesTokenCount / totalTokenCount

流式响应由 sse_parsers.py 重建为完整 JSON，usage 已被合并保留；若重建失败
（reconstruction_error=1）则退回对 response_body_raw 做正则提取。

用法
----
python scripts/count_tokens.py 28889                 # 统计端口 28889
python scripts/count_tokens.py 28889 --by-day        # 额外按天分组
python scripts/count_tokens.py 28889 --json          # 输出 JSON 便于二次处理
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import pymysql
from sqlalchemy.engine import make_url

from config import DATABASE_URL

# ── 各协议字段名 → 统一键 ──────────────────────────────────────────────
_PROMPT_KEYS = ("prompt_tokens", "input_tokens", "promptTokenCount")
_COMPLETION_KEYS = ("completion_tokens", "output_tokens", "candidatesTokenCount")
_TOTAL_KEYS = ("total_tokens", "totalTokenCount")
_CACHED_KEYS = ("cache_read_input_tokens", "prompt_cache_hit_tokens",
                "cachedContentTokenCount")

# 正则兜底：JSON 被截断（超大响应体）时直接从文本里抓最后一个数字。
# 流式响应里 usage 总在末尾出现，findall 取最后一个即是最终值。
_RE_PROMPT = re.compile(r'"(?:prompt_tokens|input_tokens|promptTokenCount)"\s*:\s*(\d+)')
_RE_COMPLETION = re.compile(r'"(?:completion_tokens|output_tokens|candidatesTokenCount)"\s*:\s*(\d+)')
_RE_TOTAL = re.compile(r'"(?:total_tokens|totalTokenCount)"\s*:\s*(\d+)')
_RE_CACHED = re.compile(r'"(?:prompt_cache_hit_tokens|cache_read_input_tokens)"\s*:\s*(\d+)')
_RE_REASONING = re.compile(r'"(?:reasoning_tokens)"\s*:\s*(\d+)')


def _num(d, *keys):
    """从 dict 中按顺序取第一个存在的数字字段。"""
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


def _extract_from_obj(obj):
    """从解析好的响应 JSON 中取 usage。"""
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage") or obj.get("usageMetadata")
    if not isinstance(usage, dict):
        return None

    prompt = _num(usage, *_PROMPT_KEYS)
    completion = _num(usage, *_COMPLETION_KEYS)
    total = _num(usage, *_TOTAL_KEYS)
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)

    cached = _num(usage, *_CACHED_KEYS)
    if cached is None:
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = _num(details, "cached_tokens")
    reasoning = None
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        reasoning = _num(details, "reasoning_tokens")

    # OpenAI/Anthropic use "model"; Gemini's reconstructed body uses
    # "modelVersion" (see GeminiSSEParser.finalize).
    model = obj.get("model") or obj.get("modelVersion")
    return {
        "prompt": prompt, "completion": completion, "total": total,
        "cached": cached, "reasoning": reasoning,
        "model": model if isinstance(model, str) and model else None,
    }


def _extract_by_regex(text_body):
    """JSON 解析失败时的兜底：正则取最后一个匹配。"""
    def last(rx):
        m = rx.findall(text_body)
        return int(m[-1]) if m else None

    prompt, completion = last(_RE_PROMPT), last(_RE_COMPLETION)
    total = last(_RE_TOTAL)
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)
    if total is None and prompt is None and completion is None:
        return None
    return {
        "prompt": prompt, "completion": completion, "total": total,
        "cached": last(_RE_CACHED), "reasoning": last(_RE_REASONING),
        "model": None,
    }


def extract_usage(response_body):
    """返回 (usage_dict | None, source)，source ∈ {json, regex, none}。"""
    if not response_body:
        return None, "none"

    obj = None
    try:
        obj = json.loads(response_body)
    except (ValueError, TypeError):
        pass

    if obj is not None:
        usage = _extract_from_obj(obj)
        if usage and usage["total"] is not None:
            return usage, "json"

    usage = _extract_by_regex(response_body)
    if usage:
        # 正则兜底时模型名从文本里抓一次
        if usage["model"] is None:
            m = re.search(r'"(?:model|modelVersion)"\s*:\s*"([^"]+)"',
                          response_body)
            if m:
                usage["model"] = m.group(1)
        return usage, "regex"

    if obj is not None:
        usage = _extract_from_obj(obj)
        if usage:
            return usage, "json"
    return None, "none"


def connect(url, streaming=False):
    """SSCursor（服务端游标）逐行拉取，避免一次性把全部响应体读进内存。"""
    kwargs = {"cursorclass": pymysql.cursors.SSCursor} if streaming else {}
    return pymysql.connect(
        host=url.host, port=url.port or 3306, user=url.username,
        password=url.password, database=url.database,
        charset="utf8mb4", connect_timeout=10, read_timeout=3600, **kwargs,
    )


def resolve_port(url, port_number):
    conn = connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, target_url FROM ports WHERE port_number=%s", (port_number,)
            )
            return cur.fetchone()
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="按端口统计 token 用量")
    parser.add_argument("port", type=int, help="代理端口号，如 28889")
    parser.add_argument("--by-day", action="store_true", help="额外按天分组统计")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    url = make_url(DATABASE_URL)
    row = resolve_port(url, args.port)
    if not row:
        print(f"❌ 端口 {args.port} 不存在", file=sys.stderr)
        return 1
    port_id, target_url = row

    sql = (
        "SELECT id, created_at, reconstruction_error, response_body, response_body_raw "
        "FROM requests WHERE port_id=%s ORDER BY id"
    )
    conn = connect(url, streaming=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (port_id,))

            n_requests = 0
            n_usage = 0
            n_no_usage = 0
            n_regex = 0
            n_recon_error = 0
            prompt_sum = completion_sum = total_sum = 0
            cached_sum = reasoning_sum = 0
            n_with_cached = n_with_reasoning = 0
            by_model = defaultdict(lambda: {"requests": 0, "prompt": 0, "completion": 0, "total": 0})
            by_day = defaultdict(lambda: {"requests": 0, "prompt": 0, "completion": 0, "total": 0})
            first_at = last_at = None

            for rid, created_at, recon_err, body, raw in cur:
                n_requests += 1
                if recon_err:
                    n_recon_error += 1
                first_at = first_at or created_at
                last_at = created_at

                usage, source = extract_usage(body)
                if usage is None and raw:
                    usage, source = extract_usage(raw)
                if usage is None or usage["total"] is None:
                    n_no_usage += 1
                    continue

                n_usage += 1
                if source == "regex":
                    n_regex += 1

                p = usage["prompt"] or 0
                c = usage["completion"] or 0
                t = usage["total"] or 0
                prompt_sum += p
                completion_sum += c
                total_sum += t
                if usage["cached"] is not None:
                    cached_sum += usage["cached"]
                    n_with_cached += 1
                if usage["reasoning"] is not None:
                    reasoning_sum += usage["reasoning"]
                    n_with_reasoning += 1

                model = usage["model"] or "(unknown)"
                by_model[model]["requests"] += 1
                by_model[model]["prompt"] += p
                by_model[model]["completion"] += c
                by_model[model]["total"] += t

                if args.by_day and created_at:
                    day = created_at.strftime("%Y-%m-%d")
                    by_day[day]["requests"] += 1
                    by_day[day]["prompt"] += p
                    by_day[day]["completion"] += c
                    by_day[day]["total"] += t
    finally:
        conn.close()

    result = {
        "port": args.port,
        "port_id": port_id,
        "target_url": target_url,
        "requests": n_requests,
        "requests_with_usage": n_usage,
        "requests_without_usage": n_no_usage,
        "requests_parsed_by_regex": n_regex,
        "reconstruction_errors": n_recon_error,
        "prompt_tokens": prompt_sum,
        "completion_tokens": completion_sum,
        "total_tokens": total_sum,
        "cached_tokens": cached_sum if n_with_cached else None,
        "reasoning_tokens": reasoning_sum if n_with_reasoning else None,
        "first_request_at": str(first_at) if first_at else None,
        "last_request_at": str(last_at) if last_at else None,
        "by_model": {k: dict(v) for k, v in sorted(by_model.items(), key=lambda kv: -kv[1]["total"])},
    }
    if args.by_day:
        result["by_day"] = {k: dict(v) for k, v in sorted(by_day.items())}

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"端口 {args.port} (port_id={port_id}) → {target_url}")
    print(f"时间范围: {first_at} ~ {last_at}")
    print(f"请求总数: {n_requests}    含 usage: {n_usage}    无 usage: {n_no_usage}"
          f"    (正则兜底 {n_regex}, 重建失败 {n_recon_error})")
    print()
    print(f"  输入 tokens (prompt)     : {prompt_sum:>15,}")
    print(f"  输出 tokens (completion) : {completion_sum:>15,}")
    print(f"  合计 tokens              : {total_sum:>15,}")
    if n_with_cached:
        print(f"  其中缓存命中 (cached)    : {cached_sum:>15,}  ({n_with_cached} 条)")
    if n_with_reasoning:
        print(f"  其中推理 (reasoning)     : {reasoning_sum:>15,}  ({n_with_reasoning} 条)")
    print()
    print("按模型:")
    for model, s in sorted(by_model.items(), key=lambda kv: -kv[1]["total"]):
        print(f"  {model:<28} {s['requests']:>6} 条  "
              f"in={s['prompt']:>13,}  out={s['completion']:>12,}  total={s['total']:>14,}")
    if args.by_day:
        print()
        print("按天:")
        for day, s in sorted(by_day.items()):
            print(f"  {day}  {s['requests']:>6} 条  "
                  f"in={s['prompt']:>13,}  out={s['completion']:>12,}  total={s['total']:>14,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
