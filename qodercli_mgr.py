# -*- coding: utf-8 -*-
"""qodercli 管理：路径探测、登录、调用、配置持久化、DPAPI 加密。"""
import base64
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_NAME = "QoderProxy"


# ── 目录与配置 ──────────────────────────────────────────────
def appdata_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_file() -> Path:
    return appdata_dir() / "config.json"


def load_config() -> dict:
    try:
        with open(config_file(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(config_file(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        if sys.platform != "win32":
            # macOS/Linux：配置文件含 pat_enc，收紧为仅本人可读
            try:
                os.chmod(config_file(), 0o600)
            except Exception:
                pass
    except Exception:
        pass


# ── DPAPI 加密（Windows 用户级）─────────────────────────────
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        # 非 Windows（macOS/Linux）：无 DPAPI，退化为文件权限保护
        # （说明见 README：PAT 明文存本地 600 权限文件）
        return data
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if ctypes.windll.crypt32.CryptProtectData(ctypes.byref(blob_in), None, None,
                                              None, None, 0, ctypes.byref(blob_out)):
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return raw
    return b""


def _dpapi_unprotect(data: bytes) -> bytes:
    if sys.platform != "win32":
        return data
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None,
                                                None, None, 0, ctypes.byref(blob_out)):
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return raw
    return b""


def encrypt_text(text: str) -> str:
    if not text:
        return ""
    try:
        if sys.platform != "win32":
            return text  # macOS/Linux 明文保存（config 600 权限保护）
        return base64.b64encode(_dpapi_protect(text.encode("utf-8"))).decode()
    except Exception:
        return ""


def decrypt_text(blob: str) -> str:
    if not blob:
        return ""
    try:
        if sys.platform != "win32":
            return blob  # 明文直读
        return _dpapi_unprotect(base64.b64decode(blob)).decode("utf-8", "replace")
    except Exception:
        return ""


# ── qodercli 探测 ─────────────────────────────────────────────
def get_qodercli_path() -> str:
    """动态探测 qodercli，不依赖具体用户名/版本号。"""
    import glob
    home = str(Path.home())
    appdata = os.environ.get("APPDATA", home)
    # 1) 常见安装位置（路径用系统变量拼接，换用户一样生效）
    candidates = [
        os.path.join(home, ".qoder", "bin", "qodercli", "qodercli.exe"),
        os.path.join(home, ".qoder", "bin", "qodercli.exe"),
        os.path.join(appdata, "npm", "qodercli.cmd"),
        os.path.join(appdata, "npm", "qodercli.exe"),
        os.path.join(home, ".local", "bin", "qodercli"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    # 2) 版本化目录扫描（qodercli-*.exe，未来版本文件名变化也能找到，取最新）
    ver_dir = os.path.join(home, ".qoder", "bin", "qodercli")
    if os.path.isdir(ver_dir):
        exes = [f for f in glob.glob(os.path.join(ver_dir, "qodercli*.exe")) if os.path.isfile(f)]
        if exes:
            exes.sort(key=lambda f: os.path.getmtime(f), reverse=True)
            return exes[0]
    # 3) PATH 兜底（公司统一装到别的目录时也能找到）
    return shutil.which("qodercli") or "qodercli"


def qodercli_installed() -> bool:
    return os.path.isfile(get_qodercli_path())


def _require_qodercli() -> str:
    """确保 qodercli 可执行文件可用，否则抛 RuntimeError。"""
    p = get_qodercli_path()
    if not os.path.isfile(p):
        raise RuntimeError(f"找不到 qodercli，请先安装（探测到: {p}）")
    return p


# ── 用户隔离配置目录（多账户）────────────────────────────────
def user_config_dir(username: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in username)[:40] or "default"
    d = appdata_dir() / "accounts" / safe
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _env_for(username: str, pat: str = "") -> dict:
    env = os.environ.copy()
    env["QODER_CONFIG_DIR"] = user_config_dir(username)
    if pat:
        env["QODER_PERSONAL_ACCESS_TOKEN"] = pat
    bin_dir = os.path.dirname(get_qodercli_path())
    if bin_dir and bin_dir not in env.get("PATH", ""):
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return env


# ── 身份适配（AGENTS.md 静态记忆注入）────────────────────────
def _default_identity_md() -> str:
    """默认身份规则：通用 Qoder 助手（不绑定任何平台）。"""
    return """# 身份与系统规则（QoderProxy 自动维护，勿删）

你是运行在本机 OpenAI 兼容反代网关中的 Qoder AI 助手。
你通过 qodercli 提供智能回答，可以按需使用本地工具处理任务。

## 工具调用协议
- 需要调用本机代理注册的外部工具时，输出 `[TOOL_CALL]` 后紧跟一个 JSON 对象（注意大小写一致）。
- 格式：`[TOOL_CALL]{"name": "工具名", "arguments": {...}}`
- `arguments` 必须是合法 JSON；字符串值里的换行必须写成 `\\n`，不要使用真实换行。
- 每次只输出一个工具调用，等外部结果返回后再继续。
"""


def ensure_identity_agents(username: str, content: str = "") -> str:
    """把身份规则写入用户配置目录 AGENTS.md（Qoder 用户级静态记忆，每次会话自动加载）。
    返回写入的路径；失败返回空串。"""
    try:
        p = Path(user_config_dir(username)) / "AGENTS.md"
        body = content.strip() or _default_identity_md()
        body = body.strip() + "\n"
        if p.exists():
            try:
                if p.read_text(encoding="utf-8") == body:
                    return str(p)
            except Exception:
                pass
        p.write_text(body, encoding="utf-8")
        return str(p)
    except Exception:
        return ""


def _run(cmd: list, env: dict, timeout: int, input_text: str = ""):
    if sys.platform == "win32" and cmd[0].lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c"] + cmd
    kwargs = {}
    if sys.platform == "win32":
        # GUI (--noconsole) 进程 spawn 控制台子进程时，Windows 会弹新黑框；
        # CREATE_NO_WINDOW 让子进程不创建新控制台窗口（不影响浏览器登录弹窗）
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          env=env, encoding="utf-8", errors="replace",
                          input=input_text or None, **kwargs)


# ── 登录与验证 ───────────────────────────────────────────────
def login_browser(username: str) -> tuple:
    """浏览器登录（独立配置目录）。返回 (ok, message)。"""
    try:
        path = _require_qodercli()
    except RuntimeError as e:
        return False, str(e)
    env = _env_for(username)
    cmd = [path, "login"]
    try:
        r = _run(cmd, env, timeout=300)
        out = (r.stdout or r.stderr or "").strip()
        if r.returncode == 0:
            return True, out or "登录成功"
        return False, out or "登录失败"
    except (FileNotFoundError, PermissionError):
        return False, "找不到 qodercli，请先安装"
    except Exception as e:
        return False, str(e)


def test_auth(username: str, pat: str = "") -> tuple:
    """验证登录态 / PAT。返回 (ok, message)。"""
    try:
        path = _require_qodercli()
    except RuntimeError as e:
        return False, str(e)
    env = _env_for(username, pat)
    cmd = [path, "-p", "--model", "Lite", "Say hello"]
    try:
        r = _run(cmd, env, timeout=120)
        out = (r.stdout or r.stderr or "").strip()
        if r.returncode == 0:
            return True, out
        if "not logged in" in out.lower():
            return False, "未登录，请先完成登录"
        return False, out or f"退出码 {r.returncode}"
    except (FileNotFoundError, PermissionError):
        return False, "找不到 qodercli，请先安装"
    except subprocess.TimeoutExpired:
        return False, "验证超时，请检查网络"
    except Exception as e:
        return False, str(e)


_MODELS_CACHE: list = []
_MODELS_TS: float = 0.0


def list_models(username: str = "", cache_seconds: int = 300) -> list:
    """列出当前账户可用模型（qodercli --list-models，5 分钟缓存）。失败返回缓存/空。"""
    global _MODELS_CACHE, _MODELS_TS
    if _MODELS_CACHE and time.time() - _MODELS_TS < cache_seconds:
        return _MODELS_CACHE
    try:
        env = _env_for(username, "") if username else os.environ.copy()
        r = _run([get_qodercli_path(), "--list-models"], env, timeout=30)
        names = [ln.strip() for ln in r.stdout.splitlines()
                 if ln.strip() and ln.strip().upper() != "MODEL"]
        if names:
            _MODELS_CACHE = names
            _MODELS_TS = time.time()
            return names
    except Exception:
        pass
    return list(_MODELS_CACHE)


def run_qodercli(username: str, pat: str, model: str, prompt: str, timeout: int = 180,
                 agent_mode: bool = False, permission_mode: str = "",
                 max_turns: int = 20, cwd: str = "", identity_md: str = "") -> str:
    """调用 qodercli 生成一次回答（可开启 Agent 模式：让 qodercli 自主读/改文件、跑命令）。
    未登录抛 RuntimeError。"""
    # [2026-08-04 v5] 身份适配：每次调用前把身份规则写入 QODER_CONFIG_DIR/AGENTS.md
    # （Qoder 用户级静态记忆，会话启动自动加载）——让 qodercli 稳定认知自己的助手身份
    ensure_identity_agents(username, identity_md)
    path = _require_qodercli()
    env = _env_for(username, pat)
    cmd = [path, "-p", "--model", model]
    if agent_mode:
        pm = permission_mode or ""
        pm_map = {"允许编辑": "acceptEdits", "保守": "", "全部自动": "bypassPermissions",
                  "conservative": "", "accept_edits": "acceptEdits", "acceptEdits": "acceptEdits",
                  "bypass_permissions": "bypassPermissions", "bypassPermissions": "bypassPermissions",
                  "yolo": "bypassPermissions"}
        pm = pm_map.get(pm, pm)
        if pm == "bypassPermissions":
            cmd += ["--permission-mode", "bypassPermissions", "--dangerously-skip-permissions"]
        elif pm:
            cmd += ["--permission-mode", pm]
        cmd += ["--max-turns", str(max_turns)]
        if cwd:
            cmd += ["-w", cwd]
    # prompt 通过 stdin 传入（不走命令行，避免 Windows 命令行长度上限 WinError 206）
    try:
        r = _run(cmd, env, timeout, input_text=prompt)
    except (FileNotFoundError, PermissionError) as e:
        raise RuntimeError(f"找不到 qodercli，请先安装（{type(e).__name__}: {e}）")
    except subprocess.TimeoutExpired:
        # [2026-08-04 v2.7] 超时必须转成 RuntimeError 上抛，否则 ThreadingHTTPServer 的
        # 请求线程会因未捕获异常直接崩溃，连接永久挂起 = 客户端以为「卡住」
        raise RuntimeError(f"qodercli 调用超时（>{timeout}s），请重试或换个模型")
    if r.returncode != 0:
        err = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
        if "not logged in" in err.lower():
            raise RuntimeError("未登录，请重新打开小程序确认账户")
        raise RuntimeError(err)
    return (r.stdout or r.stderr).strip()


def derive_api_key(username: str) -> str:
    """同一账户 API Key 固定，方便客户端一次配置。"""
    return hashlib.sha256(("qoderproxy|" + username).encode("utf-8")).hexdigest()[:32]
