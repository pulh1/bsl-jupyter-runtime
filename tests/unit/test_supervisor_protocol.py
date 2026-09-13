import pickle

import pytest

from onec_runtime.errors import InvalidMessageSequence
from onec_runtime.supervisor_protocol import (
    PROTOCOL_VERSION,
    ControlMessage,
    MessageKind,
    MessageReceiver,
    MessageSender,
)


def test_sender_numbers_messages_and_receiver_accepts_exact_sequence() -> None:
    sender = MessageSender(generation_id=4)
    receiver = MessageReceiver(generation_id=4)
    first = sender.create(MessageKind.STATUS, observer_id="reader")
    second = sender.create(MessageKind.SHUTDOWN)

    receiver.accept(first)
    receiver.accept(second)

    assert first.message_sequence == 1
    assert second.message_sequence == 2


def test_duplicate_out_of_order_and_wrong_generation_are_rejected() -> None:
    sender = MessageSender(generation_id=4)
    receiver = MessageReceiver(generation_id=4)
    first = sender.create(MessageKind.STATUS)
    receiver.accept(first)
    with pytest.raises(InvalidMessageSequence):
        receiver.accept(first)

    wrong = MessageSender(generation_id=5).create(MessageKind.STATUS)
    with pytest.raises(InvalidMessageSequence, match="generation"):
        receiver.accept(wrong)


def test_rejected_messages_do_not_advance_receiver_sequence() -> None:
    sender = MessageSender(generation_id=4)
    receiver = MessageReceiver(generation_id=4)
    first = sender.create(MessageKind.STATUS)
    receiver.accept(first)
    second = sender.create(MessageKind.STATUS)
    out_of_order = ControlMessage(
        protocol_version=PROTOCOL_VERSION,
        generation_id=4,
        message_sequence=3,
        kind=MessageKind.STATUS,
        payload={},
    )

    with pytest.raises(InvalidMessageSequence):
        receiver.accept(out_of_order)

    receiver.accept(second)


def test_message_rejects_wrong_protocol_version() -> None:
    receiver = MessageReceiver(generation_id=4)
    wrong_version = ControlMessage(
        protocol_version=PROTOCOL_VERSION + 1,
        generation_id=4,
        message_sequence=1,
        kind=MessageKind.STATUS,
        payload={},
    )

    with pytest.raises(InvalidMessageSequence, match="protocol"):
        receiver.accept(wrong_version)


def test_control_message_round_trips_through_pickle() -> None:
    message = MessageSender(generation_id=9).create(
        MessageKind.GRANT_LEASE,
        owner_id="frontend",
        lease_epoch=2,
    )

    assert pickle.loads(pickle.dumps(message)) == message
