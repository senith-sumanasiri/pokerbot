"""Opponent memory: what every other bot does, recorded by name after each hand.

Seats change every hand in a tournament, so names are the only reliable way to
know who's who. This memory lasts for a whole batch of tournaments (the engine
keeps our bot object alive between them), so we get smarter as the batch goes on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path

from poker_tournament import evaluate
from poker_tournament.cards import cards_to_ints
from poker_tournament.evaluator import HIGH_CARD, hand_category

from deck_odds import preflop_percentile


STREETS = ("preflop", "flop", "turn", "river")

# Made by my_bots/training/profile_field.py from the organizer's published hand logs:
# what every real opponent did in earlier rounds. We start each batch already knowing
# them, counted as at most PRIOR_HANDS hands, so what we see live soon takes over.
PRIORS = Path(__file__).parent / "field_priors.json"
PRIOR_HANDS = 40


def _rate(count: int, total: int, guess: float, weight: int = 5) -> float:
    """A percentage that starts at a sensible guess and moves towards what we
    actually see. With no data it's the guess; after 100 hands it's almost all data.
    This stops one weird hand from convincing us someone is a maniac."""
    return (count + guess * weight) / (total + weight)


@dataclass
class OpponentStats:
    hands: int = 0                # hands we've watched them play
    played: int = 0               # hands they put chips in when they didn't have to (not just blinds)
    raised_preflop: int = 0       # hands they raised before the flop
    aggressive: int = 0           # hands they bet or raised at any point
    shoves: int = 0               # hands they bet or raised all in
    faced_bet: int = 0            # hands where someone bet into them
    folded_to_bet: int = 0        # ...and they folded
    late_bets_shown: int = 0      # times they bet the turn/river and we later saw their cards
    bluffs_shown: int = 0         # ...and they had nothing
    # the same "bet into / folded" counts, split by street (preflop, flop, turn, river)
    faced_on: list = field(default_factory=lambda: [0, 0, 0, 0])
    folded_on: list = field(default_factory=lambda: [0, 0, 0, 0])
    flop_bets: int = 0            # hands they bet or raised the flop
    barrels: int = 0              # ...and bet or raised the turn too (a "double barrel")
    # What they really had, learned from shown hands (showdown learning):
    shown_raise: list = field(default_factory=list)  # starting-hand percentiles when they raised preflop
    shown_call: list = field(default_factory=list)   # ...when they only called preflop
    big_bets_shown: int = 0       # big turn/river bets (70%+ of the pot, or raises) we later saw
    big_bluffs_shown: int = 0     # ...that were bluffs

    # --- percentages -------------------------------------------------------
    @property
    def play_rate(self) -> float:
        return _rate(self.played, self.hands, 0.35)

    @property
    def raise_rate(self) -> float:
        return _rate(self.raised_preflop, self.hands, 0.15)

    @property
    def shove_rate(self) -> float:
        return _rate(self.shoves, self.hands, 0.03)

    @property
    def fold_to_bet(self) -> float:
        return _rate(self.folded_to_bet, self.faced_bet, 0.4)

    @property
    def bluff_rate(self) -> float:
        return _rate(self.bluffs_shown, self.late_bets_shown, 0.2, weight=3)

    @property
    def aggression(self) -> float:
        return _rate(self.aggressive, self.hands, 0.3)

    @property
    def big_bluff_rate(self) -> float:
        """How often their BIG late bets are bluffs (starts from their overall bluff rate)."""
        return _rate(self.big_bluffs_shown, self.big_bets_shown, self.bluff_rate, weight=3)

    def learned_floor(self, kind: str, formula: float) -> float:
        """Mix the formula's guess for their weakest starting hand with what they've
        really shown. The 20th-percentile shown hand is about their weakest (shown
        hands skew strong, since weak ones fold more often before showdown)."""
        shown = self.shown_raise if kind == "raise" else self.shown_call
        if len(shown) < 3:
            return formula
        learned = sorted(shown)[int(0.2 * len(shown))]
        trust = len(shown) / (len(shown) + 6)
        return (1 - trust) * formula + trust * learned

    def fold_on(self, street_index: int) -> float:
        """Fold rate on one street. Starts from their overall fold rate and moves to
        the street's own numbers as we see more (many bots fold the flop but call the river)."""
        return _rate(self.folded_on[street_index], self.faced_on[street_index], self.fold_to_bet, weight=6)

    @property
    def barrel_rate(self) -> float:
        """After betting the flop, how often they bet the turn again."""
        return _rate(self.barrels, self.flop_bets, 0.5)

    # --- labels ------------------------------------------------------------
    @property
    def is_maniac(self) -> bool:
        """Goes all in (or raises) almost every hand: the failsafe case."""
        return self.hands >= 6 and (self.shove_rate >= 0.3 or self.raise_rate >= 0.6)

    @property
    def is_calling_station(self) -> bool:
        """Never folds when bet into, so bluffing them is throwing chips away."""
        return self.faced_bet >= 6 and self.fold_to_bet < 0.15

    @property
    def is_bluffer(self) -> bool:
        """We've caught them betting big with nothing often enough."""
        return self.late_bets_shown >= 3 and self.bluff_rate >= 0.35


