import sys

import pytest

from emuflow.process_output import run_with_bounded_output


def test_bounded_output_retains_only_tail() -> None:
    completed = run_with_bounded_output(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'a' * 10000 + b'END')",
        ],
        maximum_bytes=128,
    )
    assert completed.returncode == 0
    assert len(completed.stdout.encode()) == 128
    assert completed.stdout.endswith("END")
    assert completed.stdout == "a" * 125 + "END"


def test_bounded_output_combines_stderr_and_preserves_failure() -> None:
    completed = run_with_bounded_output(
        [
            sys.executable,
            "-c",
            "import sys; print('before'); print('failure', file=sys.stderr); raise SystemExit(7)",
        ],
        maximum_bytes=1024,
    )
    assert completed.returncode == 7
    assert "before" in completed.stdout
    assert "failure" in completed.stdout


def test_bounded_output_rejects_invalid_contract() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        run_with_bounded_output([])
    with pytest.raises(ValueError, match="must be positive"):
        run_with_bounded_output([sys.executable, "--version"], maximum_bytes=0)
