def load_rows(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        header = f.readline().strip().split(",")
        for line in f:
            if line.strip():
                rows.append(dict(zip(header, line.strip().split(","))))
    return rows


def total_amount(path):
    return sum(float(r["amount"]) for r in load_rows(path))
