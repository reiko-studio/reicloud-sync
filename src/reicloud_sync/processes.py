"""Keep live rclone diagnostics while allowing graceful transfer shutdown."""

from collections import deque
import subprocess
import sys
import threading

from .activity import current_recorder, sanitize


def run_locked(command, environment, capture, lock_fd, *, bisync=False):
    recent = deque(maxlen=200)
    recorder = current_recorder()
    with subprocess.Popen(command, env=environment, text=True, stderr=subprocess.PIPE,
                          stdout=subprocess.PIPE if capture else None, pass_fds=(lock_fd,)) as process:
        stream = sys.stderr
        def relay():
            for line in process.stderr:
                recent.append(line)
                if recorder:
                    recorder.record('rclone', message=sanitize(line))
                stream.write(line)
                stream.flush()
        reader = threading.Thread(target=relay, daemon=True)
        reader.start()
        try:
            output = process.stdout.read() if capture else None
            process.wait()
        except KeyboardInterrupt:
            # The terminal or runner already signaled the process group.
            try:
                process.wait(timeout=95)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                process.kill()
                process.wait()
                raise KeyboardInterrupt() from None
            if process.returncode != 0 or not bisync:
                raise
            output = None
        finally:
            reader.join()
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, output, ''.join(recent))
        return subprocess.CompletedProcess(command, process.returncode, output)
