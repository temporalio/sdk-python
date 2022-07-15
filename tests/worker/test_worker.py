import temporalio.worker.worker


def test_load_default_worker_binary_id():
    # Just run it twice and confirm it didn't change
    val1 = temporalio.worker.worker.load_default_build_id(memoize=False)
    val2 = temporalio.worker.worker.load_default_build_id(memoize=False)
    assert val1 == val2
