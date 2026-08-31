# -*- coding: utf-8 -*-
"""OpenAI 兼容反代 HTTP 服务（标准库实现，零第三方依赖）。"""
import json
import hashlib
import os
import queue
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import qodercli_mgr as qm

# [2026-08-04 v2.4] 模型列表补全：新增 Qwen3.8-Max / Qwen3.8-Max-Preview / Kimi-K3（官方 8/3 上线）
# 注意：MODEL_MAP 仅用于 ①客户端别名→官方名 ②动态列表失败时的兜底。
# /v1/models 返回 = 动态列表 ∪ MODEL_MAP 官方名（合并去重），保证不缺模型。
MODEL_MAP = {
    "lite": "Lite", "efficient": "Efficient", "performance": "Performance",
    "ultimate": "Ultimate", "auto": "Auto",
    "qwen-3.8-max": "Qwen3.8-Max", "qwen3.8-max": "Qwen3.8-Max",
    "qwen-3.8-max-preview": "Qwen3.8-Max-Preview", "qwen3.8-max-preview": "Qwen3.8-Max-Preview",
    "qwen-3.7-max": "Qwen3.7-Max", "qwen3.7-max": "Qwen3.7-Max",
    "qwen-3.7-plus": "Qwen3.7-Plus", "qwen3.7-plus": "Qwen3.7-Plus",
    "deepseek-v4-pro": "DeepSeek-V4-Pro", "deepseek-v4-flash": "DeepSeek-V4-Flash",
    "ds-v4-pro": "DeepSeek-V4-Pro", "ds-v4-flash": "DeepSeek-V4-Flash",
    "glm-5.2": "GLM-5.2", "kimi-k3": "Kimi-K3", "kimi3": "Kimi-K3",
    "kimi-k2.7-code": "Kimi-K2.7-Code",
    "minimax-m3": "MiniMax-M3", "mm3": "MiniMax-M3",
}
DEFAULT_MODEL = "Auto"

# 官方倍数（2026-08 同步自 docs.qoder.com/zh/cli/model）
MODEL_PRICES = [
    ("层级", [
        ("Auto", "智能路由", "~1.0×"),
        ("Ultimate", "深度推理", "~1.6×"),
        ("Performance", "进阶推理", "~1.1×"),
        ("Efficient", "标准推理", "~0.3×"),
        ("Lite", "基础", "免费"),
    ]),
    ("具体模型", [
        ("Qwen3.8-Max", "通义千问", "0.5×（错峰 0.25×）"),
        ("Qwen3.8-Max-Preview", "通义千问", "0.5×"),
        ("Qwen3.7-Max", "通义千问", "0.5×"),
        ("Qwen3.7-Plus", "通义千问", "0.1×"),
        ("DeepSeek-V4-Pro", "深度求索", "0.5×"),
        ("DeepSeek-V4-Flash", "深度求索", "0.1×"),
        ("GLM-5.2", "智谱", "0.6×"),
        ("Kimi-K3", "月之暗面", "0.8×"),
        ("Kimi-K2.7-Code", "月之暗面", "0.3×（Fast 0.6×）"),
        ("MiniMax-M3", "MiniMax", "0.2×"),
    ]),
]

_CTX = {"username": "", "api_key": "", "pat": "",
        "agent_mode": False, "permission_mode": "", "max_turns": 20,
        "cwd": "", "identity_md": ""}

_LOG_FILE = ""
_LOG_LOCK = threading.Lock()

# [2026-08-04 v2.3] 防刷屏：qodercli 弱模型可能每轮重复决策同一 send_message
# 现象：读文件后同一内容 send_message 刷屏 10+ 次（跨请求循环，max_turns 管不到）
# 策略：模块级记录最近 send_message 签名，同一 (conv_id, content) 连续 3 次 → 强制 stop
_REPEAT_SEND_LIMIT = 3
_send_history_lock = threading.Lock()

_send_history: dict[str, int] = {}  # sig -> 连续次数

# [2026-08-04 v2.7] 在途请求去重锁：客户端超时会重发相同请求，
# 若第一个 qodercli 调用仍在跑，第二个请求直接等待复用同一结果，避免双进程并发更慢。
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict[str, dict] = {}  # key(sig) -> {"done": Event, "result": tuple or None}


def _inflight_key(prompt: str, model: str) -> str:
    return hashlib.sha256(f"{model}|{prompt}".encode("utf-8")).hexdigest()


