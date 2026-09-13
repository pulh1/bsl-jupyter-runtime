"""Private per-gateway control channel using authenticated, bounded JSON bytes.

The provider closure is created by the authenticated server handler and scopes
all returned contexts to its owner/connection. Clients cannot select identities.
"""
from __future__ import annotations

import json
import os
import socket
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener, answer_challenge, deliver_challenge
from secrets import token_bytes
from tempfile import TemporaryDirectory
from threading import Event, RLock, Thread, Timer
from uuid import uuid4

_PREFIX = 'ONEC_BSL_CONTROL_'
DEFAULT_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_PENDING_REQUESTS = 1
DEFAULT_AUTH_TIMEOUT_SECONDS = 2
DEFAULT_MAX_STATUS_ACKS = 256


def _poll_request(request):
    return (type(request) is dict and set(request) <= {'version', 'op', 'since_revision', 'statuses'}
            and request.get('version') == 1 and request.get('op') == 'poll'
            and (request.get('since_revision') is None or type(request['since_revision']) is int)
            and type(request.get('statuses', [])) is list
            and len(request.get('statuses', [])) <= DEFAULT_MAX_STATUS_ACKS)


class _CancellableListener:
    """Isolate CPython Listener internals needed for bounded local accept.

    Public Listener.accept() has no cancellation API. Windows PipeListener.close
    does not close the handle removed from its queue by a pending accept. Keep
    that overlapped operation here so shutdown can cancel and reclaim it.
    Authentication remains the standard multiprocessing challenge protocol,
    performed by ControlServer only after it owns the accepted connection.
    """
    def __init__(self, address, family, stop, backlog):
        self._listener = Listener(address, family=family, authkey=None, backlog=backlog)
        self._stop, self._family = stop, family
        try:
            raw = self._listener._listener
            if family == 'AF_PIPE':
                import _winapi
                from multiprocessing.connection import PipeConnection
                if (type(raw._handle_queue) is not list
                        or not raw._handle_queue
                        or any(type(handle) is not int for handle in raw._handle_queue)
                        or not callable(raw._new_handle)
                        or not all(callable(getattr(_winapi, name, None)) for name in
                                   ('ConnectNamedPipe', 'WaitForMultipleObjects', 'CloseHandle'))):
                    raise ValueError
                self._winapi, self._pipe_connection = _winapi, PipeConnection
            elif family == 'AF_UNIX' and isinstance(raw._socket, socket.socket):
                raw._socket.settimeout(0.1)
            else:
                raise ValueError
            self._raw = raw
        except (AttributeError, ImportError, TypeError, ValueError):
            self._listener.close()
            raise ValueError('cancellable-control-transport-unavailable') from None

    def accept(self):
        if self._family == 'AF_PIPE':
            return self._accept_pipe()
        while not self._stop.is_set():
            try:
                return self._listener.accept()
            except socket.timeout:
                continue
        raise EOFError

    def _accept_pipe(self):
        if self._stop.is_set():
            raise EOFError
        api = self._winapi
        self._raw._handle_queue.append(self._raw._new_handle())
        handle = self._raw._handle_queue.pop(0)
        operation = None
        try:
            try:
                operation = api.ConnectNamedPipe(handle, overlapped=True)
            except OSError as error:
                if error.winerror != api.ERROR_NO_DATA:
                    raise
            if operation is not None:
                while api.WaitForMultipleObjects([operation.event], False, 100) == api.WAIT_TIMEOUT:
                    if self._stop.is_set():
                        raise EOFError
                _, error = operation.GetOverlappedResult(True)
                if error:
                    raise OSError('control-accept-unavailable')
            if self._stop.is_set():
                raise EOFError
            return self._pipe_connection(handle)
        except BaseException:
            if operation is not None:
                operation.cancel()
                operation.GetOverlappedResult(True)
            api.CloseHandle(handle)
            raise

    def close(self):
        self._listener.close()


def _bytes(value, limit):
    try:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(payload) > limit:
            raise ValueError
        return payload
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('control-payload-limit') from None


def _json(payload):
    try:
        value = json.loads(payload.decode('utf-8'))
        if type(value) is not dict:
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid-control-message') from None


