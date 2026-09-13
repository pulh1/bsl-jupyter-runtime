from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from time import monotonic

from onec_runtime.artifacts import ArtifactWriter, utc_now
from onec_runtime.errors import BslExecutionError, ProtocolError
from onec_runtime.processes import FileModeProcesses
from onec_runtime.rdbg.session import RdbgSession, SessionState


INIT_COUNTER = 'Контекст.Вставить("Счетчик", 0); Результат = Контекст.Счетчик;'
INCREMENT = 'Контекст.Счетчик = Контекст.Счетчик + 1; Результат = Контекст.Счетчик;'
RAISE_ERROR = 'ВызватьИсключение "planned-kernel-error";'


def bsl_string_literal(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return " + Символы.ПС + ".join(
        f'"{part.replace(chr(34), chr(34) * 2)}"' for part in normalized.split("\n")
    )


@dataclass(frozen=True)
class CommandRecord:
    command_id: int
    instruction: str
    instruction_hash: str
    target_id: str
    module_type: str
    extension_name: str
    object_id: str
    property_id: str
    line: int
    start_timestamp: str
    stop_timestamp: str
    duration_ms: float
    completed_command_id: int
    result: str
    error: str
    observed_counter: int | None
    memory: dict[str, int | None]


class KernelExperiment:
    def __init__(
        self,
        session: RdbgSession,
        processes: FileModeProcesses,
        artifacts: ArtifactWriter,
        *,
        command_timeout_s: float = 60.0,
    ) -> None:
        self.session = session
        self.processes = processes
        self.artifacts = artifacts
        self.command_timeout_s = command_timeout_s
        self.command_id = 0
        self.python_context: dict[str, object] = {}

    @staticmethod
    def _int(presentation: str) -> int:
        normalized = "".join(
            character
            for character in presentation.strip().strip('"')
            if character.isdecimal() or character in "+-"
        )
        return int(normalized)

    def execute(self, instruction: str) -> CommandRecord:
        if self.session.state is not SessionState.READY:
            raise ProtocolError("Kernel command requires a READY session")
        self.command_id += 1
        command_id = self.command_id
        start_timestamp = utc_now()
        start = monotonic()
        self.session.modify("ТекущаяИнструкция", bsl_string_literal(instruction))
        self.session.modify("ИдентификаторКоманды", str(command_id))
        self.session.continue_()
        stop = self.session.wait_for_service_stop(timeout_s=self.command_timeout_s)
        completed = self._int(self.session.evaluate("ЗавершеннаяКоманда").presentation)
        result = self.session.evaluate("Результат").presentation
        error = self.session.evaluate("Ошибка").presentation.strip('"')
        counter_eval = self.session.evaluate("Контекст.Счетчик")
        observed_counter = (
            None if counter_eval.error_occurred else self._int(counter_eval.presentation)
        )
        if completed != command_id:
            raise ProtocolError(
                f"Completed command {completed} does not match sent command {command_id}"
            )
        location = stop.location
        record = CommandRecord(
            command_id=command_id,
            instruction=instruction,
            instruction_hash=sha256(instruction.encode("utf-8")).hexdigest(),
            target_id=str(stop.target_id.id),
            module_type=location.module_type,
            extension_name=location.extension_name,
            object_id=str(location.object_id),
            property_id=str(location.property_id),
            line=location.line,
            start_timestamp=start_timestamp,
            stop_timestamp=utc_now(),
            duration_ms=(monotonic() - start) * 1000,
            completed_command_id=completed,
            result=result,
            error=error,
            observed_counter=observed_counter,
            memory=self.processes.sample_memory(),
        )
        self.artifacts.append_jsonl("commands.jsonl", record)
        self.artifacts.append_jsonl(
            "1c-trace.jsonl",
            {"timestamp": record.stop_timestamp, **asdict(record)},
        )
        if error:
            raise BslExecutionError(error)
        return record

    def heartbeat(self) -> dict[str, object]:
        result = {
            "timestamp": utc_now(),
            **self.session.heartbeat(),
            "memory": self.processes.sample_memory(),
            "last_command_id": self.command_id,
        }
        self.artifacts.append_jsonl("health.jsonl", result)
        return result