def _inflight_run(key: str, fn):
    """同一 key 在途时等待复用；否则执行 fn() 并把结果广播给等待者。"""
    with _INFLIGHT_LOCK:
        ent = _INFLIGHT.get(key)
        if ent is None:
            ent = {"done": threading.Event(), "result": None}
            _INFLIGHT[key] = ent
            is_owner = True
        else:
            is_owner = False
    if not is_owner:
        ent["done"].wait(timeout=600)
        return ent["result"]
    try:
        result = fn()
    except BaseException as e:  # noqa: BLE001 - 异常也要广播，否则等待者永久挂起
        result = ("error", repr(e))
    finally:
        ent["result"] = result
        ent["done"].set()
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(key, None)
    return result


def _log(msg: str):
    """写代理日志（%APPDATA%\\QoderProxy\\proxy.log），用于定位用户反馈的问题。"""
    if not _LOG_FILE:
        return
    try:
        os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
        with _LOG_LOCK:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def resolve_model(requested: str) -> str:
    key = requested.strip().lower()
    if key in MODEL_MAP:
        return MODEL_MAP[key]
    for name in qm.list_models(_CTX["username"]):
        if key == name.lower():
            return name
    for qoder_name in MODEL_MAP.values():
        if requested.lower() == qoder_name.lower():
            return qoder_name
    return DEFAULT_MODEL


def _extract_identity(messages: list) -> str:
    """[2026-08-04 v2.5] 提取客户端 system prompt 作为强身份指令。

    背景：客户端反代后不知道 Qoder 系统怎么用，以为自己在原 CLI 里。
    修复：把客户端 system prompt 提取出来，包装成最高优先级身份指令注入 prompt 开头。
    """
    texts = []
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = msg.get("content", "") or ""
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        content = re.sub(r"\s*\[INSTRUCTION:[^\[\]]*?\]\s*$", "", content)
        if content.strip():
            texts.append(content.strip())
    if not texts:
        return ""
    return ("【最高优先级·身份与系统规则】以下是你所属宿主系统注入的身份与规则，"
            "你必须严格遵守，并据此理解自己的职责、可用能力与用户背景。"
            "你不是独立的命令行工具，而是在该宿主系统内工作的 AI 助手：\n\n"
            + "\n\n".join(texts))


def build_prompt(messages: list) -> str:
    parts = []
    identity = _extract_identity(messages)
    if identity:
        parts.append(identity)
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "") or ""
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        # [2026-08-04 v2.3] 过滤客户端附加的 [INSTRUCTION: 使用 send_message 工具回复...]
        # 反代模式下 qodercli 直接输出文本即成为回复，不应被该指令诱导去调 send_message
        content = re.sub(r"\s*\[INSTRUCTION:[^\[\]]*?\]\s*$", "", content)
        if role == "system":
            continue  # 已通过 _extract_identity 注入为强身份，避免重复
        if role == "tool":
            # OpenAI tool 消息：上一步工具调用的执行结果
            tid = msg.get("tool_call_id", "")
            parts.append(f"[Tool result {tid}]\n{content}")
        elif role == "assistant" and msg.get("tool_calls"):
            # assistant 带 tool_calls：说明上一步模型决定调用的工具（qodercli 不需要执行，只是上下文）
            parts.append(f"[Assistant]\n{content}")
        else:
            parts.append(f"[{role.capitalize()}]\n{content}")
    return "\n\n".join(parts)


# ── OpenAI tools/tool_calls 协议（让客户端能调用工具）──
# 机制：客户端发来 tools 清单 → 我们把清单转成自然语言指令注入 prompt，
#       要求 qodercli 在需要工具时输出一行固定格式 JSON [TOOL_CALL]{...} →
#       解析成标准 OpenAI tool_calls 返回，由客户端自己执行工具并回传结果。
TOOL_CALL_MARKER = "[TOOL_CALL]"


