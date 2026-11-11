"""Read-only parent liveness check so a crashed GUI cannot leave its nominal PID running."""
from __future__ import annotations

import os
import sys


class ParentWatch:
    def __init__(self, pid: int | None):
        self.pid = pid
        self.handle = None
        if pid and sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            self.kernel.CloseHandle.restype = wintypes.BOOL
            self.handle = self.kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, read-only
            if not self.handle:
                raise OSError(ctypes.get_last_error(), "Cannot monitor parent process")

    def alive(self) -> bool:
        if not self.pid:
            return True
        if sys.platform == "win32":
            return self.kernel.WaitForSingleObject(self.handle, 0) == 258  # WAIT_TIMEOUT
        try:
            os.kill(self.pid, 0)  # POSIX only: signal zero does not terminate a process
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
