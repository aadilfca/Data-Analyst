"""System-tray controller for the Internal Audit App local server."""

import os
import atexit
import ctypes
from ctypes import wintypes
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
HEALTH_URL = f"{SERVER_URL}/healthz"
PYTHON_EXE = os.path.join(PROJECT_DIR, "venv", "Scripts", "python.exe")
PORT = "5000"

START_TIMEOUT = 60
STOP_TIMEOUT = 15
POLL_INTERVAL = 0.5
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TRAY_MUTEX_NAME = "Local\\InternalAuditAppTrayController"
ERROR_ALREADY_EXISTS = 183

TEMP_DIR = os.environ.get("TEMP", PROJECT_DIR)
TRAY_LOG = os.path.join(TEMP_DIR, "tray_debug.log")
SERVER_LOG = os.path.join(TEMP_DIR, "audit_app_server.log")

SERVER_PROCESS = None
SERVER_LOG_HANDLE = None
SERVER_OPERATION_LOCK = threading.Lock()
SERVER_OPERATION_STATUS = None
TRAY_MUTEX_HANDLE = None
KERNEL32 = None
if os.name == "nt":
    KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    KERNEL32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    KERNEL32.CreateMutexW.restype = wintypes.HANDLE
    KERNEL32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    KERNEL32.ReleaseMutex.restype = wintypes.BOOL
    KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    KERNEL32.CloseHandle.restype = wintypes.BOOL


def log(message):
    with open(TRAY_LOG, "a", encoding="utf-8") as handle:
        handle.write(f"{time.ctime()}: {message}\n")


def acquire_tray_mutex():
    """Allow only one tray controller per Windows user session."""
    if os.name != "nt":
        return True

    handle = KERNEL32.CreateMutexW(None, True, TRAY_MUTEX_NAME)
    if not handle:
        log("Tray launch blocked: could not create the single-instance mutex.")
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        KERNEL32.CloseHandle(handle)
        log("Tray launch ignored: another tray controller is already running.")
        return None
    return handle


def release_tray_mutex():
    if os.name == "nt" and TRAY_MUTEX_HANDLE not in (None, True):
        KERNEL32.ReleaseMutex(TRAY_MUTEX_HANDLE)
        KERNEL32.CloseHandle(TRAY_MUTEX_HANDLE)


def refresh_tray():
    try:
        update_icon()
        tray_icon.update_menu()
    except Exception as error:
        log(f"Tray refresh failed: {error}")


def server_is_running():
    try:
        request = urllib.request.Request(HEALTH_URL, method="GET")
        with urllib.request.urlopen(request, timeout=1) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def wait_for_server(running, timeout, process=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        if server_is_running() == running:
            return True
        time.sleep(POLL_INTERVAL)
    if process is not None and process.poll() is not None:
        return False
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


def _start_server():
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

    ready = wait_for_server(True, START_TIMEOUT, SERVER_PROCESS)
    if not ready:
        if SERVER_PROCESS.poll() is not None:
            log(f"Start failed: server process exited with code {SERVER_PROCESS.returncode} before health check.")
        else:
            log(f"Start failed: server was not healthy after {START_TIMEOUT}s.")
        if SERVER_PROCESS.poll() is None:
            kill_tree(SERVER_PROCESS.pid, "Failed-start cleanup")
        SERVER_PROCESS = None
        close_server_log()
    update_icon()
    return ready


def _stop_server():
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


def _restart_server():
    log("Restart requested.")
    if _stop_server():
        return _start_server()
    return False


def run_server_operation(label, operation):
    """Run a lifecycle action away from pystray's UI callback thread."""
    global SERVER_OPERATION_STATUS

    if not SERVER_OPERATION_LOCK.acquire(blocking=False):
        log(f"{label} ignored: another server operation is already in progress.")
        return False

    SERVER_OPERATION_STATUS = label

    def worker():
        global SERVER_OPERATION_STATUS
        try:
            log(f"{label} operation started in background.")
            result = operation()
            if result is False:
                log(f"{label} operation did not complete successfully.")
        except Exception as error:
            log(f"{label} operation failed unexpectedly: {error}")
        finally:
            SERVER_OPERATION_STATUS = None
            SERVER_OPERATION_LOCK.release()
            refresh_tray()
            log(f"{label} operation finished.")

    threading.Thread(target=worker, name="audit-server-operation", daemon=True).start()
    refresh_tray()
    return True


def start_server(icon=None, item=None):
    return run_server_operation("Start", _start_server)


def stop_server(icon=None, item=None):
    return run_server_operation("Stop", _stop_server)


def restart_server(icon=None, item=None):
    return run_server_operation("Restart", _restart_server)


def toggle_server(icon=None, item=None):
    if SERVER_OPERATION_LOCK.locked():
        log("Toggle ignored: another server operation is already in progress.")
        return False
    if server_is_running():
        return stop_server(icon, item)
    return start_server(icon, item)


def toggle_label(item):
    if SERVER_OPERATION_STATUS:
        return {"Start": "Starting...", "Stop": "Stopping...", "Restart": "Restarting..."}.get(
            SERVER_OPERATION_STATUS, "Working..."
        )
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
    if SERVER_OPERATION_LOCK.locked():
        log("Quit ignored: wait for the current server operation to finish.")
        return
    close_server_log()
    icon.stop()


TRAY_MUTEX_HANDLE = acquire_tray_mutex()
if TRAY_MUTEX_HANDLE is None:
    raise SystemExit(0)
atexit.register(release_tray_mutex)


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
            refresh_tray()
        except Exception as error:
            log(f"Tray status refresh failed: {error}")
        time.sleep(5)


threading.Thread(target=watcher, daemon=True).start()
tray_icon.run()