def build_tools_prompt(tools: list) -> str:
    """把 OpenAI function definitions 转成 qodercli 能理解的自然语言工具清单。

    关键设计：qodercli 是完整 agent，自己就有本地文件/命令工具
    （Read/Write/Edit/Bash/Glob/Grep/WebFetch/WebSearch）。这里必须明确告诉它：
    - 文件/命令操作用自己的本地工具直接执行（不需要 [TOOL_CALL]）
    - 以下列出的客户端平台工具 qodercli 没有，需要时才输出 [TOOL_CALL] 让客户端执行
    否则 qodercli 面对「读文件」需求会在清单里找不到工具，输出"工具 not found"。
    """
    lines = [
        "你运行在本机 OpenAI 兼容反代网关中。对下列外部平台工具，你没有本地执行能力，"
        "只负责理解用户需求并决策调用哪个；输出调用指令由外部客户端执行。",
        "以下是你可用的外部平台工具（由客户端提供并执行）：",
    ]
    for t in tools or []:
        fn = t.get("function", {}) if isinstance(t, dict) else {}
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})
        props = params.get("properties", {}) if isinstance(params, dict) else {}
        required = params.get("required", []) if isinstance(params, dict) else []
        arg_desc = []
        for pname, pinfo in props.items():
            ptype = pinfo.get("type", "string") if isinstance(pinfo, dict) else "string"
            pdesc = pinfo.get("description", "") if isinstance(pinfo, dict) else ""
            need = "必填" if pname in required else "可选"
            arg_desc.append(f"{pname}({ptype},{need}){':' + pdesc if pdesc else ''}")
        lines.append(f"- {name}: {desc}" + (f" 参数: {', '.join(arg_desc)}" if arg_desc else ""))
    lines.append(
        f"当且仅当需要调用以上平台工具时，请只输出一行 JSON，格式：\n"
        f"{TOOL_CALL_MARKER}{{\"name\": \"工具名\", \"arguments\": {{\"参数名\": 值}}}}\n"
        f"不要输出其他任何文字。如果不需要调用平台工具，直接正常回答。\n"
        f"注意：\n"
        f"1. send_message 是向用户发送消息的工具。你的最终回复（文字）会被客户端自动发送给用户，"
        f"所以**普通回复不要调用 send_message**，直接输出文字即可。\n"
        f"2. 只有当你需要主动向另一个会话/群聊推送消息时，才调用 send_message。\n"
        f"3. **严禁重复调用同一工具**：如果某个工具你已经调用过、或之前对话中已经发送过相同内容，"
        f"绝对不要再调用。任务完成就输出最终文字结束。"
    )
    return "\n".join(lines)


def _fix_json_string_newlines(seg: str) -> str:
    """[2026-08-04 v2.6] 宽松化：把字符串值内的真实换行/制表符替换为转义形式。

    背景：qodercli（尤其 DeepSeek-V4-Flash）输出 JSON 时，content 等长文本
    常直接输出真实换行（未转义 \n），json.loads 直接拒绝 → 工具调用解析失败。
    此函数只处理字符串值内部，不破坏 JSON 结构。
    """
    out = []
    in_str = False
    esc = False
    for c in seg:
        if in_str:
            if esc:
                out.append(c)
                esc = False
            elif c == "\\":
                out.append(c)
                esc = True
            elif c == '"':
                out.append(c)
                in_str = False
            elif c == "\n":
                out.append("\\n")
            elif c == "\r":
                out.append("\\r")
            elif c == "\t":
                out.append("\\t")
            else:
                out.append(c)
        else:
            if c == '"':
                in_str = True
            out.append(c)
    return "".join(out)


