"""Run one engine trial in its own process and print the result as JSON.

The web service launches trials through this module instead of running them on
a thread: inside the service, the controller would share the interpreter lock
with request handlers (process listing, metrics polling) and could be starved
for seconds under load. This keeps dashboard trials on the same execution path
as the research harness. Setting the named event passed as argv[4] cancels.

    python -m backend.trial_runner <preset> <policy> <calibration-json-or-"-"> [cancel-event-name]
"""
from __future__ import annotations

import ctypes
import json
import sys
import threading
from ctypes import wintypes

from .controller import Calibration
from .engine import run_trial


def watch_event(name: str, cancel: threading.Event) -> None:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenEventW.restype = wintypes.HANDLE; k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = k.OpenEventW(0x00100000, False, name)  # SYNCHRONIZE
    if handle and k.WaitForSingleObject(handle, 0xFFFFFFFF) == 0: cancel.set()


def main() -> None:
    preset, policy, cal_arg = sys.argv[1], sys.argv[2], sys.argv[3]
    calibration = Calibration(**json.loads(cal_arg)) if cal_arg != "-" else None
    cancel = threading.Event()
    if len(sys.argv) > 4: threading.Thread(target=watch_event, args=(sys.argv[4], cancel), daemon=True).start()
    result = run_trial(preset, policy, calibration, cancel=cancel)
    sys.stdout.write(json.dumps(result)); sys.stdout.flush()


if __name__ == "__main__":
    main()
