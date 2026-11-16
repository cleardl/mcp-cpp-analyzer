"""Keep backend descendants in a job owned by the backend, not a client."""
import win32api
import win32job
import time


class ProcessTree:
    def __init__(self):
        self.handle = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation, info)
        win32job.AssignProcessToJobObject(self.handle, win32api.GetCurrentProcess())

    def finish(self, *, clean):
        if clean:
            deadline = time.monotonic() + 2
            while win32job.QueryInformationJobObject(self.handle,
                    win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"] != 1:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Backend descendants are still alive after normal shutdown")
                time.sleep(0.02)
            # The normal lifespan already reaped its children. Avoid terminating
            # our own process while returning a successful idle shutdown.
            info = win32job.QueryInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] &= ~win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation, info)
        self.handle.Close()