def _extract_json_obj(text: str) -> dict | None:
    """从文本中提取第一个 JSON 对象（支持前后有杂质文字）。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                seg = text[start:i + 1]
                try:
                    return json.loads(seg)
                except Exception:
                    # 宽松解析：字符串内真实换行 → 转义后重试
                    try:
                        return json.loads(_fix_json_string_newlines(seg))
                    except Exception:
                        return None
    return None


def parse_tool_calls(text: str) -> list:
    """从 qodercli 输出解析工具调用。返回 OpenAI tool_calls 列表。"""
    calls = []
    rest = text or ""
    while True:
        idx = rest.find(TOOL_CALL_MARKER)
        if idx < 0:
            break
        seg = rest[idx + len(TOOL_CALL_MARKER):]
        obj = _extract_json_obj(seg)
        if not obj or not isinstance(obj.get("name"), str) or not obj["name"]:
            rest = seg[1:]
            continue
        name = obj["name"]
        args = obj.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                try:
                    args = json.loads(_fix_json_string_newlines(args))
                except Exception:
                    args = {"raw": args}
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })
        # [2026-08-04 v2.6] 用 _extract_json_obj 定位的完整 JSON 长度推进 rest，
        # 而不是 seg.find("}")（content 内含 } 会导致截断、多调用解析错位）
        rest = _skip_first_json(seg)
    return calls


def _skip_first_json(text: str) -> str:
    """返回 text 去掉第一个 JSON 对象之后的剩余部分（含杂质容错）。"""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:]
    return ""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth_ok(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {_CTX['api_key']}"

    def do_GET(self):
        p = self.path.rstrip("/")
        if p == "/health":
            self._send(200, {"status": "ok", "username": _CTX["username"]})
        elif p == "/v1/models":
            # [2026-08-04 v2.4] 合并去重：动态列表（qodercli --list-models）+ MODEL_MAP 官方名
            # 之前是 `动态 or 静态`，只要动态列表非空（哪怕旧版 qodercli 缺新模型）就不会补全
            dyn = qm.list_models(_CTX["username"]) or []
            static = list(dict.fromkeys(MODEL_MAP.values()))
            names = list(dict.fromkeys(dyn + static))
            models = [{"id": n, "object": "model",
                       "created": int(time.time()), "owned_by": "qoder"}
                      for n in names]
            self._send(200, {"object": "list", "data": models})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send(404, {"error": {"message": "not found"}})
            return
        if not self._auth_ok():
            _log(f"401 auth_fail path={self.path} agent={self.headers.get('User-Agent', '')[:60]}")
            self._send(401, {"error": {"message": "Invalid API key", "code": "invalid_api_key"}})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            _log(f"400 body_parse_fail path={self.path}")
            self._send(400, {"error": {"message": "bad request body"}})
            return
        messages = body.get("messages", [])
        if not messages:
            _log("400 no_messages")
            self._send(400, {"error": {"message": "messages required"}})
            return
        model_name = body.get("model", DEFAULT_MODEL)
        qoder_model = resolve_model(model_name)
        tools = body.get("tools") or []
        has_tools = isinstance(tools, list) and len(tools) > 0
        stream = bool(body.get("stream"))
        prompt = build_prompt(messages)
        _log(f"REQ model={model_name!r} resolved={qoder_model} msgs={len(messages)} "
             f"prompt_len={len(prompt)} stream={stream} tools={len(tools) if has_tools else 0} "
             f"agent={_CTX['agent_mode']} "
             f"pm={_CTX['permission_mode']!r} cwd={_CTX['cwd']!r} "
             f"ua={self.headers.get('User-Agent', '')[:60]}")
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        # ── 工具决策路径：客户端平台工具 ──
        # 客户端自己有工具执行环境，qodercli 只负责"决策调哪个工具"，
        # 输出 [TOOL_CALL] JSON，由 proxy 解析成标准 tool_calls 返回客户端执行。
        # [2026-08-04 v2.7] 强制 agent_mode=False：此路径不需要 qodercli 的本地 agent
        # 能力（Read/Bash 等），让它自主在 cwd（如 C:\）跑 agent 循环反而 ①极慢（4分钟/次）
        # ②危险（bypassPermissions 会在本机乱执行命令）。纯决策模式只让模型输出文字/[TOOL_CALL]。
        if has_tools:
            prompt = prompt + "\n\n" + build_tools_prompt(tools)
            try:
                # [2026-08-04 v2.7] 在途去重：客户端超时重发的相同请求直接等待复用，
                # 不另起 qodercli 进程（旧逻辑每个 REQ 都起一个进程，双跑更慢）。
                output = _inflight_run(
                    _inflight_key(prompt, qoder_model),
                    lambda: qm.run_qodercli(_CTX["username"], _CTX["pat"], qoder_model, prompt,
                                            agent_mode=False,
                                            permission_mode="",
                                            max_turns=1,
                                            timeout=300,
                                            cwd=_CTX["cwd"],
                                            identity_md=_CTX["identity_md"]),
                )
                if isinstance(output, tuple) and output[0] == "error":
                    raise RuntimeError(output[1])
            except RuntimeError as e:
                _log(f"TOOL-DECISION-FAIL model={qoder_model} err={e!r}")
                self._send(502, {"error": {"message": str(e), "code": "qodercli_error"}})
                return
            calls = parse_tool_calls(output)
            _log(f"TOOL-DECISION model={qoder_model} tools={len(tools)} "
                 f"calls={len(calls)} out_len={len(output)} "
                 f"out_head={output[:150]!r} "
                 f"pm={_CTX['permission_mode']!r} cwd={_CTX['cwd']!r}")

            # [2026-08-04 v2.3] 防刷屏：同一 send_message 连续 3 次 → 强制 stop
            # 背景：qodercli（DeepSeek-V4-Flash）不理解 send_message 已发送成功，
            #       每轮 REQ 都决策发同一内容，客户端每轮都真发 → 刷屏 10+ 次。
            # 检测：统计最近连续相同 (conv_id, content) 的 send_message 调用。
            _sm_sig = ""
            for _tc in calls:
                _fn = _tc.get("function", {})
                if _fn.get("name") == "send_message":
                    try:
                        _args = json.loads(_fn.get("arguments", "{}"))
                    except Exception:
                        _args = {}
                    _sm_sig = f"{_args.get('conv_id','')}|{_args.get('content','')}"
                    break
            _blocked = False
            if _sm_sig:
                with _send_history_lock:
                    if _send_history.get(_sm_sig, 0) >= _REPEAT_SEND_LIMIT:
                        _blocked = True
                    else:
                        _send_history[_sm_sig] = _send_history.get(_sm_sig, 0) + 1
            if _blocked:
                _log(f"REPEAT-SEND-BLOCK sig={_sm_sig[:80]}... 连续 {_REPEAT_SEND_LIMIT} 次相同 send_message，强制 stop")
                message = {"role": "assistant", "content": "✅ 消息已发送。任务完成。"}
                finish_reason = "stop"
                calls = []
            elif calls:
                message = {"role": "assistant", "content": None, "tool_calls": calls}
                finish_reason = "tool_calls"
            elif TOOL_CALL_MARKER in output:
                # [2026-08-04 v2.6] qodercli 想调工具但 JSON 解析失败——绝不把原始 [TOOL_CALL] 文本
                # 当回复返回（会泄漏给用户/客户端）。返回简短失败说明，让客户端重试或降级。
                _log(f"TOOL-CALL-PARSE-FAIL model={qoder_model} out_len={len(output)} "
                     f"head={output[:200]!r}")
                message = {"role": "assistant",
                           "content": "⚠️ 工具调用解析失败（qodercli 输出格式异常），请重试或换个说法。"}
                finish_reason = "stop"
            else:
                message = {"role": "assistant", "content": output}
                finish_reason = "stop"
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if calls:
                    tc_chunk = {"id": chat_id, "object": "chat.completion.chunk",
                                "created": created, "model": model_name,
                                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(tc_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                    for i, tc in enumerate(calls):
                        delta = {"tool_calls": [{**tc, "index": i}]}
                        chunk = {"id": chat_id, "object": "chat.completion.chunk",
                                 "created": created, "model": model_name,
                                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                        self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                else:
                    chunk = {"id": chat_id, "object": "chat.completion.chunk",
                             "created": created, "model": model_name,
                             "choices": [{"index": 0, "delta": {"role": "assistant", "content": output}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                done = {"id": chat_id, "object": "chat.completion.chunk",
                        "created": created, "model": model_name,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
                self.wfile.write(f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self._send(200, {"id": chat_id, "object": "chat.completion", "created": created,
                                 "model": model_name,
                                 "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                                 "usage": {"prompt_tokens": len(prompt) // 4,
                                           "completion_tokens": len(output) // 4,
                                           "total_tokens": (len(prompt) + len(output)) // 4}})
            return

        # ── 旧路径：裸 qodercli subprocess（兼容回退）──
        try:
            output = qm.run_qodercli(_CTX["username"], _CTX["pat"], qoder_model, prompt,
                                     agent_mode=_CTX["agent_mode"],
                                     permission_mode=_CTX["permission_mode"],
                                     max_turns=_CTX["max_turns"], cwd=_CTX["cwd"],
                                     identity_md=_CTX["identity_md"])
            _log(f"OK model={qoder_model} out_len={len(output)}")
        except RuntimeError as e:
            _log(f"FAIL model={qoder_model} err={e!r}")
            self._send(502, {"error": {"message": str(e), "code": "qodercli_error"}})
            return
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = {"id": chat_id, "object": "chat.completion.chunk", "created": created,
                     "model": model_name,
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": output}, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            done = {"id": chat_id, "object": "chat.completion.chunk", "created": created,
                    "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            self.wfile.write(f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self._send(200, {"id": chat_id, "object": "chat.completion", "created": created,
                             "model": model_name,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": output}, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": len(prompt) // 4,
                                       "completion_tokens": len(output) // 4,
                                       "total_tokens": (len(prompt) + len(output)) // 4}})


def start_server(username: str, api_key: str, pat: str, port: int = 8080,
                 agent_mode: bool = False, permission_mode: str = "",
                 max_turns: int = 20, cwd: str = "", identity_md: str = ""):
    _CTX.update(username=username, api_key=api_key, pat=pat,
                agent_mode=agent_mode, permission_mode=permission_mode,
                max_turns=max_turns, cwd=cwd, identity_md=identity_md)
    global _LOG_FILE
    _LOG_FILE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                             "QoderProxy", "proxy.log")
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _log(f"START username={username} port={port} agent={agent_mode} pm={permission_mode!r} cwd={cwd!r}")
    return server
