"""System-tray controller for the Internal Audit App local server."""

import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from PIL import Image, ImageDraw
import pystray


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_URL = "http://127.0.0.1:5000"
HEALTH_URL = f"{SERVER_URL}/static/login.html"
PYTHON_EXE = os.path.join(PROJECT_DIR, "venv", "Scripts", "python.exe")
PORT = "5000"

START_TIMEOUT = 20
STOP_TIMEOUT = 15
POLL_INTERVAL = 0.5
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TEMP_DIR = os.environ.get("TEMP", PROJECT_DIR)
TRAY_LOG = os.path.join(TEMP_DIR, "tray_debug.log")
SERVER_LOG = os.path.join(TEMP_DIR, "audit_app_server.log")

SERVER_PROCESS = None
SERVER_LOG_HANDLE = None


def log(message):
    with open(TRAY_LOG, "a", encoding="utf-8") as handle:
        handle.write(f"{time.ctime()}: {message}\n")


def server_is_running():
    try:
        request = urllib.request.Request(HEALTH_URL, method="GET")
        with urllib.request.urlopen(request, timeout=1) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def wait_for_server(running, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_is_running() == running:
            return True
        time.sleep(POLL_INTERVAL)
    return server_is_running() == running


def listening_pids(port):
    """Return live-looking LISTENING PIDs for the exact TCP port."""
    result = subprocess.run(
        ["netstat", "-aon"],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
    )
    pids = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or "LISTENING" not in parts:
            continue
        local_address = parts[1]
        pid = parts[-1]
        if local_address.endswith(f":{port}") and pid.isdigit() and pid not in pids:
            pids.append(pid)
    return pids


def kill_tree(pid, reason):
    result = subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
    )
    detail = " ".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    log(f"{reason} PID {pid}: {detail or 'completed'}")
    return result.returncode == 0


def close_server_log():
    global SERVER_LOG_HANDLE
    if SERVER_LOG_HANDLE is not None:
        SERVER_LOG_HANDLE.close()
        SERVER_LOG_HANDLE = None


def start_server(icon=None, item=None):
    global SERVER_PROCESS, SERVER_LOG_HANDLE

    if server_is_running():
        log("Start ignored: server is already healthy.")
        update_icon()
        return True

    if not os.path.isfile(PYTHON_EXE):
        log(f"Start failed: project Python not found at {PYTHON_EXE}")
        update_icon()
        return False

    # Clear a stale listener before creating a new server process.
    for pid in listening_pids(PORT):
        kill_tree(pid, "Stale-listener cleanup")
    if not wait_for_server(False, STOP_TIMEOUT):
        log("Start aborted: an existing server still responds on port 5000.")
        update_icon()
        return False

    close_server_log()
    SERVER_LOG_HANDLE = open(SERVER_LOG, "a", encoding="utf-8", buffering=1)
    SERVER_LOG_HANDLE.write(f"\n--- Tray server start: {time.ctime()} ---\n")

    command = [
        PYTHON_EXE,
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        "0.0.0.0",
        "--port",
        PORT,
    ]
    SERVER_PROCESS = subprocess.Popen(
        command,
        cwd=PROJECT_DIR,
        stdout=SERVER_LOG_HANDLE,
        stderr=subprocess.STDOUT,
        creationflags=NO_WINDOW,
    )
    log(f"Start requested. Tracked PID {SERVER_PROCESS.pid}; command has no --reload.")

    ready = wait_for_server(True, START_TIMEOUT)
    if not ready:
        log(f"Start failed: server was not healthy after {START_TIMEOUT}s.")
        if SERVER_PROCESS.poll() is None:
            kill_tree(SERVER_PROCESS.pid, "Failed-start cleanup")
        SERVER_PROCESS = None
        close_server_log()
    update_icon()
    return ready


def stop_server(icon=None, item=None):
    global SERVER_PROCESS

    if SERVER_PROCESS is not None and SERVER_PROCESS.poll() is None:
        kill_tree(SERVER_PROCESS.pid, "Tracked-process stop")

    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not server_is_running():
            break
        for pid in listening_pids(PORT):
            kill_tree(pid, "Port-process stop")
        time.sleep(POLL_INTERVAL)

    stopped = wait_for_server(False, 2)
    if stopped:
        SERVER_PROCESS = None
        close_server_log()
        log("Stop completed: health endpoint is offline.")
    else:
        log("Stop failed: health endpoint still responds after timeout.")
    update_icon()
    return stopped


def restart_server(icon=None, item=None):
    log("Restart requested.")
    if stop_server(icon, item):
        return start_server(icon, item)
    return False


def toggle_server(icon=None, item=None):
    if server_is_running():
        return stop_server(icon, item)
    return start_server(icon, item)


def toggle_label(item):
    return "Stop Server" if server_is_running() else "Start Server"


def open_browser(icon=None, item=None):
    webbrowser.open(SERVER_URL)


def make_icon_image(running):
    color = "green" if running else "grey"
    image = Image.new("RGB", (64, 64), "white")
    ImageDraw.Draw(image).ellipse((8, 8, 56, 56), fill=color)
    return image


def update_icon():
    running = server_is_running()
    tray_icon.icon = make_icon_image(running)
    tray_icon.title = "Server: Running" if running else "Server: Stopped"


def quit_app(icon, item):
    close_server_log()
    icon.stop()


menu = pystray.Menu(
    pystray.MenuItem(toggle_label, toggle_server, default=True),
    pystray.MenuItem("Start Server", start_server),
    pystray.MenuItem("Stop Server", stop_server),
    pystray.MenuItem("Restart Server", restart_server),
    pystray.MenuItem("Open in Browser", open_browser),
    pystray.MenuItem("Quit", quit_app),
)

tray_icon = pystray.Icon(
    "audit_app_server",
    make_icon_image(server_is_running()),
    "Server: Running" if server_is_running() else "Server: Stopped",
    menu,
)


def watcher():
    while True:
        try:
            update_icon()
        except Exception as error:
            log(f"Tray status refresh failed: {error}")
        time.sleep(5)


threading.Thread(target=watcher, daemon=True).start()
tray_icon.run()
