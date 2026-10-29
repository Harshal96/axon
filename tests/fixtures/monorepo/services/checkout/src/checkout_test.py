from checkout import CheckoutFlow


def test_process_payment() -> None:
    flow = CheckoutFlow()
    assert flow is not None
