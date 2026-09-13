"""Stdio gateway entrypoint. Private server control is mandatory; no root arguments."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import BinaryIO

DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_HEADER_BYTES = 8192
DEFAULT_CONTROL_POLL_SECONDS = 0.2
DEFAULT_MAX_INFLIGHT_MESSAGES = 128


def read_message(stream: BinaryIO) -> dict | None:
    length, header_size = None, 0
    while True:
        line = stream.readline(DEFAULT_MAX_HEADER_BYTES + 1)
        header_size += len(line)
        if header_size > DEFAULT_MAX_HEADER_BYTES:
            raise ValueError('lsp-header-limit')
        if not line:
            if length is None:
                return None
            raise EOFError('lsp-header-truncated')
        if line in (b'\r\n', b'\n'):
            break
        name, _, value = line.partition(b':')
        if name.lower() == b'content-length':
            if length is not None:
                raise ValueError('lsp-header-invalid')
            length = int(value.strip())
    if length is None or not 0 <= length <= DEFAULT_MAX_MESSAGE_BYTES:
        raise ValueError('lsp-payload-limit')
    payload = bytearray()
    while len(payload) < length:
        chunk = stream.read(length - len(payload))
        if not chunk:
            raise EOFError('lsp-payload-truncated')
        payload.extend(chunk)
    result = json.loads(payload.decode('utf-8'))
    if type(result) is not dict:
        raise ValueError('lsp-message-invalid')
    return result


def write_message(stream: BinaryIO, message: dict) -> None:
    payload = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(payload) > DEFAULT_MAX_MESSAGE_BYTES:
        raise ValueError('lsp-payload-limit')
    stream.write(f'Content-Length: {len(payload)}\r\n\r\n'.encode('ascii') + payload)
    stream.flush()


async def run(command, control):
    from .lsp_gateway import Gateway
    output_lock = asyncio.Lock()
    statuses = {}
    async def emit(message):
        async with output_lock:
            await asyncio.to_thread(write_message, sys.stdout.buffer, message)
    def status(value): statuses[value['binding_id']] = value
    gateway = Gateway(emit, command=command, status=status)
    tasks = set()
    async def poll():
        revision = None
        while not gateway.closed:
            pending = list(statuses.values()); statuses.clear()
            try:
                response = await asyncio.to_thread(control.request, since_revision=revision, statuses=pending)
                if not response.get('unchanged'):
                    await gateway.accept_contexts(response.get('contexts', []))
                revision = response.get('revision')
            except Exception:
                await gateway.accept_contexts([])
                await gateway._error(None, 'control-unavailable', -32001)
                return
            await gateway.cleanup_idle()
            await asyncio.sleep(DEFAULT_CONTROL_POLL_SECONDS)
    polling = asyncio.create_task(poll())
    try:
        while (message := await asyncio.to_thread(read_message, sys.stdin.buffer)) is not None:
            # Notifications preserve document order. Read requests may overlap and cancel.
            if 'id' not in message:
                await gateway.handle(message)
            else:
                if len(tasks) >= DEFAULT_MAX_INFLIGHT_MESSAGES:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                task = asyncio.create_task(gateway.handle(message))
                tasks.add(task); task.add_done_callback(tasks.discard)
            if gateway.closed:
                break
    finally:
        polling.cancel()
        await asyncio.gather(polling, return_exceptions=True)
        await gateway.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        control.close()


def main():
    from .lsp_control import ControlClient
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('server', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.server[1:] if args.server[:1] == ['--'] else args.server
    try:
        control = ControlClient.from_environment()
    except ValueError:
        raise SystemExit('private-control-unavailable') from None
    if not command:
        control.close()
        raise SystemExit('language-server-unavailable')
    try:
        asyncio.run(run(command, control))
    except (OSError, ValueError, EOFError):
        raise SystemExit('gateway-transport-unavailable') from None


if __name__ == '__main__':
    main()
