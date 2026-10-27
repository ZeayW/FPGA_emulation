"""Small requests to the shared native external-engine lifecycle.

This adapter carries argv/environment and bounded diagnostics, never circuit
records. Explicit tools without an installed unified runtime remain usable for
reference tests. Once selected, a native runtime failure is never retried through
Python's process launcher.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import tempfile
from typing import Mapping, Optional, Sequence

from emuflow.common.errors import EmuFlowError
from emuflow.common.native_tools import native_install_roots


def native_tool_runner(executable: str) -> Optional[str]:
    # A thin, versioned runtime symlinks unchanged engines from its native base.
    # Select the invocation's sibling before dereferencing that symlink, or an
    # older base supervisor could override the current source deployment.
    candidates = [Path(executable).expanduser().absolute().parent / "emuflow-native"]
    candidates.extend(root / "bin/emuflow-native" for root in native_install_roots())
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _request_bytes(
    command: Sequence[str], *, cwd: Optional[Path],
    environment: Optional[Mapping[str, str]], forward_stdout: bool,
    maximum_bytes: int, forward_stdin: bool = False, timeout_ms: int, termination_grace_ms: int,
    cpu_seconds: int, address_space_bytes: int, file_size_bytes: int,
) -> bytes:
    if not command or not Path(command[0]).is_absolute():
        raise ValueError("native tool requires an absolute executable")
    if not 0 < maximum_bytes <= 2 * 1024 * 1024:
        raise ValueError("native diagnostic tail must be within 1..2097152 bytes")
    payload = bytearray(struct.pack(
        "<QIIIIQIQQQ", 0x31514552544C4645, 2 if forward_stdin else 1, int(forward_stdout) | (2 if forward_stdin else 0),
        int(environment is None), maximum_bytes, timeout_ms, termination_grace_ms,
        cpu_seconds, address_space_bytes, file_size_bytes,
    ))

    def text(value: str) -> None:
        if "\0" in value:
            raise ValueError("NUL in external tool request")
        raw = value.encode("utf-8")
        payload.extend(struct.pack("<I", len(raw)))
        payload.extend(raw)
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError("external tool request exceeds bound")

    text(str(Path.cwd() if cwd is None else Path(cwd).resolve()))
    if len(command) > 4096:
        raise ValueError("external tool argv exceeds bound")
    payload.extend(struct.pack("<I", len(command)))
    for arg in command:
        text(arg)
    env = {} if environment is None else environment
    if len(env) > 4096:
        raise ValueError("external tool environment exceeds bound")
    payload.extend(struct.pack("<I", len(env)))
    for key, value in sorted(env.items()):
        text(key)
        text(value)
    return bytes(payload)


class NativeToolProcess:
    """Own one supervisor and its ephemeral request/result files.

    The native supervisor owns the engine's process group. Closing this context
    while a stream consumer fails requests native cancellation and waits for
    engine cleanup before deleting the request/result scratch.
    """

    def __init__(
        self, runner: str, command: Sequence[str], *,
        cwd: Optional[Path] = None,
        environment: Optional[Mapping[str, str]] = None,
        forward_stdout: bool = False,
        stdin=None,
        maximum_bytes: int = 2 * 1024 * 1024,
        scratch_parent: Optional[Path] = None,
        timeout_ms: int = 0, termination_grace_ms: int = 1000,
        cpu_seconds: int = 0, address_space_bytes: int = 0,
        file_size_bytes: int = 0,
    ) -> None:
        payload = _request_bytes(
            command, cwd=cwd, environment=environment,
            forward_stdout=forward_stdout, forward_stdin=stdin is not None, maximum_bytes=maximum_bytes,
            timeout_ms=timeout_ms, termination_grace_ms=termination_grace_ms,
            cpu_seconds=cpu_seconds, address_space_bytes=address_space_bytes,
            file_size_bytes=file_size_bytes,
        )
        self.command = list(command)
        self.maximum_bytes = maximum_bytes
        self.metrics = None
        self.diagnostic_tail = ""
        self._completed = None
        self._scratch = tempfile.TemporaryDirectory(
            prefix=".emuflow-tool-", dir=scratch_parent,
        )
        root = Path(self._scratch.name)
        self._result_path = root / "result.json"
        request = root / "request.bin"
        request.write_bytes(payload)
        self._errors = (root / "supervisor.stderr").open("w+b")
        try:
            self.process = subprocess.Popen(
                [runner, "run-tool", str(request), str(self._result_path)],
                stdout=subprocess.PIPE if forward_stdout else subprocess.DEVNULL,
                stderr=self._errors,
                stdin=stdin,
            )
        except OSError as error:
            self._errors.close()
            self._scratch.cleanup()
            raise EmuFlowError(f"native tool supervisor failed to launch: {error}") from error
        except BaseException:
            self._errors.close()
            self._scratch.cleanup()
            raise

    @property
    def stdout(self):
        return self.process.stdout

    def finish(self) -> subprocess.CompletedProcess[str]:
        if self._completed is not None:
            return self._completed
        driver_code = self.process.wait()
        self._errors.seek(0, os.SEEK_END)
        self._errors.seek(max(0, self._errors.tell() - 32768))
        driver_error = self._errors.read().decode("utf-8", errors="replace")
        if (driver_code not in {0, 1} or not self._result_path.is_file() or
                self._result_path.stat().st_size > 4 * 1024 * 1024 + 65536):
            raise EmuFlowError(f"native tool supervisor failed ({driver_code}): {driver_error}")
        result = json.loads(self._result_path.read_text(encoding="utf-8"))
        operation = result.get("operation", {})
        if (result.get("schema") != "emuflow.external-tool-result/v1" or
                operation.get("schema") != "emuflow.native-stage-result/v1" or
                operation.get("stage") != "native.external-tool" or
                (driver_code == 0) != (operation.get("status") == "pass")):
            raise EmuFlowError("native tool supervisor result disagrees with its exit")
        tail = bytes.fromhex(result["diagnostic_tail_hex"])
        if len(tail) > self.maximum_bytes:
            raise EmuFlowError("native tool diagnostic bound violated")
        self.diagnostic_tail = tail.decode("utf-8", errors="replace")
        if (not result.get("started") or result.get("spawn_errno") or
                operation.get("error", {}).get("code") not in {"none", "execution-failed"}):
            raise EmuFlowError("native tool launch failed: " + operation.get("error", {}).get("message", ""))
        if result.get("timed_out"):
            raise subprocess.TimeoutExpired(self.command, None)
        if result.get("cancelled_signal"):
            raise EmuFlowError("native external tool was cancelled")
        self.metrics = result["engine_metrics"]
        self._completed = subprocess.CompletedProcess(
            self.command, result["returncode"],
            stdout=self.diagnostic_tail,
        )
        # Metrics remain available to native orchestration and callers of this
        # adapter; they are not relabelled as process-tree stage RSS.
        self._completed.native_metrics = self.metrics
        return self._completed

    def __enter__(self):
        return self

    def cancel(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            return self.finish()
        except (EmuFlowError, subprocess.TimeoutExpired):
            # Preserve an importer's original error. finish() still captures
            # the bounded engine tail before reporting native cancellation.
            pass

    def __exit__(self, *_):
        try:
            if self.process.poll() is None:
                self.cancel()
        finally:
            if self.stdout is not None:
                self.stdout.close()
            if self.process.stdin is not None and not self.process.stdin.closed:
                try:
                    self.process.stdin.close()
                except BrokenPipeError:
                    pass
            self._errors.close()
            self._scratch.cleanup()
