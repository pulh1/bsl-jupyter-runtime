"""POSIX-only bootstrap, executed in a fresh session (never a preexec_fn).

The launcher execs the target in the same PID. Its forked guardian keeps only a
parent-liveness read descriptor, so protocol EOF and launcher exit stay visible.
"""
import os
import signal
import sys


def watch_parent_and_kill_own_group(read_fd, status_fd):
    group = os.getpgrp()
    try:
        # Close even standard pipes: the guardian must not retain stdin/stdout
        # after the target exits. Signal only after this setup actually succeeds.
        limit = max(os.sysconf('SC_OPEN_MAX'), read_fd + 1, status_fd + 1)
        start = 0
        for kept in sorted((read_fd, status_fd)):
            os.closerange(start, kept)
            start = kept + 1
        os.closerange(start, limit)
        os.write(status_fd, b'GUARDIAN\n')
        os.close(status_fd)
        while os.read(read_fd, 1):
            pass
    finally:
        # EOF/error means the sole owning parent disappeared. This is our own
        # launch-time group; no PID lookup or descendant reconstruction occurs.
        os.killpg(group, signal.SIGKILL)


def main():
    read_fd, status_fd = int(sys.argv[1]), int(sys.argv[2])
    command = sys.argv[3:]
    try:
        # EOF after READY proves exec closed this descriptor. An exec failure
        # writes ERROR instead of allowing a dead bootstrap to look ready.
        os.set_inheritable(status_fd, False)
        guardian_pid = os.fork()
        if guardian_pid == 0:
            watch_parent_and_kill_own_group(read_fd, status_fd)
            os._exit(1)
        os.close(read_fd)
        os.write(status_fd, b'READY\n')
        os.execvpe(command[0], command, os.environ)
    except BaseException:
        try: os.write(status_fd, b'ERROR\n')
        except OSError: pass
        os._exit(1)  # Never print command, environment, path or source data.


if __name__ == '__main__':
    main()
