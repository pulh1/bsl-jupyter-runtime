from __future__ import annotations


def _blank(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[-1]
    return ""


def preprocess_server_source(source: str) -> str:
    return "".join(
        _blank(line) if line.lstrip().startswith(("#", "&")) else line
        for line in source.splitlines(keepends=True)
    )
