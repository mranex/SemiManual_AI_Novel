"""Kiểm keyboard/focus của shell bằng browser thật (T32 tiêu chí 4, T40).

Script này **không** dùng thư viện ngoài: nó tự viết WebSocket client tối thiểu
(RFC 6455) để nói Chrome DevTools Protocol với Edge/Chrome headless, rồi:

1. Tab qua toàn bộ shell và in thứ tự focus (kiểm không kẹt, có tới nav radio,
   nút toggle Arbiter, drawer và các field/action).
2. Tab đầu tiên rồi **Enter** ⇒ thu gọn sidebar (đo bề rộng sidebar).
3. Tab tới nút `Arbiter · …` rồi **Enter** ⇒ panel Arbiter mở (đếm `.dsh-arbiter-row`).
4. Tab tới nút `Chuyển tới …` của Arbiter rồi **Enter** ⇒ đổi workspace (đọc caption
   `CURRENT WORKSPACE`).
5. Focus nav radio rồi **ArrowDown** ⇒ đổi workspace (đường bàn phím của navbar).

Cách chạy (PowerShell, từ thư mục repo):

    .\\.venv\\Scripts\\python.exe docs\\design\\examples\\check_keyboard_focus.py

Script tự seed project demo trong thư mục tạm (dùng fixture test `t27_support`), tự
mở/đóng app và browser; **không** đụng `projects/` thật và không gọi API trả phí.
Kết quả PASS/FAIL in ra stdout, exit code 1 nếu có mục FAIL. Bản chạy ngày
2026-09-22 được lưu ở `docs/design/visual/t40-keyboard-focus.txt`.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
DEFAULT_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

HARNESS = '''\
"""Harness tạm cho check_keyboard_focus: seed session state rồi chạy shell thật."""

import os

import streamlit as st

from novel_ai.ui import layout

slug = os.environ.get("KBD_SLUG", "")
workspace = os.environ.get("KBD_WORKSPACE", "long_plan")

if slug:
    st.session_state[layout.KEY_OPEN_PROJECT] = slug
# Chỉ set khi chưa có: nếu set mỗi run thì điều hướng bằng bàn phím sẽ bị ghi đè.
if layout.KEY_WORKSPACE not in st.session_state:
    st.session_state[layout.KEY_WORKSPACE] = workspace
    st.session_state[layout.KEY_WORKSPACE_NAV] = layout.label_for_workspace(workspace)
if layout.KEY_ARBITER_OPEN not in st.session_state:
    st.session_state[layout.KEY_ARBITER_OPEN] = False

layout.run()
'''


# ---------------------------------------------------------------------------
# WebSocket client tối thiểu (RFC 6455)
# ---------------------------------------------------------------------------


class WS:
    """WebSocket client đủ cho vài lệnh CDP (text frame, mask phía client)."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        rest = url[len("ws://") :]
        host_port, _, path = rest.partition("/")
        host, _, port = host_port.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket handshake đóng sớm")
            header += chunk
        status = header.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise RuntimeError(f"WebSocket handshake thất bại: {status}")
        self.buf = header.split(b"\r\n\r\n", 1)[1]
        self._id = 0

    def _recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket đóng")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_text(self, text: str) -> None:
        payload = text.encode()
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        length = len(payload)
        if length < 126:
            head = struct.pack("!BB", 0x81, 0x80 | length)
        elif length < 65536:
            head = struct.pack("!BBH", 0x81, 0x80 | 126, length)
        else:
            head = struct.pack("!BBQ", 0x81, 0x80 | 127, length)
        self.sock.sendall(head + mask + masked)

    def recv_text(self) -> str:
        while True:
            b1, b2 = struct.unpack("!BB", self._recv_exact(2))
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            masked = bool(b2 & 0x80)
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:  # ping
                self.sock.sendall(struct.pack("!BB", 0x8A, 0x80) + os.urandom(4))
                continue
            if opcode == 0x8:
                raise RuntimeError("server đóng WebSocket")
            if opcode in (0x1, 0x2):
                return payload.decode(errors="replace")

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._id += 1
        msg_id = self._id
        self.send_text(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        self.sock.settimeout(timeout)
        while True:
            data = json.loads(self.recv_text())
            if data.get("id") == msg_id:
                return data

    def evaluate(self, expression: str, timeout: float = 30.0) -> object:
        out = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            timeout=timeout,
        )
        result = out.get("result", {})
        if "exceptionDetails" in result:
            raise RuntimeError(f"JS lỗi: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    # --- input ----------------------------------------------------------
    def key(self, code: str, vk: int, *, key_value: str | None = None, text: str = "") -> None:
        base = {
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "code": code,
            "key": key_value if key_value is not None else code,
        }
        self.call("Input.dispatchKeyEvent", {**base, "type": "keyDown", "text": text})
        self.call("Input.dispatchKeyEvent", {**base, "type": "keyUp"})

    def tab(self) -> None:
        self.key("Tab", 9)

    def enter(self) -> None:
        self.key("Enter", 13, text="\r")

    def arrow_down(self) -> None:
        self.key("ArrowDown", 40)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ACTIVE_JS = """(() => {
  const el = document.activeElement;
  if (!el) return null;
  return {
    tag: el.tagName,
    type: el.getAttribute('type') || '',
    testid: el.getAttribute('data-testid') || '',
    text: (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 34),
  };
})()"""
SIDEBAR_WIDTH_JS = (
    "Math.round(document.querySelector('section[data-testid=\"stSidebar\"]')"
    ".getBoundingClientRect().width)"
)
ARBITER_ROWS_JS = "document.querySelectorAll('.dsh-arbiter-row').length"
WORKSPACE_JS = (
    "(() => { const els = [...document.querySelectorAll('[data-testid=\"stCaptionContainer\"]')]"
    ".map(e => e.textContent.trim()); const hit = els.find(t => /^[a-z_]+ — /.test(t));"
    " return hit ? hit.split(' — ')[0] : ''; })()"
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_http(url: str, *, tries: int = 60, delay: float = 0.5) -> bool:
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=5) as fh:
                if fh.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - chờ server lên
            pass
        time.sleep(delay)
    return False


def page_ws_url(cdp_port: int, url_contains: str) -> str:
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=5) as fh:
                targets = json.load(fh)
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
            continue
        for target in targets:
            if target.get("type") == "page" and url_contains in str(target.get("url", "")):
                return str(target["webSocketDebuggerUrl"])
        time.sleep(0.5)
    raise RuntimeError("không thấy page target của app")


