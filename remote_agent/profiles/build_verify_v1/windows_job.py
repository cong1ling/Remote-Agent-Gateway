from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_JOB_OBJECT_BASIC_ACCOUNTING = 1
_KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)


class _JobAccounting(ctypes.Structure):
    _fields_ = [
        ("times", ctypes.c_longlong * 4),
        ("page_faults", wintypes.DWORD),
        ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD),
        ("terminated_processes", wintypes.DWORD),
    ]


def _bind(name: str, result: type, *arguments: type):
    function = getattr(_KERNEL, name)
    function.argtypes = arguments
    function.restype = result
    return function


_create_job = _bind("CreateJobObjectW", wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCWSTR)
_open_process = _bind("OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_assign_process = _bind("AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE)
_terminate_job = _bind("TerminateJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
_query_job = _bind(
    "QueryInformationJobObject", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int,
    wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
)
_close_handle = _bind("CloseHandle", wintypes.BOOL, wintypes.HANDLE)


class WindowsJob:
    """Keep the gated interpreter and its descendants in a single Windows job."""

    def __enter__(self) -> WindowsJob:
        self._handle = _create_job(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._assigned = False
        return self

    def __exit__(self, *_exc: object) -> None:
        _close_handle(self._handle)

    def assign(self, pid: int) -> None:
        child = _open_process(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, pid)
        if not child:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _assign_process(self._handle, child):
                raise ctypes.WinError(ctypes.get_last_error())
            self._assigned = True
        finally:
            _close_handle(child)

    def terminate(self) -> None:
        if not self._assigned:
            return
        if not _terminate_job(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        accounting = _JobAccounting()
        deadline = time.monotonic() + 2
        while True:
            if not _query_job(
                self._handle, _JOB_OBJECT_BASIC_ACCOUNTING,
                ctypes.byref(accounting), ctypes.sizeof(accounting), None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if accounting.active_processes == 0:
                return
            if time.monotonic() >= deadline:
                raise OSError("build.runner_failed")
            time.sleep(0.01)
