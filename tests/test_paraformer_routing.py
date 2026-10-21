import pytest

from classcue.paraformer_routing import gpu_batch_size, select_batch_size


@pytest.mark.parametrize("total,batch", [
    (2048, 8), (2048 - 64, 8), (3072 - 65, 8), (3072 - 64, 16),
    (3072, 16), (8188, 16), (24 * 1024, 16),
])
def test_gpu_native_capacity_routes_and_caps_high_end(total, batch):
    assert gpu_batch_size(total) == batch
    assert select_batch_size("gpu", gpu_total_mib=total) == batch


@pytest.mark.parametrize("total", [1024, 2048 - 65, 0, None, True, "3072",
                                 float("nan"), float("inf"), -1])
def test_unsupported_or_unknown_capacity_never_selects_gpu(total):
    with pytest.raises(ValueError):
        gpu_batch_size(total)
    with pytest.raises(ValueError):
        select_batch_size("gpu", 1, gpu_total_mib=total)


def test_cpu_default_and_explicit_overrides_are_preserved():
    assert select_batch_size("cpu") == 4
    assert select_batch_size("cpu", 16) == 16
    assert select_batch_size("gpu", 4, gpu_total_mib=8188) == 4


@pytest.mark.parametrize("batch", [0, -1, True, 1.5])
def test_invalid_explicit_batch_is_rejected(batch):
    with pytest.raises(ValueError, match="batch_size"):
        select_batch_size("cpu", batch)
