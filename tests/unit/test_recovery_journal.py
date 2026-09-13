from threading import Barrier, Event, Thread

from onec_runtime.recovery_journal import RecoveryJournal


class Sink:
    def __init__(self) -> None:
        self.rows: list[tuple[str, object]] = []

    def append_jsonl(self, name: str, value: object) -> None:
        self.rows.append((name, value))


def test_cell_events_stay_in_memory_until_terminal_flush() -> None:
    sink = Sink()
    journal = RecoveryJournal(sink.append_jsonl)

    started = journal.record(
        "write-journal.jsonl",
        "cell_started",
        runtime_generation=1,
        operation_id=2,
        cell_sequence=3,
        visible_sha256="a" * 64,
        lowered_sha256="b" * 64,
    )
    journal.record(
        "write-journal.jsonl",
        "cell_completed",
        runtime_generation=1,
        operation_id=2,
        cell_sequence=3,
    )

    assert started.sequence == 1
    assert sink.rows == []
    assert journal.pending_count == 2
    journal.flush()
    assert [row[1]["event"] for row in sink.rows] == [  # type: ignore[index]
        "cell_started",
        "cell_completed",
    ]
    assert journal.pending_count == 0


def test_side_effect_boundary_flushes_only_pending_prefix() -> None:
    sink = Sink()
    journal = RecoveryJournal(sink.append_jsonl)
    journal.record("write-journal.jsonl", "root_write_planned", root="Скаляр")
    journal.flush()
    journal.record("write-journal.jsonl", "root_write_sent", root="Скаляр")

    assert len(sink.rows) == 1
    assert sink.rows[0][1]["event"] == "root_write_planned"  # type: ignore[index]
    assert journal.pending_count == 1


def test_journal_never_reads_from_sink() -> None:
    sink = Sink()
    journal = RecoveryJournal(sink.append_jsonl)
    event = journal.record(
        "recovery-transitions.jsonl",
        "recovering",
        phase="captured",
    )

    assert journal.events == (event,)
    assert not hasattr(journal, "load")
    assert not hasattr(journal, "read")
def test_flush_retries_only_the_uncommitted_suffix_after_sink_failure() -> None:
    from onec_runtime.recovery_journal import RecoveryJournal

    received: list[int] = []

    def sink(_stream: str, value: object) -> None:
        assert isinstance(value, dict)
        sequence = value["sequence"]
        assert isinstance(sequence, int)
        if sequence == 2 and received.count(2) == 0:
            received.append(sequence)
            raise OSError("second durable write failed")
        received.append(sequence)

    journal = RecoveryJournal(sink)
    journal.record("write", "one")
    journal.record("write", "two")
    with __import__("pytest").raises(OSError):
        journal.flush()
    journal.flush()

    assert received == [1, 2, 2]


def test_concurrent_flush_callers_serialize_the_snapshot_sink_retire_loop() -> None:
    """Two flushers must never persist the same pending event twice."""
    first_sink_entered = Event()
    release_first_sink = Event()
    received: list[int] = []

    def sink(_stream: str, value: object) -> None:
        assert isinstance(value, dict)
        sequence = value["sequence"]
        assert isinstance(sequence, int)
        received.append(sequence)
        if sequence == 1 and received.count(1) == 1:
            first_sink_entered.set()
            assert release_first_sink.wait(1.0)

    journal = RecoveryJournal(sink)
    journal.record("write", "one")
    start = Barrier(3)

    def flush() -> None:
        start.wait()
        journal.flush()

    threads = (Thread(target=flush), Thread(target=flush))
    for thread in threads:
        thread.start()
    start.wait()
    assert first_sink_entered.wait(1.0)
    release_first_sink.set()
    for thread in threads:
        thread.join(1.0)
        assert not thread.is_alive()

    assert received == [1]
    assert journal.pending_count == 0


def test_append_during_flush_is_retained_without_blocking_or_deadlock() -> None:
    """The flush mutex is separate: record may append while a sink is blocked."""
    sink_entered = Event()
    release_sink = Event()
    appended = Event()
    received: list[int] = []

    def sink(_stream: str, value: object) -> None:
        assert isinstance(value, dict)
        sequence = value["sequence"]
        assert isinstance(sequence, int)
        received.append(sequence)
        if sequence == 1:
            sink_entered.set()
            assert release_sink.wait(1.0)

    journal = RecoveryJournal(sink)
    journal.record("write", "one")
    flushing = Thread(target=journal.flush)
    flushing.start()
    assert sink_entered.wait(1.0)

    writer = Thread(
        target=lambda: (journal.record("write", "two"), appended.set())
    )
    writer.start()
    assert appended.wait(1.0), "record must not wait for the flush sink"
    release_sink.set()
    flushing.join(1.0)
    writer.join(1.0)
    assert not flushing.is_alive() and not writer.is_alive()

    journal.flush()
    assert received == [1, 2]
    assert journal.pending_count == 0
