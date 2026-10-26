import pytest

from aims.server import AlreadyRunning, single_instance_lock


def test_second_lock_is_refused_and_released_after(tmp_path):
    lock = tmp_path / "server.lock"
    with single_instance_lock(lock):
        with pytest.raises(AlreadyRunning):
            with single_instance_lock(lock):
                pass
    with single_instance_lock(lock):  # leftover file does not block a new start
        pass
