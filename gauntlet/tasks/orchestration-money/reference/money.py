"""Money in integer cents."""


class Money:
    def __init__(self, cents, currency):
        self.cents, self.currency = int(cents), currency

    def add(self, other):
        if other.currency != self.currency:
            raise ValueError("Currency mismatch")
        return Money(self.cents + other.cents, self.currency)

    def __eq__(self, other):
        return isinstance(other, Money) and (self.cents, self.currency) == (other.cents, other.currency)

    def __str__(self):
        return f"{self.currency} {self.cents // 100}.{self.cents % 100:02d}"
