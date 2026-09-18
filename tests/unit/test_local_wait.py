"""Local waiter expiry must not be confused with a settled operation error."""

from contextlib import nullcontext

import pytest

from onec_runtime.execution.arbiter import RdbgArbiter, RouteToken
from onec_runtime.execution.local_wait import wait_initiator_locally

from test_main_idle_materialization import Session


def test_settled_timeout_error_does_not_detach_initiating_waiter() -> None:
    route = RouteToken("runtime", 1, 0, "main")
    arbiter = RdbgArbiter(Session([]), route)

    def failed_plan(_port):
        raise TimeoutError("remote plan failed")

    ticket = arbiter.submit(route, failed_plan)
    arbiter.dispatch(ticket)
    try:
        with pytest.raises(TimeoutError, match="remote plan failed"):
            wait_initiator_locally(
                ticket, timeout_s=1, wait_handoff=nullcontext,
                request_stop=lambda: arbiter.request_stop(ticket),
            )
        assert ticket.status().settled is True
        assert ticket.status().waiter_detached is False
    finally:
        arbiter.close(timeout=3)
