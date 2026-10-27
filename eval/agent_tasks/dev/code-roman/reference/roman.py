PAIRS = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
         (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


def to_roman(n):
    if not isinstance(n, int) or not 1 <= n <= 3999:
        raise ValueError("out of range")
    out = ""
    for value, sym in PAIRS:
        while n >= value:
            out, n = out + sym, n - value
    return out


def from_roman(s):
    values = {sym: v for v, sym in PAIRS if len(sym) == 1}
    if not s or any(c not in values for c in s):
        raise ValueError("invalid numeral")
    total = 0
    for i, c in enumerate(s):
        v = values[c]
        total += -v if i + 1 < len(s) and values[s[i + 1]] > v else v
    if not 1 <= total <= 3999 or to_roman(total) != s:
        raise ValueError("non-canonical numeral")
    return total
