from uuid import UUID

import pytest

from onec_runtime.errors import (
    LeaseConflict,
    LeaseExpired,
    StaleLeaseEpoch,
    StaleRuntimeGeneration,
)
from onec_runtime.lease import MAX_LEASE_TTL_S, LeaseAuthority


OWNER_A = UUID("11111111-1111-1111-1111-111111111111")
OWNER_B = UUID("22222222-2222-2222-2222-222222222222")


class Clock:
    value = 100.0

    def __call__(self) -> float:
        return self.value


class CoerciveTTL:
    def __float__(self) -> float:
        return 2.0


INVALID_TTLS = (
    True,
    "2.0",
    CoerciveTTL(),
    float("nan"),
    float("inf"),
    float("-inf"),
    0,
    -1.0,
    MAX_LEASE_TTL_S + 0.1,
)


def test_one_owner_renews_but_rival_cannot_take_over() -> None:
    clock = Clock()
    leases = LeaseAuthority(7, clock=clock)
    first = leases.grant(OWNER_A, ttl_s=2.0)
    same = leases.grant(OWNER_A, ttl_s=2.0)
    with pytest.raises(LeaseConflict):
        leases.grant(OWNER_B, ttl_s=2.0)

    clock.value += 0.5
    renewed = leases.renew(first, ttl_s=2.0)

    assert same == first
    assert renewed.lease_epoch == first.lease_epoch
    assert leases.status().owner_id == OWNER_A


def test_expiry_blocks_mutation_and_takeover() -> None:
    clock = Clock()
    leases = LeaseAuthority(7, clock=clock)
    handle = leases.grant(OWNER_A, ttl_s=2.0)
    clock.value = 102.0

    assert leases.expire_if_due() == handle
    with pytest.raises(LeaseExpired):
        leases.validate(handle)
    with pytest.raises(LeaseExpired):
        leases.grant(OWNER_B, ttl_s=2.0)


def test_wrong_epoch_is_rejected() -> None:
    leases = LeaseAuthority(7, clock=Clock())
    handle = leases.grant(OWNER_A, ttl_s=2.0)
    wrong = type(handle)(handle.generation_id, handle.owner_id, handle.lease_epoch + 1)
    with pytest.raises(StaleLeaseEpoch):
        leases.validate(wrong)


def test_status_does_not_extend_the_lease_deadline() -> None:
    clock = Clock()
    leases = LeaseAuthority(7, clock=clock)
    handle = leases.grant(OWNER_A, ttl_s=2.0)
    clock.value = 101.0

    status = leases.status()

    assert status.owner_id == OWNER_A
    assert status.lease_epoch == handle.lease_epoch
    assert status.expires_in_s == 1.0
    assert status.expired is False
    clock.value = 102.0
    assert leases.expire_if_due() == handle
    assert leases.expire_if_due() is None


def test_generation_owner_and_epoch_fence_renewal_in_order() -> None:
    leases = LeaseAuthority(7, clock=Clock())
    handle = leases.grant(OWNER_A, ttl_s=2.0)
    wrong_generation = type(handle)(8, OWNER_B, handle.lease_epoch + 1)
    wrong_owner = type(handle)(7, OWNER_B, handle.lease_epoch + 1)

    with pytest.raises(StaleRuntimeGeneration):
        leases.renew(wrong_generation, ttl_s=2.0)
    with pytest.raises(LeaseConflict):
        leases.renew(wrong_owner, ttl_s=2.0)


@pytest.mark.parametrize("invalid_ttl", INVALID_TTLS)
def test_ttl_rejects_non_exact_non_finite_and_out_of_policy_values(
    invalid_ttl: object,
) -> None:
    leases = LeaseAuthority(7, clock=Clock())

    with pytest.raises(ValueError, match="ttl_s"):
        leases.grant(OWNER_A, ttl_s=invalid_ttl)  # type: ignore[arg-type]

    assert leases.status().owner_id is None
    handle = leases.grant(OWNER_A, ttl_s=95.0)

    with pytest.raises(ValueError, match="ttl_s"):
        leases.renew(handle, ttl_s=invalid_ttl)  # type: ignore[arg-type]

    assert leases.status().expires_in_s == 95.0
