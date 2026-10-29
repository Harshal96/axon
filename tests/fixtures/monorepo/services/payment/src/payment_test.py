from payment import PaymentService


def test_charge() -> None:
    result = PaymentService().charge(10.0, "USD")
    assert result.status == "success"
