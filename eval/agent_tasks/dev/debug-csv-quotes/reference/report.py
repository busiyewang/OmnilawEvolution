import csv


def load_rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        return [row for row in csv.DictReader(f) if any(row.values())]


def total_amount(path):
    return sum(float(r["amount"]) for r in load_rows(path))
