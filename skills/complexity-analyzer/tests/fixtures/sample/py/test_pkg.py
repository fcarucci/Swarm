from pkg.x import decide


def test_decide():
    assert decide(0, False) == "neg"
