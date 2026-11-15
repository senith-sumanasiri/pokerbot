"""The maths: the deck we can't see, our chance of winning, and the hands we're likely to make.

Cards are ints 0-51 inside here (that's how the engine stores them):
    rank = card >> 2   (0 = deuce ... 12 = ace)
    suit = card & 3    (0-3)
"""

from __future__ import annotations

import bisect
import itertools
import json
import random
from pathlib import Path

from poker_tournament import evaluate
from poker_tournament.cards import cards_to_ints
from poker_tournament.evaluator import CATEGORY_NAMES, STRAIGHT_HIGH, hand_category

FULL_DECK = list(range(52))


# ---------------------------------------------------------------------------
# 1. The deck "memory": every card we haven't seen yet.
# ---------------------------------------------------------------------------

def remaining_deck(hole_cards, board) -> list[int]:
    """Start from all 52 cards and cross off our hole cards and the community cards.
    What's left is every card that could still be in an opponent's hand or still to come.
    The deck is reshuffled every hand, so this is rebuilt from scratch each time."""
    seen = set(cards_to_ints(hole_cards)) | set(cards_to_ints(board))
    return [card for card in FULL_DECK if card not in seen]


# ---------------------------------------------------------------------------
# 2. Preflop hand strength: how good are two cards before any community cards?
# ---------------------------------------------------------------------------

def _chen_score(c1: int, c2: int) -> float:
    """The Chen formula, a well-known quick way to score two starting cards.
    AA = 20, KK = 16, AKs = 12, 72 offsuit is about -1."""
    r1, r2 = c1 >> 2, c2 >> 2
    high, low = max(r1, r2), min(r1, r2)
    high_points = {12: 10, 11: 8, 10: 7, 9: 6}.get(high, (high + 2) / 2)  # A, K, Q, J, else half the face value
    if r1 == r2:
        return max(5.0, high_points * 2)  # pairs: double, at least 5
    score = high_points
    if c1 & 3 == c2 & 3:
        score += 2  # suited
    gap = high - low - 1
    score -= (0, 1, 2, 4)[gap] if gap < 4 else 5
    if gap <= 1 and high < 10:
        score += 1  # connected, below a queen: easier to make straights
    return score


RANKS = "23456789TJQKA"
_TABLE_FILE = Path(__file__).parent / "preflop_table.json"
# Each starting hand's real chance of winning against 1-9 random opponents, worked out
# offline by my_bots/training/precompute_preflop.py. {"AKs": [vs 1, vs 2, ... vs 9], ...}
PREFLOP_TABLE = json.loads(_TABLE_FILE.read_text()) if _TABLE_FILE.exists() else None


def hand_class(c1: int, c2: int) -> str:
    """e.g. "AKs", "T9o", "77"."""
    r1, r2 = c1 >> 2, c2 >> 2
    hi, lo = max(r1, r2), min(r1, r2)
    if hi == lo:
        return RANKS[hi] * 2
    return RANKS[hi] + RANKS[lo] + ("s" if c1 & 3 == c2 & 3 else "o")


def preflop_equity(c1: int, c2: int, opponents: int) -> float | None:
    """Chance to win against `opponents` random hands, looked up instantly (None if no table)."""
    if PREFLOP_TABLE is None:
        return None
    return PREFLOP_TABLE[hand_class(c1, c2)][min(9, max(1, opponents)) - 1]


def _build_percentiles() -> dict:
    """Rank every one of the 1,326 possible starting hands and turn each into a
    percentile: 1.0 = the best hand (AA), 0.0 = the worst. Ranked by real heads-up
    win chance from the table, or by the Chen formula if the table is missing."""
    scores = {}
    for c1, c2 in itertools.combinations(FULL_DECK, 2):
        scores[(c1, c2)] = preflop_equity(c1, c2, 1) if PREFLOP_TABLE else _chen_score(c1, c2)
    ordered = sorted(scores.values())
    total = len(ordered)
    table = {}
    for (c1, c2), score in scores.items():
        # share of hands that are strictly worse, plus half of the ties
        below = bisect.bisect_left(ordered, score)
        equal = bisect.bisect_right(ordered, score) - below
        table[_key(c1, c2)] = (below + equal / 2) / total
    return table


def _key(c1: int, c2: int) -> tuple:
    r1, r2 = c1 >> 2, c2 >> 2
    return (max(r1, r2), min(r1, r2), (c1 & 3) == (c2 & 3))


_PERCENTILE = _build_percentiles()  # built once when the bot loads (a few milliseconds)


def preflop_percentile(c1: int, c2: int) -> float:
    """1.0 = AA, 0.5 = a middling hand, 0.0 = 72 offsuit."""
    return _PERCENTILE[_key(c1, c2)]


# ---------------------------------------------------------------------------
# 3. The simulation: our chance of winning, and what hand we're likely to end with.
# ---------------------------------------------------------------------------

ANY_HAND = (0.0, 0, 1.0)  # an opponent we know nothing about


