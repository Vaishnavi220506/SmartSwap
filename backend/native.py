"""Thin, verified wrappers over the Win32/NT controls SmartSwap uses.

Every setter re-reads the kernel state after writing it and returns whether the
read-back matches, so the action ledger records verified effects rather than API
return codes. All controls are scoped to processes SmartSwap itself started.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

JobObjectCpuRateControlInformation = 15
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
ProcessIoPriority = 33
ProcessMemoryPriority = 0

IDLE_PRIORITY_CLASS = 0x40
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
NORMAL_PRIORITY_CLASS = 0x20
ABOVE_NORMAL_PRIORITY_CLASS = 0x8000

IO_PRIORITY_VERY_LOW = 0
IO_PRIORITY_NORMAL = 2


class _CpuRate(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]


def _kernel32() -> ctypes.WinDLL:
    k = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]; k.CreateJobObjectW.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]; k.OpenProcess.restype = wintypes.HANDLE
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]; k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]; k.IsProcessInJob.restype = wintypes.BOOL
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]; k.SetInformationJobObject.restype = wintypes.BOOL
    k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]; k.QueryInformationJobObject.restype = wintypes.BOOL
    k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]; k.SetPriorityClass.restype = wintypes.BOOL
    k.GetPriorityClass.argtypes = [wintypes.HANDLE]; k.GetPriorityClass.restype = wintypes.DWORD
    k.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]; k.SetProcessInformation.restype = wintypes.BOOL
    k.GetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]; k.GetProcessInformation.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]; k.CloseHandle.restype = wintypes.BOOL
    return k


def _ntdll() -> ctypes.WinDLL:
    n = ctypes.WinDLL("ntdll.dll")
    n.NtSetInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG]; n.NtSetInformationProcess.restype = ctypes.c_long
    n.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]; n.NtQueryInformationProcess.restype = ctypes.c_long
    return n


class _Process:
    """Context manager for a process handle with the given access rights."""

    def __init__(self, pid: int, access: int) -> None:
        self.k = _kernel32(); self.handle = self.k.OpenProcess(access, False, pid)

    def __enter__(self) -> int | None:
        return self.handle or None

    def __exit__(self, *_: object) -> None:
        if self.handle: self.k.CloseHandle(self.handle)


class Job:
    """A SmartSwap-owned Job Object. Membership is verified per process."""

    def __init__(self, name: str) -> None:
        self.k = _kernel32() if IS_WINDOWS else None
        self.handle = self.k.CreateJobObjectW(None, name) if self.k else None
        self.members: set[int] = set()
        self.cpu_rate_percent: float | None = None

    @property
    def ok(self) -> bool:
        return bool(self.handle)

    def assign(self, pid: int) -> bool:
        if not self.handle or pid in self.members: return pid in self.members
        with _Process(pid, PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION) as proc:
            if not proc: return False
            in_job = wintypes.BOOL(False)
            assigned = bool(self.k.AssignProcessToJobObject(self.handle, proc)) and bool(self.k.IsProcessInJob(proc, self.handle, ctypes.byref(in_job))) and bool(in_job.value)
        if assigned: self.members.add(pid)
        return assigned

    def query_cpu_rate(self) -> tuple[int, int] | None:
        if not self.handle: return None
        observed = _CpuRate(); length = wintypes.DWORD()
        if not self.k.QueryInformationJobObject(self.handle, JobObjectCpuRateControlInformation, ctypes.byref(observed), ctypes.sizeof(observed), ctypes.byref(length)): return None
        return int(observed.ControlFlags), int(observed.CpuRate)

    def set_cpu_rate(self, percent: float) -> bool:
        """Hard-cap the job to `percent` of total machine CPU cycles; verified by read-back."""
        if not self.handle: return False
        rate = max(1, min(10000, int(round(percent * 100))))
        requested = _CpuRate(JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP, rate)
        if not self.k.SetInformationJobObject(self.handle, JobObjectCpuRateControlInformation, ctypes.byref(requested), ctypes.sizeof(requested)): return False
        observed = self.query_cpu_rate()
        verified = observed is not None and observed[1] == rate and bool(observed[0] & JOB_OBJECT_CPU_RATE_CONTROL_ENABLE)
        if verified: self.cpu_rate_percent = rate / 100
        return verified

    def clear_cpu_rate(self) -> bool:
        """Remove the cap. Closing the handle alone does NOT lift a cap on live members."""
        if not self.handle: return False
        requested = _CpuRate(0, 0)
        if not self.k.SetInformationJobObject(self.handle, JobObjectCpuRateControlInformation, ctypes.byref(requested), ctypes.sizeof(requested)): return False
        observed = self.query_cpu_rate()
        verified = observed is not None and not (observed[0] & JOB_OBJECT_CPU_RATE_CONTROL_ENABLE)
        if verified: self.cpu_rate_percent = None
        return verified

    def close(self) -> None:
        if self.handle: self.k.CloseHandle(self.handle); self.handle = None


class StopEvent:
    """Manual-reset named event; background workers poll it to stop together."""

    def __init__(self, name: str) -> None:
        self.name = name; self.k = _kernel32()
        self.k.CreateEventW.restype = wintypes.HANDLE; self.k.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.k.SetEvent.argtypes = [wintypes.HANDLE]; self.k.SetEvent.restype = wintypes.BOOL
        self.handle = self.k.CreateEventW(None, True, False, name)
        if not self.handle: raise OSError(ctypes.get_last_error(), "CreateEventW failed")

    def set(self) -> None:
        self.k.SetEvent(self.handle)

    def close(self) -> None:
        if self.handle: self.k.CloseHandle(self.handle); self.handle = None


def get_priority_class(pid: int) -> int | None:
    if not IS_WINDOWS: return None
    with _Process(pid, PROCESS_QUERY_LIMITED_INFORMATION) as proc:
        return int(_kernel32().GetPriorityClass(proc)) or None if proc else None


def set_priority_class(pid: int, value: int) -> bool:
    if not IS_WINDOWS: return False
    k = _kernel32()
    with _Process(pid, PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION) as proc:
        return bool(proc) and bool(k.SetPriorityClass(proc, value)) and k.GetPriorityClass(proc) == value


def get_io_priority(pid: int) -> int | None:
    if not IS_WINDOWS: return None
    value = wintypes.ULONG(); length = wintypes.ULONG()
    with _Process(pid, PROCESS_QUERY_INFORMATION) as proc:
        if not proc: return None
        status = _ntdll().NtQueryInformationProcess(proc, ProcessIoPriority, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(length))
    return int(value.value) if status == 0 else None


def set_io_priority(pid: int, value: int) -> bool:
    """Lower (never raise) a process's I/O priority hint; verified by read-back."""
    if not IS_WINDOWS: return False
    raw = wintypes.ULONG(value)
    with _Process(pid, PROCESS_SET_INFORMATION) as proc:
        if not proc: return False
        status = _ntdll().NtSetInformationProcess(proc, ProcessIoPriority, ctypes.byref(raw), ctypes.sizeof(raw))
    return status == 0 and get_io_priority(pid) == value


