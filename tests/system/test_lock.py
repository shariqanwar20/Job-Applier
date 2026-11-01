from jobapplier.system.lock import locked


def test_lock_is_exclusive(tmp_path):
    with locked(str(tmp_path / "l")) as a:
        with locked(str(tmp_path / "l")) as b:
            assert a is True and b is False


def test_lock_is_released_on_exit(tmp_path):
    with locked(str(tmp_path / "l")) as a:
        assert a is True
    with locked(str(tmp_path / "l")) as b:
        assert b is True
