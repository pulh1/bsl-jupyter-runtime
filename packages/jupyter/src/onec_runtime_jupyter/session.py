"""Interactive owner for a headless runtime session."""

from __future__ import annotations

import atexit
import logging
from pathlib import Path
from threading import RLock

from IPython.core.interactiveshell import InteractiveShell

from onec_runtime.errors import ProtocolError
from onec_runtime.session import RuntimeSession, RuntimeSessionConfig
from onec_runtime_jupyter.extension import (
    NotebookDisplayConfig,
    install_runtime,
    load_ipython_extension,
)
from onec_runtime_jupyter.session_guardian import (
    GuardianHandle,
    recover_failed_guards,
    start_guardian,
)


_OWNED_SESSION_ATTR = "_onec_interactive_runtime_owner"


class InteractiveRuntimeSession:
    """Own a runtime through normal shutdown and abrupt Windows kernel exit."""

    def __init__(
        self, runtime: RuntimeSession, guardian: GuardianHandle | None = None,
    ) -> None:
        self.runtime = runtime
        self._guardian = guardian
        self._closed = False
        self._close_lock = RLock()
        self._shutdown_shell: InteractiveShell | None = None

    @classmethod
    def start(
        cls,
        config: RuntimeSessionConfig,
        *,
        shell: InteractiveShell | None = None,
        display: NotebookDisplayConfig | None = None,
    ) -> "InteractiveRuntimeSession":
        target_shell = shell or InteractiveShell.instance()
        previous_owner = getattr(target_shell, _OWNED_SESSION_ATTR, None)
        if isinstance(previous_owner, InteractiveRuntimeSession):
            previous_owner.close()
            if not previous_owner._closed:
                raise ProtocolError(
                    "Previous runtime session cleanup is still in progress"
                )
        for session_id in recover_failed_guards(config):
            print(
                f"Не удалось завершить предыдущий сеанс 1С {session_id}; "
                "проверьте .runtime/kernel-guardians",
                flush=True,
            )
        runtime = RuntimeSession.start(config, progress=lambda stage: print(stage, flush=True))
        try:
            guardian = start_guardian(runtime)
        except BaseException:
            runtime.close()
            raise
        owner = cls(runtime, guardian)
        try:
            install_runtime(target_shell, runtime, display=display)
            load_ipython_extension(target_shell)
            owner._register_shutdown(target_shell)
            setattr(target_shell, _OWNED_SESSION_ATTR, owner)
            return owner
        except BaseException:
            runtime.close()
            if guardian is not None:
                guardian.stop()
            raise

    def close(self) -> None:
        self._close(shutdown=False)

    def _close(self, *, shutdown: bool) -> None:
        with self._close_lock:
            if self._closed:
                return
            # Failed core cleanup remains retryable, including at Python exit.
            shutdown_close = (
                getattr(self.runtime, "close_for_kernel_shutdown", None)
                if shutdown else None
            )
            if callable(shutdown_close):
                shutdown_close()
            else:
                self.runtime.close()
            if (
                isinstance(self.runtime, RuntimeSession)
                and not self.runtime.is_closed
            ):
                return
            if self._guardian is not None:
                self._guardian.stop()
            self._closed = True
            atexit.unregister(self._close_at_shutdown)
            if self._shutdown_shell is not None:
                if getattr(self._shutdown_shell, _OWNED_SESSION_ATTR, None) is self:
                    delattr(self._shutdown_shell, _OWNED_SESSION_ATTR)
                self._shutdown_shell.unobserve(self._shell_exiting, names="exit_now")
                self._shutdown_shell = None

    def _register_shutdown(self, shell: InteractiveShell) -> None:
        # Keep the active owner alive even when its notebook variable is lost.
        # Directly wrapped/injected runtimes stay external.
        atexit.register(self._close_at_shutdown)
        if isinstance(shell, InteractiveShell):
            self._shutdown_shell = shell
            # ipykernel sets this before replying to shutdown/restart requests,
            # while threads and transport are still usable. Client disconnects
            # do not change the trait. atexit also covers ordinary Python exit.
            shell.observe(self._shell_exiting, names="exit_now")

    def _shell_exiting(self, change: dict[str, object]) -> None:
        if change["new"]:
            self._close_at_shutdown()

    def _close_at_shutdown(self) -> None:
        try:
            self._close(shutdown=True)
        except BaseException:
            # Never leak raw platform errors or prevent other owners' cleanup.
            logging.getLogger(__name__).warning("1C runtime cleanup failed during shutdown")

    def configure_capture_source(self, project: str, source_root: Path | str) -> None:
        self.runtime.configure_capture_source(project, Path(source_root).resolve())

    def __getattr__(self, name: str) -> object:
        return getattr(self.runtime, name)

    def __enter__(self) -> "InteractiveRuntimeSession":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
