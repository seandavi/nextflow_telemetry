"""Unit tests for nf_client.submission — focused on the retry helper.

submit_with_retry wraps subprocess-based submit_* functions with exponential
backoff so transient SLURM/PBS controller failures don't burn the run claim
(see issue #21).
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from nf_client.submission import submit_pbs, submit_slurm, submit_with_retry


def test_submit_with_retry_first_attempt_success() -> None:
    """No retry if the first call succeeds."""
    fn = MagicMock(return_value="12345")
    with patch("nf_client.submission.time.sleep") as sleep_mock:
        assert submit_with_retry(fn, label="sbatch") == "12345"
    assert fn.call_count == 1
    assert sleep_mock.call_count == 0


def test_submit_with_retry_succeeds_after_transient_failures() -> None:
    """Two transient CalledProcessError raises then a success returns the value."""
    err = subprocess.CalledProcessError(1, ["sbatch"], stderr="controller temporarily unreachable")
    fn = MagicMock(side_effect=[err, err, "67890"])
    with patch("nf_client.submission.time.sleep") as sleep_mock:
        assert submit_with_retry(fn, label="sbatch") == "67890"
    assert fn.call_count == 3
    # Two retries → two sleeps (none after the final attempt)
    assert sleep_mock.call_count == 2


def test_submit_with_retry_raises_after_max_attempts() -> None:
    """All attempts fail — the last exception propagates so the caller can record it."""
    err = subprocess.CalledProcessError(2, ["sbatch"], stderr="quota exceeded")
    fn = MagicMock(side_effect=err)
    with patch("nf_client.submission.time.sleep"):
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            submit_with_retry(fn, max_attempts=3, label="sbatch")
    assert exc_info.value is err
    assert fn.call_count == 3


def test_submit_with_retry_exponential_backoff_schedule() -> None:
    """Backoff doubles between attempts: 2.0s, then 4.0s, then 8.0s, ..."""
    err = subprocess.CalledProcessError(1, ["sbatch"])
    fn = MagicMock(side_effect=[err, err, err, "ok"])
    with patch("nf_client.submission.time.sleep") as sleep_mock:
        result = submit_with_retry(
            fn, max_attempts=4, initial_backoff=2.0, backoff_multiplier=2.0, label="sbatch"
        )
    assert result == "ok"
    assert [call.args[0] for call in sleep_mock.call_args_list] == [2.0, 4.0, 8.0]


def test_submit_with_retry_propagates_oserror() -> None:
    """FileNotFoundError (no sbatch in PATH) is treated as retryable."""
    fn = MagicMock(side_effect=[FileNotFoundError("sbatch: not found"), "abc"])
    with patch("nf_client.submission.time.sleep"):
        assert submit_with_retry(fn, label="sbatch") == "abc"
    assert fn.call_count == 2


def test_submit_with_retry_does_not_swallow_unrelated_exceptions() -> None:
    """A non-subprocess/non-OS exception (e.g. ValueError) propagates immediately."""
    fn = MagicMock(side_effect=ValueError("bad arg"))
    with patch("nf_client.submission.time.sleep") as sleep_mock:
        with pytest.raises(ValueError):
            submit_with_retry(fn, label="sbatch")
    assert fn.call_count == 1
    assert sleep_mock.call_count == 0


def test_submit_with_retry_single_attempt_no_sleep() -> None:
    """max_attempts=1 disables retry entirely — first failure raises with no sleep."""
    err = subprocess.CalledProcessError(1, ["sbatch"])
    fn = MagicMock(side_effect=err)
    with patch("nf_client.submission.time.sleep") as sleep_mock:
        with pytest.raises(subprocess.CalledProcessError):
            submit_with_retry(fn, max_attempts=1, label="sbatch")
    assert fn.call_count == 1
    assert sleep_mock.call_count == 0


def test_submit_with_retry_rejects_zero_or_negative_max_attempts() -> None:
    """max_attempts < 1 is a programming error — fail fast, don't silently no-op."""
    fn = MagicMock()
    with pytest.raises(ValueError, match=r"max_attempts must be >= 1"):
        submit_with_retry(fn, max_attempts=0, label="sbatch")
    with pytest.raises(ValueError, match=r"max_attempts must be >= 1"):
        submit_with_retry(fn, max_attempts=-3, label="sbatch")
    assert fn.call_count == 0


def test_submit_with_retry_preserves_original_traceback_on_final_failure() -> None:
    """The last attempt re-raises with bare `raise` (not `raise last_exc`) so the
    operator sees the underlying subprocess call's frame chain, not a synthetic
    `raise last_exc` frame inside this helper.
    """
    err = subprocess.CalledProcessError(2, ["sbatch"])
    fn = MagicMock(side_effect=err)
    with patch("nf_client.submission.time.sleep"):
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            submit_with_retry(fn, max_attempts=2, label="sbatch")
    tb = exc_info.value.__traceback__
    frames = []
    while tb is not None:
        frames.append(tb.tb_frame.f_code.co_name)
        tb = tb.tb_next
    # The chain should pass through submit_with_retry's `try` callsite, not
    # through a synthetic `raise last_exc` block at the bottom of the function.
    assert "submit_with_retry" in frames


def test_submit_slurm_through_retry_helper_end_to_end() -> None:
    """submit_with_retry wrapping submit_slurm survives one failure then succeeds.

    Patches subprocess.run at the submission module level so we exercise the real
    submit_slurm call path (cmd construction, --parsable, --export=NONE, etc.).
    """
    fail = subprocess.CalledProcessError(1, ["sbatch"], stderr="transient")
    ok = MagicMock(stdout="98765\n")
    with patch("nf_client.submission.subprocess.run", side_effect=[fail, ok]) as run_mock:
        with patch("nf_client.submission.time.sleep"):
            job_id = submit_with_retry(
                lambda: submit_slurm("#!/bin/bash\necho hi", export_none=True),
                label="sbatch",
            )
    assert job_id == "98765"
    assert run_mock.call_count == 2
    # The cmd shape didn't change across attempts.
    first_cmd = run_mock.call_args_list[0].args[0]
    assert first_cmd == ["sbatch", "--parsable", "--export=NONE"]


def test_submit_pbs_through_retry_helper_end_to_end() -> None:
    """Same path for qsub."""
    fail = subprocess.CalledProcessError(1, ["qsub"], stderr="transient")
    ok = MagicMock(stdout="11111.pbsserver\n")
    with patch("nf_client.submission.subprocess.run", side_effect=[fail, ok]) as run_mock:
        with patch("nf_client.submission.time.sleep"):
            job_id = submit_with_retry(lambda: submit_pbs("#!/bin/bash\necho hi"), label="qsub")
    assert job_id == "11111.pbsserver"
    assert run_mock.call_count == 2
    assert run_mock.call_args_list[0].args[0] == ["qsub"]
