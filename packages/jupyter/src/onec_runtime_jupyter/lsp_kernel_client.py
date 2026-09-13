"""Server-owned Jupyter client. Never executes code to discover a project."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
import inspect
from queue import Empty
from threading import RLock
from uuid import uuid4

from .lsp_kernel import TARGET
from .lsp_project import ProjectLimits, decode_envelope
from .lsp_cleanup import drain, finish, observe


async def _await(value):
    return await value if inspect.isawaitable(value) else value


class KernelProjectClient:
    def __init__(self, kernel_manager, kernel_id, on_config, on_degraded=None,
                 limits=ProjectLimits()):
        self.kernel_manager, self.kernel_id = kernel_manager, kernel_id
        self.on_config = on_config
        self.on_degraded = on_degraded or (lambda *_args: None)
        self.limits = limits
        self.transport = None
        self.incarnation = None
        self.installation_id = None
        self.info_request_id = None
        self.comm_id = None
        self._epoch = -1
        self._last_config = None
        self._task = None
        self._monitor_task = None
        self.closed = False
        self._started = False
        self._loop = None
        self._close_future = None
        self._lifecycle_lock = RLock()
        self._generation = 0

    async def start(self):
        with self._lifecycle_lock:
            if self._started and not self.closed:
                return
            self.close()
            generation = self._generation
        await self.wait_closed()
        try:
            with self._lifecycle_lock:
                # Even an idempotent close revokes this pending start. Reopening
                # and registering its task must be atomic against worker close.
                if generation != self._generation:
                    return
                self._loop = asyncio.get_running_loop()
                self.closed, self._started = False, True
                self._close_future = None
                if self.kernel_manager is None:
                    return
                self._task = asyncio.create_task(self._connect())
                self._task.add_done_callback(observe)
            await self._task
            with self._lifecycle_lock:
                if self.closed or generation != self._generation:
                    return
                self._task = asyncio.create_task(self._listen())
                self._monitor_task = asyncio.create_task(self._monitor())
                self._task.add_done_callback(observe)
                self._monitor_task.add_done_callback(observe)
        except asyncio.CancelledError:
            with self._lifecycle_lock:
                if self.closed or generation != self._generation:
                    return
                self.close()
            raise
        except Exception:
            with self._lifecycle_lock:
                if generation != self._generation:
                    return
                self.close()
            self.on_degraded(self, 'kernel-unavailable')

    async def _connect(self):
        self._loop = asyncio.get_running_loop()
        generation = self._generation
        manager = await _await(self.kernel_manager.get_kernel(self.kernel_id))
        if self.closed or generation != self._generation:
            return
        self.transport = manager.client()
        # KernelManager.client clones Session but preserves its ROUTER identity.
        # Every observer needs a unique shell route, including shared kernels.
        self.transport.session.session = uuid4().hex
        self.transport.start_channels()
        try:
            await self._probe()
        except (Empty, asyncio.TimeoutError):
            self.on_degraded(self, 'kernel-info-pending')

    def accept_info(self, message):
        if self.closed or type(message) is not dict or self.info_request_id is None:
            return False
        parent, header = message.get('parent_header'), message.get('header')
        if (
            type(parent) is not dict or type(header) is not dict
            or parent.get('msg_id') != self.info_request_id
        ):
            return False
        incarnation = header.get('session')
        if type(incarnation) is not str or not incarnation:
            return False
        self.info_request_id = None
        if incarnation == self.incarnation and self.comm_id is not None:
            return True
        if self.comm_id is not None:
            self.transport.shell_channel.send(self.transport.session.msg('comm_close', {
                'comm_id': self.comm_id, 'data': {},
            }))
        if self.incarnation is not None and incarnation != self.incarnation:
            self.installation_id = None
            self.on_degraded(self, 'kernel-incarnation-changed')
        self.incarnation = incarnation
        self.comm_id = uuid4().hex
        self._epoch, self._last_config, self.installation_id = -1, None, None
        self.transport.shell_channel.send(self.transport.session.msg('comm_open', {
            'comm_id': self.comm_id, 'target_name': TARGET, 'data': {'version': 2},
        }))
        return True

    async def _probe(self):
        self.info_request_id = self.transport.kernel_info()

        async def correlate():
            while not self.accept_info(await _await(self.transport.get_shell_msg(timeout=5))):
                pass

        await asyncio.wait_for(correlate(), timeout=5)

    async def _monitor(self):
        # Correlated probes discover restarts. Project discovery itself is comm-only.
        try:
            while True:
                await asyncio.sleep(2)
                try:
                    if self._task is not None and self._task.done():
                        self._disconnect_transport()
                        await self._connect()
                        self._task = asyncio.create_task(self._listen())
                        self._task.add_done_callback(observe)
                    else:
                        await self._probe()
                except asyncio.CancelledError:
                    raise
                except (Empty, asyncio.TimeoutError):
                    continue
                except Exception:
                    self.incarnation = self.comm_id = self.installation_id = None
                    self.on_degraded(self, 'kernel-unavailable')
        except asyncio.CancelledError:
            pass

    async def _listen(self):
        try:
            while True:
                try:
                    message = await _await(self.transport.get_iopub_msg(timeout=1))
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if isinstance(error, (Empty, asyncio.TimeoutError)):
                        continue
                    raise
                self.accept_message(message)
        except asyncio.CancelledError:
            pass
        except Exception:
            self._disconnect_transport()
            self.on_degraded(self, 'kernel-transport-unavailable')

    def accept_message(self, message):
        if self.closed or type(message) is not dict:
            return False
        header, content = message.get('header', {}), message.get('content', {})
        if type(header) is not dict or type(content) is not dict:
            return False
        if (
            header.get('session') != self.incarnation
            or content.get('comm_id') != self.comm_id
            or self.comm_id is None
        ):
            return False
        if header.get('msg_type') == 'comm_close':
            self.comm_id = self.installation_id = None
            self.on_degraded(self, 'runtime-unavailable')
            return False
        if header.get('msg_type') != 'comm_msg':
            return False
        data = content.get('data', {})
        try:
            epoch, config, reason = decode_envelope(data, self.limits)
        except ValueError:
            return self._reject_invalid_config()
        if epoch < self._epoch:
            return False
        state = (config, reason)
        if epoch == self._epoch:
            return state == self._last_config
        self._epoch, self._last_config = epoch, state
        if config is None:
            self.installation_id = None
            self.on_degraded(self, reason)
            return False
        self.installation_id = config.installation_id
        self.on_config(self, self.incarnation, config)
        return True

    def _reject_invalid_config(self):
        if self._last_config is None or self._last_config[0] is None:
            self.on_degraded(self, 'project-config-invalid-or-limited')
        return False

    def close(self):
        # Registry callers may hold an RLock on the control worker. Revoke now;
        # never wait here or touch tasks/channels off their owning event loop.
        with self._lifecycle_lock:
            self._generation += 1
            if self.closed and self._close_future is not None:
                return self._close_future
            self.closed = True
            completion = self._close_future = Future()
            loop = self._loop
            if loop is None:
                task = self._task or self._monitor_task
                loop = task.get_loop() if task is not None else None
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if loop is None or current is loop:
                self._close_on_loop(completion)
            elif loop.is_running():
                try:
                    loop.call_soon_threadsafe(self._close_on_loop, completion)
                except RuntimeError as error:
                    completion.set_exception(error)
            else:
                completion.set_exception(RuntimeError('kernel-client-owner-loop-unavailable'))
            return completion

    def _close_on_loop(self, completion):
        # start() awaits this exact completion before it can replace any fields.
        tasks = tuple(task for task in (self._monitor_task, self._task) if task is not None)
        self._monitor_task = self._task = None
        failure = None
        for task in tasks:
            try:
                task.cancel()
            except BaseException as error:
                failure = failure or error
        try:
            self._disconnect_transport()
        except BaseException as error:
            failure = failure or error
        if not tasks:
            if failure is None:
                completion.set_result(None)
            else:
                completion.set_exception(failure)
            return

        async def complete():
            try:
                await drain(tasks)
                if failure is not None:
                    raise failure
            except BaseException as error:
                completion.set_exception(error)
            else:
                completion.set_result(None)
        task = asyncio.create_task(complete())
        task.add_done_callback(observe)

    async def wait_closed(self):
        completion = self._close_future
        if completion is not None:
            await finish(asyncio.create_task(drain([asyncio.wrap_future(completion)], timeout=3)))

    def _disconnect_transport(self):
        transport, comm_id = self.transport, self.comm_id
        self.transport = None
        self.incarnation = self.installation_id = self.comm_id = self.info_request_id = None
        if transport is not None:
            failure = None
            try:
                if comm_id is not None:
                    transport.shell_channel.send(transport.session.msg(
                        'comm_close', {'comm_id': comm_id, 'data': {}},
                    ))
            except Exception as error:
                failure = error
            finally:
                try:
                    transport.stop_channels()
                except Exception as error:
                    failure = failure or error
            if failure is not None:
                raise failure
