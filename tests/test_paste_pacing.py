import pytest

from endstone_ninjos_schematics.paste_pacing import PastePacer


def test_healthy_ticks_ramp_up_from_legacy_limit():
    pacer = PastePacer(1200)
    pacer.observe(0, True)
    assert pacer.limit == 256
    for tick in range(1, 81):
        pacer.observe(tick * 0.05, True)
        assert 256 <= pacer.limit <= 1200
    assert pacer.limit == 1200


def test_slow_ticks_back_off_and_idle_resets():
    pacer = PastePacer(1200)
    pacer.limit = 1200
    pacer.observe(0, True)
    pacer.observe(0.1, True)
    assert pacer.limit == 600
    for tick in range(2, 11):
        pacer.observe(tick * 0.1, True)
    assert pacer.limit == 64
    pacer.observe(1.05, False)
    assert pacer.limit == 256
    assert pacer.healthy_ticks == 0


@pytest.mark.parametrize("maximum", [1, 32, 128, 500])
def test_adaptive_limits_always_respect_custom_ceiling(maximum):
    pacer = PastePacer(maximum)
    for tick in range(100):
        pacer.observe(tick * 0.05, True)
        assert 1 <= pacer.limit <= maximum
    for tick in range(100, 120):
        pacer.observe(tick * 0.1, True)
        assert 1 <= pacer.limit <= maximum


def test_catch_up_callbacks_do_not_trigger_acceleration():
    pacer = PastePacer(1200)
    for tick in range(100):
        pacer.observe(tick * 0.001, True)
    assert pacer.limit == 256
