from decimal import Decimal


# Learning example: 50.00 AED -> multiply by 100 -> 5000 fils.
# pytest discovers files named test_*.py and functions named test_*.
# This demonstrates assert; it does not test the real TopUp service yet.
def test_amount_conversion():
    # Arrange: prepare the input using Decimal for exact decimal arithmetic.
    amount = Decimal("50.00")

    # Act: convert this valid two-decimal amount to minor units.
    amount_minor = int(amount * 100)

    # Assert: a false condition fails the test; a true condition passes it.
    assert amount_minor == 5000
