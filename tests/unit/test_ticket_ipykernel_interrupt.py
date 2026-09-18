"""A kernel interrupt must reach an initiating ticket wait on Windows."""

import asyncio
import inspect
import json
import time
from pathlib import Path
from threading import Thread

import nbformat
import pytest
from nbclient import NotebookClient


@pytest.mark.parametrize("wait_method", ["wait_initiator", "wait_unknown"])
def test_ipykernel_interrupt_reaches_unbounded_ticket_wait(
    tmp_path: Path, wait_method: str,
) -> None:
    marker = tmp_path / "ticket-active"
    notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("")])
    client = NotebookClient(
        notebook, timeout=8, allow_errors=True, kernel_name="python3",
        resources={"metadata": {"path": str(Path.cwd())}},
    )
    source = (
        "from pathlib import Path\n"
        "from threading import Event\n"
        "from types import SimpleNamespace\n"
        "from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken, Settlement\n"
        "route = RouteToken('kernel-interrupt', 1, 0, 'main')\n"
        "arbiter = RdbgArbiter(SimpleNamespace(target=None), route)\n"
        "entered, release = Event(), Event()\n"
        "def plan(_port):\n"
        "    entered.set()\n"
        "    assert release.wait(20)\n"
        "    return Settlement(None)\n"
        "ticket = arbiter.submit(route, plan)\n"
        "arbiter.dispatch(ticket)\n"
        "assert entered.wait(3)\n"
        f"Path({json.dumps(str(marker))}).write_text('active')\n"
        "try:\n"
        f"    ticket.{wait_method}()\n"
        "finally:\n"
        "    release.set()\n"
        "    ticket.wait_settled(3)\n"
        "    arbiter.close(timeout=3)\n"
    )

    with client.setup_kernel():
        def interrupt_active_ticket() -> None:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert marker.exists(), "the kernel never entered its ticket wait"
            time.sleep(0.2)
            result = client.km.interrupt_kernel()
            if inspect.isawaitable(result):
                asyncio.run(result)

        interrupt = Thread(target=interrupt_active_ticket)
        interrupt.start()
        cell = nbformat.v4.new_code_cell(source)
        try:
            client.execute_cell(cell, 0)
        finally:
            interrupt.join(timeout=5)
        assert any(
            output.output_type == "error" and output.ename == "KeyboardInterrupt"
            for output in cell.outputs
        )
