"""Launch-time process-tree ownership, independent of whether its PID remains alive."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .lsp_process_windows import _Accounting, _BasicLimits, _ExtendedLimits, _WindowsJob

DEFAULT_SHUTDOWN_SECONDS = 10
DEFAULT_TERMINATION_SECONDS = 5
DEFAULT_STARTUP_SECONDS = 5
POSIX_GUARDIAN_PATH = Path(__file__).with_name('lsp_process_guardian.py')


class OwnedProcess:
    def __init__(self, command, *, env=None, cwd=None):
        self.process, self.job, self.closed = None, None, False
        self._closing = None
        self._abort = threading.Event()
        self._ready = self._ready_fd = self._liveness = None
        child_fds = []
        options = {'env': env, 'cwd': cwd, 'stdin': subprocess.PIPE, 'stdout': subprocess.PIPE, 'stderr': subprocess.DEVNULL}
        try:
            if os.name == 'nt':
                self.job = _WindowsJob()
                options['creationflags'] = 0x00000004  # CREATE_SUSPENDED: no gateway code runs before ownership
            else:
                options['start_new_session'] = True
                read_fd, self._liveness = os.pipe()
                child_fds.append(read_fd)
                self._ready_fd, status_fd = os.pipe()
                child_fds.append(status_fd)
                os.set_blocking(self._ready_fd, False)
                options['pass_fds'] = tuple(child_fds)
                command = [sys.executable, '-I', str(POSIX_GUARDIAN_PATH), str(read_fd), str(status_fd), *command]
            self.process = subprocess.Popen(command, **options)
            self.group = self.process.pid
            if self.job:
                self.job.assign_and_resume(self.process)
        except Exception:
            if os.name == 'nt':
                # Partial bootstrap may itself fail to clean up. Attempt each
                # owned resource independently, then keep the public error fixed.
                if self.process:
                    try: self.process.kill()
                    except Exception: pass
                    try: self.process.wait(timeout=DEFAULT_TERMINATION_SECONDS)
                    except Exception: pass
                if self.job:
                    try: self.job.close()
                    except Exception: pass
                if self.process:
                    for stream in (self.process.stdin, self.process.stdout):
                        try: stream.close()
                        except Exception: pass
                self.process = None
                raise ValueError('gateway-ownership-unavailable') from None
            try:
                if self.process:
                    if os.name != 'nt':
                        try: os.killpg(self.process.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                    else:
                        self.process.kill()
                    self.process.wait(timeout=DEFAULT_TERMINATION_SECONDS)
            finally:
                if self.job: self.job.close()
                for fd in (self._ready_fd, self._liveness):
                    if fd is not None: os.close(fd)
                self._ready_fd = self._liveness = None
                if self.process:
                    self.process.stdin.close(); self.process.stdout.close()
                self.process = None
            raise ValueError('gateway-ownership-unavailable') from None
        finally:
            for fd in child_fds: os.close(fd)

    async def wait_ready(self):
        """Await bounded exec readiness without blocking the server event loop."""
        if self._closing is not None:
            raise ValueError('gateway-ownership-unavailable')
        if self._ready is None:
            self._ready = asyncio.create_task(self._read_startup())
        try:
            await asyncio.shield(self._ready)
        except (Exception, asyncio.CancelledError) as error:
            await self.close(graceful=False)
            if isinstance(error, asyncio.CancelledError): raise
            raise ValueError('gateway-ownership-unavailable') from None

    async def _read_startup(self):
        if self._ready_fd is None: return  # Windows assignment/resume is synchronous.
        fd, received = self._ready_fd, bytearray()
        loop = asyncio.get_running_loop()
        result = loop.create_future()
        def readable():
            if result.done(): return
            try:
                chunk = os.read(fd, 64)
                if chunk:
                    received.extend(chunk)
                    if len(received) > 32 or b'ERROR' in received:
                        raise ValueError('gateway-ownership-unavailable')
                elif received in (b'READY\nGUARDIAN\n', b'GUARDIAN\nREADY\n'):
                    result.set_result(None)
                else:
                    raise ValueError('gateway-ownership-unavailable')
            except BlockingIOError:
                pass
            except (OSError, ValueError) as error:
                result.set_exception(error)
        try:
            loop.add_reader(fd, readable)
            async with asyncio.timeout(DEFAULT_STARTUP_SECONDS):
                await result
        finally:
            try:
                loop.remove_reader(fd)
            finally:
                os.close(fd)
                self._ready_fd = None

    async def close(self, *, timeout=DEFAULT_SHUTDOWN_SECONDS, graceful=True):
        if not graceful:
            self._abort.set()  # Escalate an already-running graceful close, too.
        if self._closing is None:
            async def close():
                if self._ready is not None:
                    if not self._ready.done():
                        self._ready.cancel()
                        await asyncio.gather(self._ready, return_exceptions=True)
                    elif not self._ready.cancelled():
                        self._ready.exception()
                if self._ready_fd is not None:
                    os.close(self._ready_fd)
                    self._ready_fd = None
                await asyncio.to_thread(self._close, timeout, graceful)
            self._closing = asyncio.create_task(close())
        await asyncio.shield(self._closing)

    def _close(self, timeout, graceful):
        if self.closed: return
        self.closed = True
        # Closing a buffered stream may wait for a concurrent read/write lock.
        # Keep that wait independent of the thread enforcing the tree deadline.
        terminated = threading.Event()
        def close_pipes():
            for stream in (self.process.stdin, self.process.stdout):
                if stream is self.process.stdout:
                    terminated.wait()  # Preserve stdout until the grace period ends.
                try: stream.close()
                except OSError: pass  # A killed peer can reject a final flush.
        pipes = threading.Thread(target=close_pipes, name='onec-owned-pipes')
        try:
            if graceful:
                deadline = time.monotonic() + timeout
                pipes.start()
                while self.process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or self._abort.wait(min(.01, remaining)):
                        break
            if self.job:
                deadline = time.monotonic() + DEFAULT_TERMINATION_SECONDS
                failure = None
                try:
                    self.job.terminate(deadline)
                except Exception as error:
                    failure = error
                try:
                    self.process.wait(timeout=max(0, deadline - time.monotonic()))
                except Exception as error:
                    if failure is None: failure = error
                if failure is None and time.monotonic() >= deadline:
                    failure = TimeoutError('gateway-termination-timeout')
                if failure is not None: raise failure
            else:
                try: os.killpg(self.group, signal.SIGKILL)
                except ProcessLookupError: pass
                self.process.wait(timeout=DEFAULT_TERMINATION_SECONDS)
        finally:
            if self.job: self.job.close()
            if self._liveness is not None:
                os.close(self._liveness)
                self._liveness = None
            terminated.set()
            if pipes.ident is None:
                pipes.start()  # Abort the owned peer before waiting on pipe locks.
            pipes.join(DEFAULT_TERMINATION_SECONDS)
            if pipes.is_alive():
                raise TimeoutError('owned-pipe-close-timeout')


# Existing outer WebSocket gateway API; children use the identical ownership primitive.
OwnedGateway = OwnedProcess
