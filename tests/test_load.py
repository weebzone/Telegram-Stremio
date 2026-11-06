from Backend.helper import custom_dl
from Backend.pyrofork.bot import work_loads


def test_load_released_once_even_if_stale_cleaner_and_stream_end_both_run():
    work_loads[7] = 1
    entry = {"client_index": 7}
    custom_dl.release_load(entry)   # stale cleaner
    custom_dl.release_load(entry)   # stream finally block
    assert work_loads[7] == 0


def test_load_never_negative():
    work_loads[8] = 0
    custom_dl.release_load({"client_index": 8})
    assert work_loads[8] == 0
