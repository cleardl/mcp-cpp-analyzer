"""Windows ownership, private state, and on-demand process launch.

Only stdlib imports: the stdio proxy must not load the analysis framework.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from functools import lru_cache
from html import escape
from pathlib import Path
import os
import subprocess


@lru_cache
def _apis():
    if os.name != "nt":
        raise NotImplementedError("Shared backend launch currently supports Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    security.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
    security.SetFileSecurityW.restype = wintypes.BOOL
    return kernel, security


@lru_cache
def user_sid():
    kernel, security = _apis()
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if ctypes.get_last_error() != 122:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID)).contents.value
        text = wintypes.LPWSTR()
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, wintypes.HLOCAL))
    finally:
        kernel.CloseHandle(token)


def private_directory(path):
    """Restrict runtime state to the current user and SYSTEM, including new files."""
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    kernel, security = _apis()
    descriptor = wintypes.LPVOID()
    sddl = f"D:P(A;OICI;FA;;;{user_sid()})(A;OICI;FA;;;SY)"
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not security.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)
    return path


def read_shared_text(path):
    """Allow an atomic publisher to replace the file while this snapshot is read."""
    import msvcrt
    kernel, _ = _apis()
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with open(descriptor, "r", encoding="utf-8", closefd=True) as stream:
        return stream.read()


@contextmanager
def workspace_lock(name, *, timeout=90.0):
    kernel, _ = _apis()
    kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        result = kernel.WaitForSingleObject(handle, int(timeout * 1000))
        if result == 258:
            raise TimeoutError(f"Timed out acquiring shared-backend launch lock: {name}")
        if result not in (0, 128):
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = True
        yield
    finally:
        if acquired and not kernel.ReleaseMutex(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(handle)


def _created(kernel, handle):
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    values = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime


def process_created():
    kernel, _ = _apis()
    return _created(kernel, kernel.GetCurrentProcess())


def process_alive(pid, created=None):
    kernel, _ = _apis()
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error in (87, 1168):
            return False
        raise ctypes.WinError(error)
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return exit_code.value == 259 and (created is None or _created(kernel, handle) == created)
    finally:
        kernel.CloseHandle(handle)


def task_xml(command, arguments, cwd):
    """An interactive-user task with no timer, startup, or logon trigger."""
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers />
  <Principals><Principal id="Owner"><UserId>{escape(user_sid())}</UserId>
    <LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>
  </Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate><AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled><Hidden>true</Hidden><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Owner"><Exec><Command>{escape(str(command))}</Command>
    <Arguments>{escape(subprocess.list2cmdline([str(value) for value in arguments]))}</Arguments>
    <WorkingDirectory>{escape(str(cwd))}</WorkingDirectory>
  </Exec></Actions>
</Task>
'''


def task_command(*arguments):
    executable = Path(os.environ["SystemRoot"]) / "System32/schtasks.exe"
    result = subprocess.run([str(executable), *arguments], capture_output=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        kernel, _ = _apis()
        kernel.GetOEMCP.restype = wintypes.UINT
        detail = (result.stdout + result.stderr).decode(f"cp{kernel.GetOEMCP()}").strip()
        raise RuntimeError(f"Task Scheduler command failed ({result.returncode}): {detail}")


def launch_task(name, definition):
    if not name.startswith("McpCppAnalyzer-") or "\\" in name:
        raise ValueError("Invalid shared-backend task name")
    task_command("/Create", "/TN", name, "/XML", str(definition), "/F")
    task_command("/Run", "/TN", name)


def delete_task(name):
    if not name.startswith("McpCppAnalyzer-") or "\\" in name:
        raise ValueError("Invalid shared-backend task name")
    task_command("/Delete", "/TN", name, "/F")