def set_memory_priority(pid: int, value: int) -> bool:
    if not IS_WINDOWS: return False
    k = _kernel32(); raw = wintypes.ULONG(value); observed = wintypes.ULONG()
    with _Process(pid, PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION) as proc:
        if not proc or not k.SetProcessInformation(proc, ProcessMemoryPriority, ctypes.byref(raw), ctypes.sizeof(raw)): return False
        return bool(k.GetProcessInformation(proc, ProcessMemoryPriority, ctypes.byref(observed), ctypes.sizeof(observed))) and observed.value == value


def core_classes() -> dict[int, dict[str, int]]:
    """Logical processor -> {efficiency, llc} from GetSystemCpuSetInformation.

    Higher EfficiencyClass means a faster core (P-core); class 0 is the most
    efficient (E-core). On homogeneous CPUs every core reports the same class.
    """
    if not IS_WINDOWS: return {}
    k = _kernel32()
    k.GetSystemCpuSetInformation.argtypes = [ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG), wintypes.HANDLE, wintypes.ULONG]
    k.GetSystemCpuSetInformation.restype = wintypes.BOOL
    size = wintypes.ULONG(0)
    k.GetSystemCpuSetInformation(None, 0, ctypes.byref(size), None, 0)
    buf = (ctypes.c_ubyte * size.value)()
    if not k.GetSystemCpuSetInformation(buf, size, ctypes.byref(size), None, 0): return {}
    raw = bytes(buf); out: dict[int, dict[str, int]] = {}; offset = 0
    while offset < size.value:
        entry = int.from_bytes(raw[offset:offset + 4], "little")
        # SYSTEM_CPU_SET_INFORMATION.CpuSet: Id@8, Group@12, LogicalProcessorIndex@14, CoreIndex@15, LastLevelCacheIndex@16, NumaNodeIndex@17, EfficiencyClass@18
        out[raw[offset + 14]] = {"efficiency": raw[offset + 18], "llc": raw[offset + 16], "core": raw[offset + 15]}
        offset += entry
    return out


def efficient_core_mask() -> int:
    """Affinity mask of the lowest-efficiency-class (E-)cores; 0 on homogeneous CPUs."""
    classes = core_classes()
    if not classes or len({c["efficiency"] for c in classes.values()}) < 2: return 0
    low = min(c["efficiency"] for c in classes.values())
    return sum(1 << lp for lp, c in classes.items() if c["efficiency"] == low)


def get_affinity(pid: int) -> tuple[int, int] | None:
    if not IS_WINDOWS: return None
    k = _kernel32()
    k.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]; k.GetProcessAffinityMask.restype = wintypes.BOOL
    proc_mask = ctypes.c_size_t(); sys_mask = ctypes.c_size_t()
    with _Process(pid, PROCESS_QUERY_LIMITED_INFORMATION) as proc:
        if not proc or not k.GetProcessAffinityMask(proc, ctypes.byref(proc_mask), ctypes.byref(sys_mask)): return None
    return int(proc_mask.value), int(sys_mask.value)


def set_affinity(pid: int, mask: int) -> bool:
    """Hard-confine a process to `mask` (its own workers only); verified by read-back."""
    if not IS_WINDOWS or not mask: return False
    k = _kernel32()
    k.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]; k.SetProcessAffinityMask.restype = wintypes.BOOL
    with _Process(pid, PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION) as proc:
        if not proc or not k.SetProcessAffinityMask(proc, mask): return False
    observed = get_affinity(pid)
    return observed is not None and observed[0] == mask
