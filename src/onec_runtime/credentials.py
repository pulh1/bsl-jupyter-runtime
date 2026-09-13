"""Safe diagnostic representations of 1C launch arguments."""

from collections.abc import Sequence


def redact_command(command: Sequence[str]) -> tuple[str, ...]:
    """Hide /P values; consume /N values even when they look like switches."""
    result: list[str] = []
    credential: str | None = None
    for argument in command:
        if credential is not None:
            result.append("<redacted>" if credential == "/p" and argument else argument)
            credential = None
        else:
            result.append(argument)
            if argument.casefold() in ("/n", "/p"):
                credential = argument.casefold()
    return tuple(result)
