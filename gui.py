# -*- coding: utf-8 -*-
"""tkinter 图形界面：账户确认页 + 反代结果页。"""
import os
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import qodercli_mgr as qm
import proxy_server as ps

PORT = 8080
VERSION = "v3.0"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Qoder 额度反代助手 {VERSION}")
        self.geometry("700x620")
        self.resizable(False, False)
        self.cfg = qm.load_config()
        self.server = None
        self.pat = ""
        self.remember = tk.BooleanVar(value=self.cfg.get("remember", True))
        self.agent_mode = tk.BooleanVar(value=self.cfg.get("agent_mode", False))
        self.perm_var = tk.StringVar(value=self.cfg.get("permission_mode", "允许编辑"))
        self.max_turns_var = tk.IntVar(value=int(self.cfg.get("max_turns", 20)))
        self.cwd_var = tk.StringVar(value=self.cfg.get("cwd", "") or os.path.expanduser("~"))
        self._build_login()

    # ── 登录 / 确认页 ──────────────────────────
    def _build_login(self):
        for w in self.winfo_children():
            w.destroy()
        f = ttk.Frame(self, padding=24)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Qoder 额度反代助手", font=("Arial", 16, "bold")).pack(anchor="w")
        ttk.Label(f, text=f"版本 {VERSION} ｜ 用你的 Qoder 账户额度，在本机开放一个 OpenAI 兼容接口。",
                  foreground="#666").pack(anchor="w", pady=(2, 14))
        ttk.Label(f, text="Qoder 用户名（邮箱）").pack(anchor="w")
        self.entry_user = ttk.Entry(f, font=("Consolas", 11))
        self.entry_user.pack(fill="x", pady=(2, 8))
        self.entry_user.insert(0, self.cfg.get("username", ""))
        ttk.Label(f, text="密码 / PAT（留空 = 浏览器登录；填写 = 免浏览器，PAT 在 qoder.com/account/integrations 生成）",
                  wraplength=640).pack(anchor="w")
        self.entry_pat = ttk.Entry(f, font=("Consolas", 11), show="*")
        self.entry_pat.pack(fill="x", pady=(2, 8))
        self.entry_pat.insert(0, qm.decrypt_text(self.cfg.get("pat_enc", "")))
        ttk.Checkbutton(f, text="记住账户（下次打开自动填入，PAT 用系统加密保存）",
                        variable=self.remember).pack(anchor="w", pady=(0, 10))
        # ── Agent 模式（可选）────────────────────
        ttk.Separator(f).pack(fill="x", pady=(6, 8))
        ttk.Checkbutton(f, text="启用 Agent 模式（AI 可在本地读文件 / 改代码 / 执行命令）",
                        variable=self.agent_mode, command=self._on_agent_toggle).pack(anchor="w")
        self.agent_box = ttk.Frame(f)
        self.agent_box.pack(fill="x", pady=(4, 0))
        rowp = ttk.Frame(self.agent_box)
        rowp.pack(fill="x")
        ttk.Label(rowp, text="权限级别：").pack(side="left")
        ttk.Combobox(rowp, textvariable=self.perm_var, state="readonly", width=14,
                     values=["允许编辑", "保守", "全部自动"]).pack(side="left", padx=(0, 14))
        ttk.Label(rowp, text="最大轮数：").pack(side="left")
        ttk.Spinbox(rowp, from_=1, to=200, textvariable=self.max_turns_var, width=5).pack(side="left")
        rowc = ttk.Frame(self.agent_box)
        rowc.pack(fill="x", pady=(4, 0))
        ttk.Label(rowc, text="工作目录：").pack(side="left")
        ttk.Entry(rowc, textvariable=self.cwd_var, font=("Consolas", 10)).pack(side="left", fill="x", expand=True)
        ttk.Button(rowc, text="浏览…", command=self._pick_cwd).pack(side="left", padx=6)
        ttk.Label(f, text="保守=只读为主；允许编辑=自动批准目录内改动；全部自动=跳过所有确认（慎用）",
                  foreground="#888", wraplength=640, justify="left").pack(anchor="w", pady=(2, 8))
        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(4, 10))
        self.btn_start = ttk.Button(btns, text="开始反代", command=self._on_start)
        self.btn_start.pack(side="left")
        self.btn_install = ttk.Button(btns, text="安装 qodercli", command=self._on_install)
        self.btn_install.pack(side="left", padx=8)
        ttk.Button(btns, text="退出", command=self._on_close).pack(side="right")
        self.lbl_status = ttk.Label(f, text="", foreground="#c00", wraplength=640)
        self.lbl_status.pack(anchor="w", pady=(4, 0))
        ttk.Separator(f).pack(fill="x", pady=10)
        tip = ("使用提示：每次打开确认用户名（预填上次），点「开始反代」即可。\n"
               "v1.0 基于官方协议调用 qodercli（每次请求独立进程，稳定可靠）。\n"
               "想换账户？改用户名重新开始即可，各账户登录态互不影响。\n"
               "本程序不保存你的 Qoder 网站密码；PAT 用系统加密保存。")
        ttk.Label(f, text=tip, foreground="#888", wraplength=640, justify="left").pack(anchor="w")

    def _set_status(self, text, color="#c00"):
        self.lbl_status.config(text=text, foreground=color)

    def _on_agent_toggle(self):
        if self.agent_mode.get():
            messagebox.showinfo(
                "Agent 模式提示",
                "开启后，AI 可以在你选的工作目录内自主读文件、改代码、执行命令。\n\n"
                "· 「允许编辑」= 自动批准工作目录内的文件修改与命令\n"
                "· 「全部自动」= 跳过所有确认（高风险，仅限信任环境）\n"
                "· 本程序只监听 127.0.0.1，不对外网开放",
                parent=self)

    def _pick_cwd(self):
        d = filedialog.askdirectory(title="选择 Agent 工作目录",
                                    initialdir=self.cwd_var.get() or os.path.expanduser("~"))
        if d:
            self.cwd_var.set(d)

    # ── 开始反代（后台线程）────────────────────
    def _on_start(self):
        username = self.entry_user.get().strip()
        pat = self.entry_pat.get().strip()
        if not username:
            self._set_status("请先输入 Qoder 用户名（邮箱）")
            return
        self.btn_start.config(state="disabled")
        self.pat = pat
        threading.Thread(target=self._worker, args=(username, pat), daemon=True).start()

    def _worker(self, username, pat):
        def ui(fn):
            self.after(0, fn)

        if not qm.qodercli_installed():
            ui(lambda: self._set_status("未检测到 qodercli，请先点「安装 qodercli」按钮"))
            ui(lambda: self.btn_start.config(state="normal"))
            return
        ui(lambda: self._set_status("正在验证账户…", "#09c"))
        ok, msg = qm.test_auth(username, pat)
        if not ok and not pat:
            ui(lambda: self._set_status("正在打开浏览器登录… 请在弹出的页面登录你的 Qoder 账户", "#09c"))
            ok2, msg2 = qm.login_browser(username)
            if ok2:
                ok, msg = qm.test_auth(username, "")
            else:
                ok, msg = False, f"浏览器登录未完成：{msg2}"
        if not ok:
            ui(lambda: self._set_status(f"❌ {msg}", "#c00"))
            ui(lambda: self.btn_start.config(state="normal"))
            return
        if self.remember.get():
            qm.save_config({"username": username,
                            "pat_enc": qm.encrypt_text(pat) if pat else "",
                            "remember": True,
                            "agent_mode": self.agent_mode.get(),
                            "permission_mode": self.perm_var.get(),
                            "max_turns": self.max_turns_var.get(),
                            "cwd": self.cwd_var.get()})
        api_key = qm.derive_api_key(username)
        try:
            self.server = ps.start_server(username, api_key, pat, PORT,
                                          agent_mode=self.agent_mode.get(),
                                          permission_mode=self.perm_var.get(),
                                          max_turns=self.max_turns_var.get(),
                                          cwd=self.cwd_var.get())
        except OSError as e:
            ui(lambda: self._set_status(f"❌ 端口 {PORT} 被占用：{e}", "#c00"))
            ui(lambda: self.btn_start.config(state="normal"))
            return
        ui(lambda: self._show_result(username, api_key))

    # ── 安装 qodercli ──────────────────────────
    def _on_install(self):
        self.btn_install.config(state="disabled")
        self._set_status("正在安装 qodercli（PowerShell）…", "#09c")

        def run():
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "irm https://qoder.com/install.ps1 | iex"],
                    capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
                if qm.qodercli_installed():
                    self.after(0, lambda: self._set_status("✅ qodercli 安装完成，可以开始反代了", "#0a0"))
                else:
                    tail = (r.stderr or r.stdout or "未知错误")[-300:]
                    self.after(0, lambda: self._set_status(f"❌ 安装似乎未成功：{tail}", "#c00"))
            except Exception as e:
                self.after(0, lambda: self._set_status(f"❌ 安装失败：{e}", "#c00"))
            self.after(0, lambda: self.btn_install.config(state="normal"))

        threading.Thread(target=run, daemon=True).start()

    # ── 结果页 ─────────────────────────────────
    def _show_result(self, username, api_key):
        for w in self.winfo_children():
            w.destroy()
        f = ttk.Frame(self, padding=24)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="✅ 反代已启动", font=("Arial", 16, "bold")).pack(anchor="w")
        ttk.Label(f, text=f"当前账户：{username}", foreground="#666").pack(anchor="w", pady=(2, 0))
        if self.agent_mode.get():
            agent_state = f"Agent 模式：开（权限={self.perm_var.get()}） ｜ 目录：{self.cwd_var.get()}"
        else:
            agent_state = "Agent 模式：关"
        ttk.Label(f, text=agent_state,
                  foreground=("#0a0" if self.agent_mode.get() else "#888")).pack(anchor="w", pady=(0, 12))
        log_path = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                                "QoderProxy", "proxy.log")
        ttk.Label(f, text=f"版本 {VERSION}",
                  foreground="#888").pack(anchor="w", pady=(0, 12))
        base_url = f"http://127.0.0.1:{PORT}/v1"
        box = ttk.Frame(f, relief="solid", borderwidth=1, padding=10)
        box.pack(fill="x")
        ttk.Label(box, text="Base URL（客户端填这个）").pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(2, 8))
        e1 = ttk.Entry(row, font=("Consolas", 11))
        e1.insert(0, base_url)
        e1.config(state="readonly")
        e1.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="复制", command=lambda: self._copy(base_url)).pack(side="left", padx=6)
        ttk.Label(box, text="API Key（客户端填这个）").pack(anchor="w")
        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(2, 0))
        e2 = ttk.Entry(row2, font=("Consolas", 11))
        e2.insert(0, api_key)
        e2.config(state="readonly")
        e2.pack(side="left", fill="x", expand=True)
        ttk.Button(row2, text="复制", command=lambda: self._copy(api_key)).pack(side="left", padx=6)
        ttk.Label(f, text="可用模型（官方倍数）", font=("Arial", 11, "bold")).pack(anchor="w", pady=(14, 4))
        txt = tk.Text(f, height=12, font=("Consolas", 10))
        for group, items in ps.MODEL_PRICES:
            txt.insert("end", f"【{group}】\n")
            for name, desc, price in items:
                txt.insert("end", f"  {name:<18}{desc:<12}{price}\n")
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True)
        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="复制全部连接信息",
                   command=lambda: self._copy(f"Base URL: {base_url}\nAPI Key: {api_key}")).pack(side="left")
        ttk.Button(btns, text="停止并退出", command=self._on_close).pack(side="right")

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)

    # ── 关闭 ───────────────────────────────────
    def _on_close(self):
        if self.server:
            try:
                self.server.shutdown()
            except Exception:
                pass
        self.destroy()


def run():
    App().mainloop()
