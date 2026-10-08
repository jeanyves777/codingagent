"""Invoices."""


class Invoice:
    def __init__(self, currency):
        self.currency = currency
        self.lines = []

    def add_line(self, description, cents):
        self.lines.append((description, cents))

    def total(self):
        """Return the invoice total in cents."""
        return sum(cents for _, cents in self.lines)
