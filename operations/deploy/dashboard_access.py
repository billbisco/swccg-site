"""Silent, loopback-only SSH dashboard access, owned by Windows Task Scheduler."""
import ctypes
from ctypes import wintypes
import subprocess
import sys


class BASIC(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit",ctypes.c_int64),("PerJobUserTimeLimit",ctypes.c_int64),
                ("LimitFlags",wintypes.DWORD),("MinimumWorkingSetSize",ctypes.c_size_t),
                ("MaximumWorkingSetSize",ctypes.c_size_t),("ActiveProcessLimit",wintypes.DWORD),
                ("Affinity",ctypes.c_size_t),("PriorityClass",wintypes.DWORD),("SchedulingClass",wintypes.DWORD)]


class IO(ctypes.Structure):
    _fields_ = [(name,ctypes.c_uint64) for name in ("ReadOperationCount","WriteOperationCount","OtherOperationCount","ReadTransferCount","WriteTransferCount","OtherTransferCount")]


class EXTENDED(ctypes.Structure):
    _fields_ = [("BasicLimitInformation",BASIC),("IoInfo",IO),("ProcessMemoryLimit",ctypes.c_size_t),
                ("JobMemoryLimit",ctypes.c_size_t),("PeakProcessMemoryUsed",ctypes.c_size_t),("PeakJobMemoryUsed",ctypes.c_size_t)]


def main():
    if sys.platform != 'win32': return 1
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]; k.CreateJobObjectW.restype=wintypes.HANDLE
    k.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]; k.SetInformationJobObject.restype=wintypes.BOOL
    k.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]; k.AssignProcessToJobObject.restype=wintypes.BOOL
    k.CloseHandle.argtypes=[wintypes.HANDLE]; k.CloseHandle.restype=wintypes.BOOL
    job=k.CreateJobObjectW(None,None)
    if not job: return 1
    child=None
    try:
        info=EXTENDED(); info.BasicLimitInformation.LimitFlags=0x2000 # kill on last job handle close
        if not k.SetInformationJobObject(job,9,ctypes.byref(info),ctypes.sizeof(info)): return 1
        child=subprocess.Popen(['ssh.exe','-N','-T','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes',
            '-o','ConnectTimeout=10','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=2',
            '-L','127.0.0.1:8765:/run/swccg-operations-reader/dashboard.sock','gemp'],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW|subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        if not k.AssignProcessToJobObject(job,int(child._handle)):
            child.terminate(); child.wait(timeout=10); return 1
        try: return child.wait(timeout=7*3600+55*60)
        except subprocess.TimeoutExpired: return 0
    finally:
        k.CloseHandle(job)
        if child is not None and child.poll() is None:
            child.terminate()


if __name__=='__main__': raise SystemExit(main())