def simulate(hole_cards, board, opponent_specs: list[tuple], iterations: int, rng: random.Random) -> dict:
    """Play the rest of the hand out many times, at random, and count what happens.

    opponent_specs has one entry per opponent still in the hand, describing what
    we think they hold (built by OpponentTracker.range_spec):
        (preflop_floor, board_fit, bluff_chance)
        preflop_floor: weakest starting hand they'd play this way, as a percentile.
                       0.0 = any two cards (the all-in maniac), 0.8 = only their top 20%.
        board_fit:     0 = no requirement, 1 = they bet/called after the flop so their
                       cards probably connect with the board (a pair or a draw),
                       2 = they bet big or raised, so probably top pair or better.
        bluff_chance:  how often they'd do that with nothing anyway.
    Hands that don't match are thrown back and redrawn.

    Returns:
        win:        chance we win (ties count as a share), 0 to 1
        hand_odds:  chance of ending with each hand type, e.g. {"Pair": 0.42, ...}
        top_three:  the three most likely hand types, most likely first
    """
    hole = cards_to_ints(hole_cards)
    known = cards_to_ints(board)
    deck = remaining_deck(hole_cards, board)
    missing = 5 - len(known)
    specs = opponent_specs or [ANY_HAND]
    texture = _BoardTexture(known)

    # River and only one opponent: few enough possibilities to check every single one.
    if missing == 0 and len(specs) == 1:
        return _exact_river(hole, known, deck, specs[0], texture)

    win = 0.0
    categories = [0] * len(CATEGORY_NAMES)
    spare = min(len(deck) - missing - 2 * len(specs), 24)  # extra cards to redraw opponents' hands from
    draw_size = missing + 2 * len(specs) + max(0, spare)
    for _ in range(iterations):
        draw = rng.sample(deck, draw_size)
        full_board = known + draw[:missing]
        mine = evaluate(hole + full_board)
        categories[hand_category(mine)] += 1

        pool = draw[missing:]  # cards available for opponents' hands this time
        best, ties = -1, 0
        for spec in specs:
            hand = _pick_hand(pool, spec, texture, rng)
            score = evaluate(hand + full_board)
            if score > best:
                best, ties = score, 1
            elif score == best:
                ties += 1
        if mine > best:
            win += 1.0
        elif mine == best:
            win += 1.0 / (ties + 1)

    return _summary(win / iterations, [c / iterations for c in categories])


def _pick_hand(pool: list[int], spec: tuple, texture, rng) -> list[int]:
    """Take two cards from the pool for one opponent, skipping hands that don't match
    how they've played. If nothing matching turns up, use the first pair."""
    floor, fit, bluff = spec
    if floor > 0 or fit > 0:
        for i in range(0, len(pool) - 1, 2):
            a, b = pool[i], pool[i + 1]
            if preflop_percentile(a, b) < floor:
                continue
            if fit and not texture.fits(a, b, fit) and rng.random() >= bluff:
                continue
            del pool[i : i + 2]
            return [a, b]
    hand = [pool[0], pool[1]]
    del pool[0:2]
    return hand


class _BoardTexture:
    """Quick facts about the community cards, so checking whether an opponent's
    cards 'fit the board' is a few comparisons instead of a full hand evaluation."""

    def __init__(self, board: list[int]):
        self.board = board
        self.ranks = {c >> 2 for c in board}
        self.top = max(self.ranks) if board else -1
        self.suit_counts = [0, 0, 0, 0]
        for c in board:
            self.suit_counts[c & 3] += 1
        self.mask = 0
        for r in self.ranks:
            self.mask |= 1 << r

    def fits(self, a: int, b: int, level: int) -> bool:
        """level 1: a pair with the board, a pocket pair, or a flush draw.
        level 2: top pair or better (overpair, two pair, set, straight) or a flush draw."""
        if not self.board:
            return True
        ra, rb = a >> 2, b >> 2
        need = 5 if len(self.board) == 5 else 4  # river: only a made flush counts, not a missed draw
        flushy = (self.suit_counts[a & 3] + 1 + (a & 3 == b & 3) >= need) or (self.suit_counts[b & 3] + 1 >= need)
        hit_a, hit_b = ra in self.ranks, rb in self.ranks
        if level == 1:
            return hit_a or hit_b or ra == rb or flushy
        if flushy or (hit_a and hit_b) or (ra == rb and (ra > self.top or ra in self.ranks)):
            return True  # flush draw/flush, two pair, overpair or set
        if (hit_a and ra == self.top) or (hit_b and rb == self.top):
            return True  # top pair
        return STRAIGHT_HIGH[self.mask | (1 << ra) | (1 << rb)] >= 0  # made a straight


def _exact_river(hole, board, deck, spec, texture) -> dict:
    """Every possible opponent hand (990 of them), each weighted by how likely they'd
    have played it this way."""
    floor, fit, bluff = spec
    mine = evaluate(hole + board)
    win = total = 0.0
    for a, b in itertools.combinations(deck, 2):
        if preflop_percentile(a, b) < floor:
            continue
        weight = 1.0 if not fit or texture.fits(a, b, fit) else bluff
        if not weight:
            continue
        theirs = evaluate([a, b] + board)
        total += weight
        if mine > theirs:
            win += weight
        elif mine == theirs:
            win += weight / 2
    if total == 0:  # nothing matched: fall back to any hand
        return _exact_river(hole, board, deck, ANY_HAND, texture)
    categories = [0.0] * len(CATEGORY_NAMES)
    categories[hand_category(mine)] = 1.0
    return _summary(win / total, categories)


def _summary(win: float, category_odds: list[float]) -> dict:
    hand_odds = {CATEGORY_NAMES[i]: p for i, p in enumerate(category_odds) if p > 0}
    top_three = sorted(hand_odds.items(), key=lambda item: item[1], reverse=True)[:3]
    return {"win": win, "hand_odds": hand_odds, "top_three": top_three}


def chance_of_at_least(hand_odds: dict, category_name: str) -> float:
    """e.g. chance_of_at_least(odds, "Straight") = chance of a straight or better."""
    start = CATEGORY_NAMES.index(category_name)
    return sum(hand_odds.get(name, 0.0) for name in CATEGORY_NAMES[start:])


def current_category(hole_cards, board) -> str:
    """What we've made right now, e.g. "Pair"."""
    return CATEGORY_NAMES[hand_category(evaluate(cards_to_ints(hole_cards) + cards_to_ints(board)))]
