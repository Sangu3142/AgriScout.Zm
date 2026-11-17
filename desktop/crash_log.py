"""Crash log for the GUI. Needed because the shortcut starts it with pythonw.exe: no console,
so an error would otherwise vanish without a trace.

install() does three things:
  - opens %LOCALAPPDATA%\\AgriScout\\gui.log (starts a new file above 1 MB, keeps one old copy)
  - sends print()/stderr there (pythonw has no stdout/stderr at all)
  - sys.excepthook + threading.excepthook: any uncaught error -> traceback in the log + error box
The error box uses the Windows API directly (ctypes), so it also works when Qt itself failed.
No faulthandler: on Windows it logs every internal COM exception as "fatal" (tested: code
0x8001010d on every start while Qt opens the window, which Windows handles itself), so the log
would cry wolf. For a hard crash in C code, use Start AgriScout.bat and read the console.
Only standard-library imports here: gui.py imports this before cv2/PySide6.
"""

import ctypes
import os
import sys
import threading
import time
import traceback

LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "AgriScout")
LOG_PATH = os.path.join(LOG_DIR, "gui.log")
MAX_BYTES = 1_000_000
BOX_GAP_S = 5.0        # at most one error box per 5 s (a failing timer would spam boxes)

_log = None
_last_box = 0.0
_show_box = True


def _open_log(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path) > MAX_BYTES:
        os.replace(path, path + ".1")  # keep one old log, never grow forever
    return open(path, "a", encoding="utf-8", buffering=1)  # line-buffered: written at once


def _box(message: str) -> None:
    global _last_box
    if not _show_box or time.monotonic() - _last_box < BOX_GAP_S:
        return
    _last_box = time.monotonic()
    MB_ICONERROR = 0x10
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "AgriScout error", MB_ICONERROR)
    except (AttributeError, OSError):
        pass  # not on Windows: the log is enough


def report(exc_type, exc_value, exc_tb, where: str = "main thread") -> None:
    """Write the traceback to the log and tell the user where it is."""
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    if _log:
        _log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} error in {where} ===\n{text}")
    _box(f"AgriScout hit an error:\n\n{exc_type.__name__}: {exc_value}\n\n"
         f"Details are saved in:\n{LOG_PATH}\n\nStart AgriScout.bat shows them in a console.")


def _excepthook(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    report(exc_type, exc_value, exc_tb)


def _thread_excepthook(args):
    name = args.thread.name if args.thread else "a thread"
    report(args.exc_type, args.exc_value, args.exc_traceback, where=name)


def install(path: str = LOG_PATH, show_box: bool = True) -> None:
    global _log, _show_box, LOG_PATH
    LOG_PATH = path
    _show_box = show_box
    _log = _open_log(path)
    _log.write(f"\n--- AgriScout GUI started {time.strftime('%Y-%m-%d %H:%M:%S')} "
               f"({os.path.basename(sys.executable)}) ---\n")
    if sys.stdout is None:   # pythonw: print() output goes to the log instead of nowhere
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
