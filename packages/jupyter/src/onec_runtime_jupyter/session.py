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
    detach_runtime_namespace,
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
    """Own a core runtime for one interactive notebook shell.

    :meth:`start` installs the BSL magic and Python value namespace. Public
    methods not defined here are delegated to :attr:`runtime`, the owned
    ``RuntimeSession``. The wrapper also arranges cleanup at kernel exit.
    """

    def __init__(
        self,
        runtime: RuntimeSession,
        guardian: GuardianHandle | None = None,
        *,
        display: NotebookDisplayConfig | None = None,
    ) -> None:
        self.runtime = runtime
        self._guardian = guardian
        self._display = display
        self._capture_source: tuple[str, Path] | None = None
        self._closed = False
        self._close_lock = RLock()
        self._installed_shell: object | None = None
        self._shutdown_shell: InteractiveShell | None = None

    @classmethod
    def start(
        cls,
        config: RuntimeSessionConfig,
        *,
        shell: InteractiveShell | None = None,
        display: NotebookDisplayConfig | None = None,
    ) -> "InteractiveRuntimeSession":
        """Start a runtime and install its notebook integration.

        ``config`` selects the platform, infobase and source export. ``shell``
        defaults to the current IPython shell. ``display`` chooses notebook
        reply presentation. Returns the owner; call :meth:`close` or use it as
        a context manager. An existing owner in the same shell is closed
        before the replacement starts.
        """
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
        owner = cls(runtime, guardian, display=display)
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
        """Close the owned runtime and release its shutdown registration.

        An incomplete core cleanup remains retryable through another call.
        """
        self._close(shutdown=False)

    def _close(self, *, shutdown: bool) -> None:
        with self._close_lock:
            if self._closed:
                return
            if self._installed_shell is not None:
                detach_runtime_namespace(self._installed_shell, self.runtime)
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
            self._installed_shell = None
            atexit.unregister(self._close_at_shutdown)
            if self._shutdown_shell is not None:
                if getattr(self._shutdown_shell, _OWNED_SESSION_ATTR, None) is self:
                    delattr(self._shutdown_shell, _OWNED_SESSION_ATTR)
                self._shutdown_shell.unobserve(self._shell_exiting, names="exit_now")
                self._shutdown_shell = None

    def _register_shutdown(self, shell: InteractiveShell) -> None:
        # Keep the active owner alive even when its notebook variable is lost.
        # Directly wrapped/injected runtimes stay external.
        self._installed_shell = shell
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
        """Bind symbolic capture lookup to a local project source export."""
        self._recover_confirmed_stop()
        resolved_root = Path(source_root).resolve()
        self.runtime.configure_capture_source(project, resolved_root)
        self._capture_source = (project, resolved_root)

    def _recover_confirmed_stop(self) -> None:
        """Replace an owned runtime only after exact target termination proof.

        The wrapper keeps its identity in user code. Reinstalling the namespace
        revokes proxies from the old runtime before the next notebook action.
        """
        with self._close_lock:
            if self._closed or self._installed_shell is None:
                return
            old_runtime = self.runtime
            if getattr(old_runtime, "confirmed_target_termination", None) is None:
                return

            shell = self._installed_shell
            detach_runtime_namespace(shell, old_runtime)
            old_runtime.close()
            if not getattr(old_runtime, "is_closed", True):
                raise ProtocolError(
                    "Stopped 1C runtime cleanup is incomplete; replacement is deferred"
                )
            if self._guardian is not None:
                self._guardian.stop()
                self._guardian = None

            replacement = RuntimeSession.start(
                old_runtime.config,
                progress=lambda stage: print(stage, flush=True),
            )
            replacement_guardian: GuardianHandle | None = None
            try:
                if self._capture_source is not None:
                    replacement.configure_capture_source(*self._capture_source)
                replacement_guardian = start_guardian(replacement)
                install_runtime(shell, replacement, display=self._display)
            except BaseException:
                detach_runtime_namespace(shell, replacement)
                replacement.close()
                if replacement_guardian is not None:
                    replacement_guardian.stop()
                raise
            self.runtime = replacement
            self._guardian = replacement_guardian

    def __getattr__(self, name: str) -> object:
        self._recover_confirmed_stop()
        return getattr(self.runtime, name)

    def __enter__(self) -> "InteractiveRuntimeSession":
        """Return the owner for a ``with`` block."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Close the session when leaving a ``with`` block."""
        self.close()