class OpponentTracker:
    def __init__(self, use_priors: bool = True):
        self.stats: dict[str, OpponentStats] = {}
        self.priors: dict = {}
        if use_priors:
            try:
                self.priors = json.loads(PRIORS.read_text(encoding="utf-8"))
            except Exception:
                pass  # no profile of the field yet

    def get(self, name: str) -> OpponentStats:
        if name not in self.stats:
            self.stats[name] = _from_prior(self.priors.get(name))
        return self.stats[name]

    # -----------------------------------------------------------------------
    # Learning: called from on_hand_end with everything that happened.
    # -----------------------------------------------------------------------
    def record_hand(self, result) -> None:
        played, raised, aggressive, shoved, faced, folded, late_bettors = (set() for _ in range(7))
        faced_streets, folded_streets, flop_bettors, turn_bettors = (set() for _ in range(4))

        big_late: set = set()
        pot = 0
        street = None
        street_bets: dict[int, int] = {}
        current_bet = 0
        for event in result.history:
            if event["street"] != street:  # new street: bets start again from zero
                street, street_bets, current_bet = event["street"], {}, 0
            seat, action = event["seat"], event["action"]
            s_index = STREETS.index(street)

            if action not in ("small_blind", "big_blind"):
                if current_bet > street_bets.get(seat, 0):  # someone had bet into this player
                    faced.add(seat)
                    faced_streets.add((seat, s_index))
                    if action == "fold":
                        folded.add(seat)
                        folded_streets.add((seat, s_index))
                if action in ("call", "bet", "raise") and street == "preflop":
                    played.add(seat)
                if action in ("bet", "raise"):
                    aggressive.add(seat)
                    if street == "preflop":
                        raised.add(seat)
                    if street == "flop":
                        flop_bettors.add(seat)
                    if street == "turn":
                        turn_bettors.add(seat)
                    if street in ("turn", "river"):
                        late_bettors.add(seat)
                        if action == "raise" or event["amount"] >= 0.7 * max(pot, 1):
                            big_late.add(seat)
                    if event["all_in"]:
                        shoved.add(seat)

            street_bets[seat] = event["to"]
            current_bet = max(current_bet, event["to"])
            pot += event["amount"]

        for player in result.players:
            if player.seat == result.seat:
                continue  # that's us
            s = self.get(player.name)
            s.hands += 1
            s.played += player.seat in played
            s.raised_preflop += player.seat in raised
            s.aggressive += player.seat in aggressive
            s.shoves += player.seat in shoved
            s.faced_bet += player.seat in faced
            s.folded_to_bet += player.seat in folded
            for i in range(4):
                s.faced_on[i] += (player.seat, i) in faced_streets
                s.folded_on[i] += (player.seat, i) in folded_streets
            if player.seat in flop_bettors:
                s.flop_bets += 1
                s.barrels += player.seat in turn_bettors
            # We saw their cards after they bet late: was it a bluff?
            if player.hole_cards and player.seat in late_bettors and len(result.board) == 5:
                bluff = _was_bluff(player.hole_cards, result.board)
                s.late_bets_shown += 1
                s.bluffs_shown += bluff
                if player.seat in big_late:
                    s.big_bets_shown += 1
                    s.big_bluffs_shown += bluff
            # What did they start with, compared with how they played it before the flop?
            if player.hole_cards and len(player.hole_cards) == 2:
                c1, c2 = cards_to_ints(player.hole_cards)
                shown = s.shown_raise if player.seat in raised else s.shown_call if player.seat in played else None
                if shown is not None:
                    shown.append(preflop_percentile(c1, c2))
                    del shown[:-60]  # keep the most recent 60

    # -----------------------------------------------------------------------
    # Using it: how strong do we think an opponent's cards are right now?
    # -----------------------------------------------------------------------
    def range_spec(self, name: str, seat: int, history: list[dict]) -> tuple:
        """What we think this opponent holds, given how they've played this hand so far:
        (preflop_floor, board_fit, bluff_chance). See deck_odds.simulate for the meaning.

        Before the flop, a raise says "good starting cards". After the flop, a bet says
        "my cards hit this board", and a big bet or a raise says "hit it hard". Bigger
        bets relative to the pot are treated as stronger."""
        s = self.get(name)
        if s.is_maniac:
            return (0.0, 0, 1.0)  # any two cards: don't let them bully us

        # --- preflop: how good are their starting cards? ---
        pre = [e for e in history if e["seat"] == seat and e["street"] == "preflop"]
        if any(e["all_in"] and e["action"] in ("bet", "raise") for e in pre):
            floor = 1 - max(s.shove_rate, 0.08)
        elif any(e["action"] == "raise" for e in pre):
            floor = s.learned_floor("raise", 1 - s.raise_rate * 1.5)  # their best ~raise_rate hands, plus some bluffs
        elif any(e["action"] == "call" for e in pre):
            floor = s.learned_floor("call", (1 - s.play_rate) * 0.7)
        else:
            floor = 0.0  # only posted a blind or checked: could be anything
        if s.is_bluffer:
            floor *= 0.6
        floor = max(0.0, min(0.9, floor))

        # --- after the flop: did they bet, how big, or call? ---
        fit, bluff = 0, 1.0
        pot = 0
        for e in history:
            if e["seat"] == seat and e["street"] != "preflop":
                if e["action"] in ("bet", "raise"):
                    size = e["amount"] / max(pot, 1)  # bet as a share of the pot before it
                    strong = e["action"] == "raise" or size >= 0.7
                    fit = max(fit, 2 if strong else 1)
                    rate = s.big_bluff_rate if strong and e["street"] in ("turn", "river") else s.bluff_rate
                    bluff = min(1.0, max(0.05, rate * (1.5 if s.is_bluffer else 1.0)))
                elif e["action"] == "call" and fit == 0:
                    fit, bluff = 1, 0.4  # callers usually have something, but float more often
            pot += e["amount"]
        return (floor, fit, bluff)


def _was_bluff(hole_cards, board) -> bool:
    """A bluff = their own cards added nothing: their hand was no better than the board alone, or just high card."""
    theirs = hand_category(evaluate(cards_to_ints(hole_cards) + cards_to_ints(board)))
    board_only = hand_category(evaluate(cards_to_ints(board)))
    return theirs == HIGH_CARD or theirs == board_only


def _from_prior(prior: dict | None) -> OpponentStats:
    """A fresh OpponentStats, pre-filled from earlier rounds but shrunk to at most
    PRIOR_HANDS hands' worth, so it's a strong first guess rather than a fixed belief."""
    stats = OpponentStats()
    if not prior or not prior.get("hands"):
        return stats
    scale = min(1.0, PRIOR_HANDS / prior["hands"])
    for f in fields(OpponentStats):
        value = prior.get(f.name)
        if value is None:
            continue
        if f.name in ("shown_raise", "shown_call"):
            setattr(stats, f.name, list(value)[-15:])
        elif isinstance(value, list):
            setattr(stats, f.name, [round(v * scale) for v in value])
        else:
            setattr(stats, f.name, round(value * scale))
    return stats
