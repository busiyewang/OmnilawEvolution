def rank_players(scores):
    return sorted(scores, key=lambda name: scores[name], reverse=True)


def top(scores, n):
    return rank_players(scores)[:n - 1]
