#!/usr/bin/env python3
"""Start a local ttyd web terminal and serve its single-page wrapper."""

from __future__ import annotations

import contextlib
import http.server
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
TTYD_PORT = 7681
TTYD_PATH = "/ttyd"
START_TIMEOUT = 5.0
TTYD_LOG = Path("/tmp/ttyd.log")
TTYD_CLIENT_HTML = ""

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
.tab.dragging { opacity: .45; }
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
.tab .rename {
    min-width: 4em;
    max-width: 14em;
    padding: 1px 4px;
    border: 1px solid var(--accent);
    border-radius: 4px;
    background: #1e1e1e;
    color: var(--text);
    font: inherit;
    outline: none;
}
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

    const START_URL = "/ttyd/";
    const tabsEl = document.getElementById("tabs");
    const panesEl = document.getElementById("panes");
    const addBtn = document.getElementById("newTab");
    let counter = 0;
    let dragTab = null;
    let scrollDir = 0;
    let scrollRaf = 0;

    function tabs() {
        return Array.from(tabsEl.querySelectorAll(".tab"));
    }

    function updateAutoScroll(clientX) {
        if (!dragTab) return;
        const rect = tabsEl.getBoundingClientRect();
        if (clientX < rect.left + 36) scrollDir = -1;
        else if (clientX > rect.right - 36) scrollDir = 1;
        else scrollDir = 0;
    }

    function tickAutoScroll() {
        if (!dragTab) {
            scrollRaf = 0;
            return;
        }
        if (scrollDir) tabsEl.scrollLeft += scrollDir * 10;
        scrollRaf = requestAnimationFrame(tickAutoScroll);
    }

    function endDrag() {
        if (dragTab) dragTab.classList.remove("dragging");
        dragTab = null;
        scrollDir = 0;
        if (scrollRaf) {
            cancelAnimationFrame(scrollRaf);
            scrollRaf = 0;
        }
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

    function moveTab(step) {
        const items = tabs();
        if (items.length < 2) return null;
        const current = items.indexOf(tabsEl.querySelector(".tab.active"));
        if (current === -1) return null;
        const next = items[(current + step + items.length) % items.length];
        activate(next);
        return next;
    }

    // 终端页面通过 /ttyd/ 反向代理到 ttyd，与包装页同源，因此可以直接监听 iframe 内的按键。
    function handleFrameKey(event) {
        if (!event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return;
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        if (tabs().length < 2) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        moveTab(event.key === "ArrowRight" ? 1 : -1);
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

    function applyName(tab, name) {
        tab.dataset.name = name;
        const label = tab.querySelector(".label");
        if (label) label.textContent = name;
        const close = tab.querySelector(".close");
        if (close) {
            close.title = `关闭${name}`;
            close.setAttribute("aria-label", `关闭${name}`);
        }
        const pane = document.getElementById(tab.getAttribute("aria-controls"));
        const frame = pane && pane.querySelector("iframe");
        if (frame) frame.title = name;
    }

    function startRename(tab) {
        if (tab.querySelector(".rename")) return;
        const label = tab.querySelector(".label");
        if (!label) return;

        const input = document.createElement("input");
        input.className = "rename";
        input.type = "text";
        input.value = tab.dataset.name || label.textContent;
        input.spellcheck = false;
        input.setAttribute("aria-label", "终端名称");
        const fitSize = () => {
            input.size = Math.max(4, Math.min(18, input.value.length * 2 || 4));
        };
        fitSize();
        let done = false;

        function finish(commit, refocus) {
            if (done) return;
            done = true;
            const next = commit ? input.value.trim() : "";
            input.remove();
            label.hidden = false;
            if (next) applyName(tab, next);
            if (refocus) tab.focus();
        }

        input.addEventListener("keydown", (event) => {
            event.stopPropagation();
            if (event.key === "Enter") {
                event.preventDefault();
                finish(true, true);
            } else if (event.key === "Escape") {
                event.preventDefault();
                finish(false, true);
            }
        });
        input.addEventListener("click", (event) => event.stopPropagation());
        input.addEventListener("dblclick", (event) => event.stopPropagation());
        input.addEventListener("input", fitSize);
        input.addEventListener("blur", () => finish(true, false));

        label.hidden = true;
        label.after(input);
        input.focus();
        input.select();
    }

    function createTab() {
        counter += 1;
        const name = `终端 ${counter}`;
        const id = `term-${counter}`;

        const tab = document.createElement("div");
        tab.className = "tab";
        tab.id = `${id}-tab`;
        tab.dataset.name = name;
        tab.setAttribute("role", "tab");
        tab.setAttribute("aria-selected", "false");
        tab.setAttribute("aria-controls", `${id}-panel`);
        tab.tabIndex = -1;

        const label = document.createElement("span");
        label.className = "label";
        label.textContent = name;
        label.title = "双击重命名，拖动调整顺序，Shift+左右方向键切换标签";
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
        frame.addEventListener("load", () => {
            try {
                const view = frame.contentWindow;
                if (view) view.addEventListener("keydown", handleFrameKey, true);
            } catch (error) {
                // 终端页面跨源时无法监听，忽略
            }
        });
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
        tab.addEventListener("dblclick", (event) => {
            if (event.target.closest(".close")) return;
            startRename(tab);
        });
        tab.draggable = true;
        tab.addEventListener("dragstart", (event) => {
            if (event.target.closest(".rename")) {
                event.preventDefault();
                return;
            }
            dragTab = tab;
            // 用自定义类型，避免拖到终端里时被当成文本粘贴
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("application/x-terminal-tab", tab.id);
            tab.classList.add("dragging");
            scrollDir = 0;
            if (!scrollRaf) scrollRaf = requestAnimationFrame(tickAutoScroll);
        });
        tab.addEventListener("dragend", endDrag);
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

    tabsEl.addEventListener("dragover", (event) => {
        if (!dragTab) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        updateAutoScroll(event.clientX);
        const target = event.target.closest(".tab");
        if (!target || target === dragTab) return;
        const rect = target.getBoundingClientRect();
        const after = event.clientX >= rect.left + rect.width / 2;
        if (after) {
            if (target.nextElementSibling !== dragTab) {
                tabsEl.insertBefore(dragTab, target.nextElementSibling);
            }
        } else if (target.previousElementSibling !== dragTab) {
            tabsEl.insertBefore(dragTab, target);
        }
    });

    tabsEl.addEventListener("drop", (event) => {
        if (!dragTab) return;
        event.preventDefault();
        endDrag();
    });

    tabsEl.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        const step = event.key === "ArrowRight" ? 1 : -1;
        const next = moveTab(step);
        if (!next) return;
        event.preventDefault();
        next.focus();
    });

    createTab();
})();
</script>
</body>
</html>
"""


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


def load_ttyd_client() -> str:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{TTYD_PORT}/", timeout=START_TIMEOUT) as response:
            return response.read().decode("utf-8")
    except OSError as error:
        sys.exit(f"无法读取 ttyd 页面，请检查 {TTYD_LOG}：{error}")


class PageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path in ("/", "/hterminal.html"):
            self.send_html(PAGE_HTML)
        elif path == TTYD_PATH or path.startswith(f"{TTYD_PATH}/"):
            self.serve_ttyd(path, query)
        else:
            self.send_error(404)

    def serve_ttyd(self, path: str, query: str) -> None:
        if self.headers.get("Upgrade", "").lower() == "websocket":
            target = path[len(TTYD_PATH):] or "/"
            self.tunnel(f"{target}?{query}" if query else target)
        elif path == f"{TTYD_PATH}/token":
            self.relay_token()
        else:
            # 由本服务提供 ttyd 页面，使 iframe 与包装页同源，才能监听终端内的按键
            self.send_html(TTYD_CLIENT_HTML)

    def send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def relay_token(self) -> None:
        try:
            with urllib.request.urlopen(f"http://{HOST}:{TTYD_PORT}/token", timeout=START_TIMEOUT) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type", "application/json;charset=utf-8")
        except OSError:
            body = b'{"token": ""}'
            content_type = "application/json;charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def tunnel(self, target: str) -> None:
        self.close_connection = True
        try:
            upstream = socket.create_connection((HOST, TTYD_PORT))
        except OSError:
            self.send_error(502)
            return
        try:
            lines = [f"GET {target} HTTP/1.1"]
            for key, value in self.headers.items():
                if key.lower() == "host":
                    value = f"{HOST}:{TTYD_PORT}"
                lines.append(f"{key}: {value}")
            upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            peers = {self.connection: upstream, upstream: self.connection}
            while True:
                readable, _, _ = select.select(list(peers), [], [], 30)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    peers[source].sendall(data)
        except OSError:
            pass
        finally:
            upstream.close()

    def log_message(self, format: str, *args: object) -> None:
        pass


def start_page_server() -> tuple[http.server.ThreadingHTTPServer, str]:
    global TTYD_CLIENT_HTML
    TTYD_CLIENT_HTML = load_ttyd_client()
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