class ControlServer:
    def __init__(self, provider, *, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
                 max_pending_requests=DEFAULT_MAX_PENDING_REQUESTS, revision=None, consumer=None):
        if max_payload_bytes < 256 or max_pending_requests != 1:
            raise ValueError('invalid-control-limits')
        self.provider, self.max_payload_bytes = provider, max_payload_bytes
        self.revision, self.consumer = revision, consumer
        self._key = token_bytes(32)
        self._temporary = None
        self._closed = Event()
        self._connection = None
        self._connection_lock = RLock()
        if os.name == 'nt':
            self.family = 'AF_PIPE'
            self.address = '\\\\.\\pipe\\onec-bsl-' + uuid4().hex
        else:
            self.family = 'AF_UNIX'
            self._temporary = TemporaryDirectory(prefix='onec-bsl-')
            self.address = self._temporary.name + '/control.sock'
        self._listener = _CancellableListener(self.address, self.family, self._closed, max_pending_requests)
        self._thread = Thread(target=self._serve, daemon=True)
        self._thread.start()

    def child_environment(self):
        # Return a dedicated child mapping. Never modify the server environment.
        return {_PREFIX + 'ADDRESS': self.address, _PREFIX + 'FAMILY': self.family,
                _PREFIX + 'KEY': self._key.hex(), _PREFIX + 'LIMIT': str(self.max_payload_bytes)}

    def _serve(self):
        while not self._closed.is_set():
            try:
                connection = self._listener.accept()
            except (OSError, EOFError):
                return
            except Exception:
                continue
            with self._connection_lock:
                self._connection = connection
            try:
                if self._closed.is_set():
                    continue
                self._authenticate(connection)
                while not self._closed.is_set():
                    if not connection.poll(0.2):
                        continue
                    request = _json(connection.recv_bytes(self.max_payload_bytes))
                    if not _poll_request(request):
                        response = {'mode': 'virtual-only', 'reason': 'invalid-control-request'}
                    else:
                        try:
                            if self.consumer and request.get('statuses'):
                                self.consumer(request['statuses'])
                            revision = self.revision() if self.revision else None
                            if revision is not None and request.get('since_revision') == revision:
                                response = {'revision': revision, 'unchanged': True}
                            else:
                                response = self.provider()
                                if revision is not None:
                                    response = {**response, 'revision': revision}
                            _bytes({'version': 1, 'ack': response}, self.max_payload_bytes)
                        except Exception:
                            response = {'mode': 'virtual-only', 'reason': 'control-payload-limit'}
                    connection.send_bytes(_bytes({'version': 1, 'ack': response}, self.max_payload_bytes))
            except (OSError, EOFError, ValueError, AuthenticationError):
                pass
            finally:
                self._abort_connection(connection)
                with self._connection_lock:
                    self._connection = None

    def _authenticate(self, connection):
        deadline = Timer(DEFAULT_AUTH_TIMEOUT_SECONDS, self._abort_connection, args=(connection,))
        deadline.start()
        try:
            deliver_challenge(connection, self._key)
            answer_challenge(connection, self._key)
        finally:
            deadline.cancel()
            deadline.join()

    def _abort_connection(self, connection):
        with self._connection_lock:
            if connection.closed:
                return
            if self.family == 'AF_UNIX':
                # Closing a descriptor alone does not interrupt another thread's
                # blocked POSIX read; shutdown the socket before closing it.
                wrapped = socket.socket(fileno=connection.fileno())
                try:
                    wrapped.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                finally:
                    wrapped.detach()
            connection.close()

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        with self._connection_lock:
            if self._connection is not None:
                self._abort_connection(self._connection)
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise RuntimeError('control-shutdown-timeout')
        self._listener.close()
        if self._temporary is not None:
            self._temporary.cleanup()

    def __enter__(self): return self
    def __exit__(self, *_args): self.close()


class ControlClient:
    def __init__(self, address, family, key, *, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES):
        if family not in ('AF_PIPE', 'AF_UNIX'):
            raise ValueError('invalid-control-transport')
        self.max_payload_bytes = max_payload_bytes
        self._connection = Client(address, family=family, authkey=key)

    @classmethod
    def from_environment(cls, environment=None):
        env = os.environ if environment is None else environment
        try:
            return cls(env[_PREFIX + 'ADDRESS'], env[_PREFIX + 'FAMILY'], bytes.fromhex(env[_PREFIX + 'KEY']),
                       max_payload_bytes=int(env[_PREFIX + 'LIMIT']))
        except (KeyError, ValueError, OSError):
            raise ValueError('control-unavailable') from None

    def request(self, request=None, *, since_revision=None, statuses=None):
        if request is not None and request != {'version': 1, 'op': 'poll'}:
            raise ValueError('invalid-control-request')
        request = {'version': 1, 'op': 'poll'}
        if since_revision is not None:
            request['since_revision'] = since_revision
        if statuses:
            request['statuses'] = statuses
        if not _poll_request(request):
            raise ValueError('invalid-control-request')
        self._connection.send_bytes(_bytes(request, self.max_payload_bytes))
        if not self._connection.poll(5):
            raise ValueError('control-timeout')
        response = _json(self._connection.recv_bytes(self.max_payload_bytes))
        if set(response) != {'version', 'ack'} or response['version'] != 1 or type(response['ack']) is not dict:
            raise ValueError('invalid-control-ack')
        return response['ack']

    def close(self): self._connection.close()
    def __enter__(self): return self
    def __exit__(self, *_args): self.close()
