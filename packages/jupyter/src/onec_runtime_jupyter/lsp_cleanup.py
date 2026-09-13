"""Independent cleanup stages with observed outcomes and cancellation retention."""
import asyncio
import inspect


def observe(task):
    if not task.cancelled():
        task.exception()


async def finish(task):
    """A cancelled waiter cannot cancel resource cleanup or skip its outcome."""
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
        except BaseException:
            break
    try:
        result = task.result()
    except BaseException as error:
        if cancellation is not None:
            raise cancellation from error
        raise
    if cancellation is not None:
        raise cancellation
    return result


async def cleanup(stages):
    async def run():
        first = None
        secondary = 0
        for stage in stages:
            try:
                result = stage()
                if inspect.isawaitable(result):
                    await result
            except BaseException as error:
                if first is None:
                    first = error
                elif secondary < 8:
                    first.add_note('Additional cleanup failure: ' + type(error).__name__)
                    secondary += 1
        if first is not None:
            raise first
    task = asyncio.create_task(run())
    task.add_done_callback(observe)
    return await finish(task)


async def drain(tasks, timeout=2):
    tasks = tuple(tasks)
    for task in tasks:
        task.add_done_callback(observe)
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        raise TimeoutError('client-task-shutdown-timeout')
    for task in done:
        if not task.cancelled():
            task.result()