def seed_demo(projects_root: pathlib.Path) -> str:
    sys.path.insert(0, str(REPO_ROOT / "tests" / "integration"))
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from t27_support import seed_project_with_accepted_short_plan  # noqa: PLC0415

    project = seed_project_with_accepted_short_plan(
        projects_root, title="KBD Demo", chapters=3
    )
    return project.slug


def main() -> int:
    parser = argparse.ArgumentParser(description="Kiểm keyboard/focus shell thật")
    parser.add_argument("--browser", default="", help="đường dẫn Edge/Chrome")
    parser.add_argument("--tabs", type=int, default=45, help="số lần Tab để ghi thứ tự")
    parser.add_argument("--keep", action="store_true", help="giữ thư mục tạm để xem log")
    args = parser.parse_args()

    browser = args.browser
    if not browser:
        browser = DEFAULT_EDGE if pathlib.Path(DEFAULT_EDGE).exists() else DEFAULT_CHROME
    if not pathlib.Path(browser).exists():
        print(f"FAIL: không thấy browser: {browser}")
        return 1

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="kbd_focus_"))
    projects_root = tmp / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    slug = seed_demo(projects_root)
    harness = tmp / "harness.py"
    harness.write_text(HARNESS, encoding="utf-8")

    app_port, cdp_port = free_port(), free_port()
    env = {
        **os.environ,
        "NOVEL_AI_PROJECTS_ROOT": str(projects_root),
        "KBD_SLUG": slug,
        "KBD_WORKSPACE": "long_plan",
    }
    problems: list[str] = []
    app = subprocess.Popen(
        [
            sys.executable, "-m", "streamlit", "run", str(harness),
            "--server.port", str(app_port), "--server.headless", "true",
            "--server.address", "127.0.0.1", "--browser.gatherUsageStats", "false",
        ],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    edge = subprocess.Popen(
        [
            browser, "--headless=new", "--disable-gpu", "--no-first-run",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={tmp / 'browser-profile'}", "--window-size=1440,900",
            f"http://127.0.0.1:{app_port}/",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_http(f"http://127.0.0.1:{app_port}"):
            print("FAIL: app không lên")
            return 1
        ws = WS(page_ws_url(cdp_port, f"127.0.0.1:{app_port}"))
        ws.call("Page.enable")
        if not wait_http(f"http://127.0.0.1:{cdp_port}/json/version"):
            print("FAIL: browser không mở cổng debug")
            return 1
        # chờ Streamlit render xong
        for _ in range(60):
            if ws.evaluate("document.querySelectorAll('input[type=radio]').length > 1"):
                break
            time.sleep(0.5)
        else:
            print("FAIL: app chưa render xong")
            return 1
        time.sleep(1.0)

        # --- 1. thứ tự Tab -------------------------------------------------
        sequence: list[dict] = []
        for _ in range(max(10, args.tabs)):
            ws.tab()
            time.sleep(0.12)
            active = ws.evaluate(ACTIVE_JS)
            if active:
                sequence.append(active)  # type: ignore[arg-type]
        print(f"--- thứ tự Tab ({len(sequence)} bước) ---")
        for index, item in enumerate(sequence, start=1):
            testid = f"[{item['testid']}]" if item["testid"] else ""
            kind = f"{item['tag']}{'/' + item['type'] if item['type'] else ''}"
            print(f"{index:2d}. {kind}{testid}: {item['text']}")
        distinct = {json.dumps(item, sort_keys=True) for item in sequence}
        if len(distinct) < 10:
            problems.append(f"chỉ {len(distinct)} control nhận focus (nghi kẹt focus)")
        if not any(item["type"] == "radio" for item in sequence):
            problems.append("Tab không tới được nav radio")
        if not any("Arbiter" in str(item["text"]) for item in sequence):
            problems.append("Tab không tới được nút toggle Arbiter")
        if not any(
            "stSidebar" in str(item["testid"]) or "headerNoPadding" in str(item["testid"])
            for item in sequence
        ):
            # Control thu gọn sidebar của Streamlit 1.41 là button con của
            # `stSidebarCollapseButton` với testid `stBaseButton-headerNoPadding`.
            problems.append("Tab không tới được control sidebar")

        # --- 2. Tab 1 + Enter ⇒ thu gọn sidebar ---------------------------
        def reload_page() -> None:
            ws.call("Page.navigate", {"url": f"http://127.0.0.1:{app_port}/"})
            for _ in range(60):
                time.sleep(0.5)
                if ws.evaluate("document.querySelectorAll('input[type=radio]').length > 1"):
                    time.sleep(1.0)
                    return
            raise RuntimeError("trang chưa render lại")

        reload_page()
        width_before = ws.evaluate(SIDEBAR_WIDTH_JS)
        ws.tab()
        time.sleep(0.3)
        first_focus = ws.evaluate(ACTIVE_JS) or {}
        print(f"\nTab đầu tiên: {first_focus}")
        if "stSidebar" not in str(first_focus.get("testid", "")) and "headerNoPadding" not in str(
            first_focus.get("testid", "")
        ):
            problems.append("Tab đầu tiên không vào control thu gọn sidebar")
        ws.enter()
        time.sleep(2.0)
        width_after = ws.evaluate(SIDEBAR_WIDTH_JS)
        print(f"sidebar width: {width_before} -> {width_after} (Enter)")
        if not (isinstance(width_before, int) and isinstance(width_after, int)):
            problems.append("không đọc được bề rộng sidebar")
        elif width_after >= width_before:
            problems.append("Enter trên control thu gọn không thu hẹp sidebar")

        # --- 3. Tab tới Arbiter + Enter ⇒ mở panel ------------------------
        reload_page()
        rows_before = ws.evaluate(ARBITER_ROWS_JS)
        reached = None
        for step in range(40):
            active = ws.evaluate(ACTIVE_JS) or {}
            if "Arbiter" in str(active.get("text", "")):
                reached = step
                break
            ws.tab()
            time.sleep(0.12)
        if reached is None:
            problems.append("không Tab tới được nút Arbiter")
        else:
            print(f"\nTab tới nút Arbiter ở bước {reached}; Enter…")
            ws.enter()
            time.sleep(2.0)
            rows_after = ws.evaluate(ARBITER_ROWS_JS)
            print(f"arbiter rows: {rows_before} -> {rows_after} (Enter)")
            if not (isinstance(rows_after, int) and isinstance(rows_before, int)):
                problems.append("không đếm được row Arbiter")
            elif rows_after <= rows_before:
                problems.append("Enter trên toggle Arbiter không mở panel")

        # --- 4. Tab tới 'Chuyển tới …' + Enter ⇒ đổi workspace -------------
        before_ws = ws.evaluate(WORKSPACE_JS)
        moved = False
        for _ in range(80):
            active = ws.evaluate(ACTIVE_JS) or {}
            if str(active.get("text", "")).startswith("Chuyển tới"):
                moved = True
                break
            ws.tab()
            time.sleep(0.12)
        if not moved:
            problems.append("không Tab tới được nút 'Chuyển tới …' của Arbiter")
        else:
            ws.enter()
            time.sleep(2.0)
            after_ws = ws.evaluate(WORKSPACE_JS)
            print(f"workspace: {before_ws} -> {after_ws} (Enter trên nút Chuyển tới)")
            if before_ws == after_ws:
                problems.append("Enter trên nút 'Chuyển tới …' không đổi workspace")

        # --- 5. radio nav + ArrowDown ⇒ đổi workspace ---------------------
        reload_page()
        before_ws = ws.evaluate(WORKSPACE_JS)
        ws.evaluate(
            "[...document.querySelectorAll('input[type=radio]')]"
            ".find(r => r.getAttribute('tabindex') === '0')?.focus()"
        )
        time.sleep(0.4)
        ws.arrow_down()
        time.sleep(2.0)
        after_ws = ws.evaluate(WORKSPACE_JS)
        print(f"workspace (radio + ArrowDown): {before_ws} -> {after_ws}")
        if before_ws == after_ws:
            problems.append("ArrowDown trên nav radio không đổi workspace")
    finally:
        for proc in (edge, app):
            try:
                proc.terminate()
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                proc.kill()
        if not args.keep:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"(giữ thư mục tạm: {tmp})")

    print("\n=== KẾT QUẢ ===")
    if problems:
        for item in problems:
            print("FAIL:", item)
        return 1
    print(
        "PASS: keyboard/focus dùng được — Tab phủ nav/drawer/Arbiter/form, Enter thu gọn "
        "sidebar, Enter mở Arbiter, Enter trên 'Chuyển tới …' đổi workspace, ArrowDown trên "
        "nav radio đổi workspace."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
