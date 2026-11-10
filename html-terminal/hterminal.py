#!/usr/bin/env python3
"""Start a local ttyd web terminal and serve its single-page wrapper."""

from __future__ import annotations

import contextlib
import http.server
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
TTYD_PORT = 7681
START_TIMEOUT = 5.0
TTYD_LOG = Path("/tmp/ttyd.log")

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%237aa2f7'/%3E%3Cpath d='M9 11l5 5-5 5' fill='none' stroke='%231e1e1e' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/%3E%3Cpath d='M17 21h7' stroke='%231e1e1e' stroke-width='3' stroke-linecap='round'/%3E%3C/svg%3E">
<title>Web Terminal</title>
<style>
* { box-sizing: border-box; }
:root {
    --bar: #181818;
    --tab: #202020;
    --tab-active: #2b2b2b;
    --line: #303030;
    --text: #d8d8d8;
    --muted: #8a8a8a;
    --accent: #7aa2f7;
}
html, body {
    width: 100%;
    height: 100%;
    margin: 0;
    background: #1e1e1e;
}
body {
    display: flex;
    flex-direction: column;
    overflow: hidden;
    font: 13px/1.4 "Segoe UI", "Noto Sans CJK SC", "PingFang SC", sans-serif;
    color: var(--text);
}
.tabbar {
    display: flex;
    flex: 0 0 36px;
    align-items: stretch;
    background: var(--bar);
    border-bottom: 1px solid var(--line);
}
.tabs {
    display: flex;
    flex: 1 1 auto;
    min-width: 0;
    overflow-x: auto;
    scrollbar-width: none;
}
.tabs::-webkit-scrollbar { display: none; }
.tab {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 0 8px 0 14px;
    border-right: 1px solid var(--line);
    color: var(--muted);
    white-space: nowrap;
    cursor: pointer;
    user-select: none;
    outline: none;
}
.tab:hover { background: var(--tab); }
.tab.active {
    background: var(--tab-active);
    color: var(--text);
    box-shadow: inset 0 2px 0 var(--accent);
}
.tab:focus-visible { box-shadow: inset 0 0 0 1px var(--accent); }
.tab .close {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    padding: 0;
    border: 0;
    border-radius: 4px;
    background: transparent;
    color: var(--muted);
    font: inherit;
    font-size: 15px;
    line-height: 1;
    cursor: pointer;
}
.tab .close:hover { background: #3a3a3a; color: var(--text); }
.tabs.single .tab .close { visibility: hidden; }
.add {
    flex: 0 0 40px;
    border: 0;
    border-left: 1px solid var(--line);
    background: transparent;
    color: var(--muted);
    font: inherit;
    font-size: 19px;
    line-height: 1;
    cursor: pointer;
}
.add:hover { background: var(--tab); color: var(--text); }
.panes { position: relative; flex: 1 1 auto; min-height: 0; }
.pane {
    position: absolute;
    inset: 0;
    visibility: hidden;
}
.pane.active { visibility: visible; }
iframe {
    display: block;
    width: 100%;
    height: 100%;
    border: 0;
    background: #1e1e1e;
    /* ttyd 默认配色偏灰，提饱和让 ANSI 颜色更醒目 */
    filter: saturate(1.6);
}
</style>
</head>
<body>
<div class="tabbar">
    <div class="tabs" id="tabs" role="tablist" aria-label="终端标签"></div>
    <button class="add" id="newTab" type="button" title="新建终端" aria-label="新建终端">+</button>
</div>
<div class="panes" id="panes"></div>
<script>
(() => {
    "use strict";

    const START_URL = "__TTYD_URL__";
    const tabsEl = document.getElementById("tabs");
    const panesEl = document.getElementById("panes");
    const addBtn = document.getElementById("newTab");
    let counter = 0;

    function tabs() {
        return Array.from(tabsEl.querySelectorAll(".tab"));
    }

    function sync() {
        tabsEl.classList.toggle("single", tabs().length <= 1);
    }

    function activate(tab) {
        tabs().forEach((item) => {
            const active = item === tab;
            item.classList.toggle("active", active);
            item.setAttribute("aria-selected", String(active));
            item.tabIndex = active ? 0 : -1;
        });
        const panelId = tab.getAttribute("aria-controls");
        Array.from(panesEl.children).forEach((pane) => {
            pane.classList.toggle("active", pane.id === panelId);
        });
        const pane = document.getElementById(panelId);
        const frame = pane && pane.querySelector("iframe");
        if (frame) frame.focus();
        tab.scrollIntoView({ block: "nearest", inline: "nearest" });
    }

    function closeTab(tab) {
        if (tabs().length <= 1) return;
        const wasActive = tab.classList.contains("active");
        const panelId = tab.getAttribute("aria-controls");
        const index = tabs().indexOf(tab);
        const pane = document.getElementById(panelId);
        tab.remove();
        if (pane) pane.remove();
        sync();
        if (wasActive) {
            const remaining = tabs();
            activate(remaining[Math.min(index, remaining.length - 1)]);
        }
    }

    function createTab() {
        counter += 1;
        const name = `终端 ${counter}`;
        const id = `term-${counter}`;

        const tab = document.createElement("div");
        tab.className = "tab";
        tab.id = `${id}-tab`;
        tab.setAttribute("role", "tab");
        tab.setAttribute("aria-selected", "false");
        tab.setAttribute("aria-controls", `${id}-panel`);
        tab.tabIndex = -1;

        const label = document.createElement("span");
        label.textContent = name;
        const close = document.createElement("button");
        close.className = "close";
        close.type = "button";
        close.title = `关闭${name}`;
        close.setAttribute("aria-label", `关闭${name}`);
        close.textContent = "×";
        tab.append(label, close);

        const pane = document.createElement("div");
        pane.className = "pane";
        pane.id = `${id}-panel`;
        pane.setAttribute("role", "tabpanel");
        pane.setAttribute("aria-labelledby", tab.id);

        const frame = document.createElement("iframe");
        frame.src = START_URL;
        frame.title = name;
        pane.appendChild(frame);

        tab.addEventListener("click", (event) => {
            if (event.target.closest(".close")) return;
            activate(tab);
        });
        close.addEventListener("click", (event) => {
            event.stopPropagation();
            closeTab(tab);
        });
        tab.addEventListener("auxclick", (event) => {
            if (event.button === 1) {
                event.preventDefault();
                closeTab(tab);
            }
        });
        tab.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                activate(tab);
            }
        });

        tabsEl.appendChild(tab);
        panesEl.appendChild(pane);
        sync();
        activate(tab);
    }

    addBtn.addEventListener("click", createTab);

    tabsEl.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        const items = tabs();
        if (items.length < 2) return;
        const current = items.indexOf(document.activeElement.closest(".tab"));
        if (current === -1) return;
        event.preventDefault();
        const step = event.key === "ArrowRight" ? 1 : -1;
        const next = items[(current + step + items.length) % items.length];
        activate(next);
        next.focus();
    });

    createTab();
})();
</script>
</body>
</html>
""".replace("__TTYD_URL__", f"http://{HOST}:{TTYD_PORT}")


def port_open(port: int, timeout: float = 0.25) -> bool:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((HOST, port)) == 0


def wait_for_port(port: int, timeout: float, process: subprocess.Popen | None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(port):
            return True
        if process is not None and process.poll() is not None:
            return False
        time.sleep(0.1)
    return False


def start_ttyd() -> subprocess.Popen | None:
    """Return the ttyd process, or None when an instance is already running."""
    if port_open(TTYD_PORT):
        print(f"ttyd 已在 {HOST}:{TTYD_PORT} 运行，直接复用。", flush=True)
        return None

    if not shutil.which("ttyd"):
        sys.exit("未找到 ttyd，请先安装（例如：sudo apt install ttyd）。")

    print(f"启动 ttyd：{HOST}:{TTYD_PORT}（日志：{TTYD_LOG}）", flush=True)
    with open(TTYD_LOG, "ab", buffering=0) as log:
        process = subprocess.Popen(
            ["ttyd", "-W", "-i", HOST, "-p", str(TTYD_PORT), "bash"],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    if not wait_for_port(TTYD_PORT, START_TIMEOUT, process):
        stop_process(process)
        sys.exit(f"ttyd 启动失败，请查看 {TTYD_LOG}。")
    return process


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


class PageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in ("/", "/hterminal.html"):
            self.send_error(404)
            return
        body = PAGE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def start_page_server() -> tuple[http.server.ThreadingHTTPServer, str]:
    server = http.server.ThreadingHTTPServer((HOST, 0), PageHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{HOST}:{server.server_address[1]}/"


def main() -> int:
    process = start_ttyd()
    server, url = start_page_server()
    print(f"页面地址：{url}", flush=True)
    print("按 Ctrl+C 停止。", flush=True)

    if not webbrowser.open(url):
        print("未能自动打开浏览器，请手动访问上面的地址。", flush=True)

    try:
        while True:
            time.sleep(1)
            if process is not None and process.poll() is not None:
                print("ttyd 已退出。", file=sys.stderr)
                break
    except KeyboardInterrupt:
        print("\n正在停止…", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        stop_process(process)
    return 0


def raise_keyboard_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, raise_keyboard_interrupt)
    raise SystemExit(main())
