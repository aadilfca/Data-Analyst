"""
System tray controller for the Internal Audit App's local server.
Standalone utility — does not modify or import any app code.
Right-click the tray icon for Start / Stop / Restart / Open in Browser.
"""

import os
import subprocess
import webbrowser
import threading
import time
import requests
from PIL import Image, ImageDraw
import pystray

# --- Settings: adjust if your setup differs ---
PROJECT_DIR = r"D:\Excel Auto\2. Python App"
SERVER_URL = "http://127.0.0.1:5000"
START_COMMAND = os.path.join(PROJECT_DIR, "run.bat")
PORT = "5000"
PROCESS = None  # holds the running server process (the whole tree's root)

# Log file kept OUTSIDE the project folder on purpose — if it lived
# inside PROJECT_DIR, writing to it could itself trigger uvicorn's
# --reload file-watcher and cause the exact kind of PID-shifting
# behaviour we saw in testing.
LOG_PATH = os.path.join(os.environ.get("TEMP", PROJECT_DIR), "tray_debug.log")


def log(message):
    with open(LOG_PATH, "a") as f:
        f.write(f"{time.ctime()}: {message}\n")


def is_server_running():
    try:
        requests.get(SERVER_URL, timeout=1)
        return True
    except requests.exceptions.RequestException:
        return False


def get_pid_on_port(port):
    """Find the PID currently LISTENING on the given port, using plain
    netstat (no PowerShell) — simpler and avoids the window-flash issue."""
    result = subprocess.run(
        ["netstat", "-aon"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    for line in result.stdout.splitlines():
        if f":{port} " in line and "LISTENING" in line:
            parts = line.split()
            return parts[-1]
    return None


def start_server(icon=None, item=None):
    global PROCESS
    if is_server_running():
        log("Start requested but server already running — ignored.")
        return
    # cwd=PROJECT_DIR ensures run.bat is found and runs as if launched
    # from inside the project folder (needed for venv activation).
    PROCESS = subprocess.Popen(START_COMMAND, shell=True, cwd=PROJECT_DIR)
    log(f"Start command issued. Tracked root PID: {PROCESS.pid}")
    time.sleep(2)  # give the server a moment to bind the port
    update_icon()


def stop_server(icon=None, item=None):
    global PROCESS

    killed_something = False

    # Preferred method: kill the ENTIRE process tree we started
    # (cmd.exe -> venv activate -> uvicorn supervisor -> uvicorn worker).
    # /T ensures child processes die too — this is what --reload needs,
    # since killing only the worker lets the supervisor carry on.
    if PROCESS is not None:
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(PROCESS.pid)],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        log(f"Tree-kill on tracked PID {PROCESS.pid}: {result.stdout.strip()} {result.stderr.strip()}")
        killed_something = True

    # Fallback / cleanup: also directly kill whatever is still listening
    # on the port, in case the server was started outside the tray, or
    # a stray process survived the step above.
    pid_on_port = get_pid_on_port(PORT)
    if pid_on_port:
        result = subprocess.run(
            ["taskkill", "/F", "/PID", pid_on_port],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        log(f"Port-kill on PID {pid_on_port}: {result.stdout.strip()} {result.stderr.strip()}")
        killed_something = True

    if not killed_something:
        log("Stop requested but no tracked process and nothing found on the port.")

    PROCESS = None
    time.sleep(1)
    update_icon()


def restart_server(icon=None, item=None):
    log("Restart requested.")
    stop_server()
    time.sleep(1)
    start_server()


def open_browser(icon=None, item=None):
    webbrowser.open(SERVER_URL)


def make_icon_image(running):
    # Simple green (running) or grey (stopped) circle icon
    color = "green" if running else "grey"
    img = Image.new("RGB", (64, 64), "white")
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=color)
    return img


def update_icon():
    running = is_server_running()
    tray_icon.icon = make_icon_image(running)
    tray_icon.title = "Server: Running" if running else "Server: Stopped"


def quit_app(icon, item):
    icon.stop()


menu = pystray.Menu(
    pystray.MenuItem("Start Server", start_server),
    pystray.MenuItem("Stop Server", stop_server),
    pystray.MenuItem("Restart Server", restart_server),
    pystray.MenuItem("Open in Browser", open_browser),
    pystray.MenuItem("Quit", quit_app),
)

tray_icon = pystray.Icon("audit_app_server", make_icon_image(False), "Server: Stopped", menu)


# Background thread to keep the icon colour accurate even if the
# server is started/stopped some other way (e.g. manually).
def watcher():
    while True:
        update_icon()
        time.sleep(5)


threading.Thread(target=watcher, daemon=True).start()

tray_icon.run()