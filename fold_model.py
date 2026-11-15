"""The fold predictor: "if we bet this much, how likely is this opponent to fold?"

Bots follow fixed logic, so whether they fold depends mostly on the situation:
the street, how big the bet is compared with the pot (the price they're offered),
how many chips they have left, and their habits. A small logistic-regression model
learns that from thousands of logged hands (my_bots/training/train_fold_model.py)
and is saved in fold_model.npz next to this file.

The same features() function is used for training and for playing, so the model
always sees numbers built exactly the same way.
"""

from __future__ import annotations

import math
from pathlib import Path

WEIGHTS = Path(__file__).parent / "fold_model.npz"

FEATURE_NAMES = [
    "flop", "turn", "river",          # which street (all 0 = before the flop)
    "price", "price_sq",              # share of the final pot they'd pay to call
    "all_in_call",                    # calling would put them all in
    "stack_to_pot",                   # chips they have left / pot, capped at 10
    "players_in",                     # players still in the hand
    "facing_raise",                   # this bet is a raise (someone had already bet this street)
    "was_aggressor",                  # they bet or raised earlier this hand
    "fold_to_bet", "fold_this_street", "aggression", "play_rate", "shove_rate",
    "experience",                     # how many hands we've seen them play (0-1)
    "fold_x_price",                   # their fold habit times the price
]


def features(street_index: int, call: int, pot_after_bet: int, their_stack: int, players_in: int,
             facing_raise: bool, was_aggressor: bool, stats) -> list[float]:
    price = call / max(1, pot_after_bet + call)
    fold = stats.fold_to_bet
    return [
        float(street_index == 1), float(street_index == 2), float(street_index == 3),
        price, price * price,
        float(call >= their_stack),
        min(their_stack / max(1, pot_after_bet), 10.0) / 10.0,
        players_in / 10.0,
        float(facing_raise),
        float(was_aggressor),
        fold, stats.fold_on(street_index), stats.aggression, stats.play_rate, stats.shove_rate,
        min(stats.hands, 100) / 100.0,
        fold * price,
    ]


class FoldModel:
    def __init__(self):
        self.ready = False
        try:
            import numpy as np
            data = np.load(WEIGHTS)
            if list(data["names"]) == FEATURE_NAMES:
                self.mean, self.std = data["mean"].tolist(), data["std"].tolist()
                self.w, self.b = data["w"].tolist(), float(data["b"])
                self.ready = True
        except Exception:
            pass  # no trained model yet (or numpy missing): fall back to the simple estimate

    def predict(self, x: list[float], stats, street_index: int) -> float:
        """Chance (0-1) that this opponent folds."""
        if not self.ready:
            # Simple fallback: their fold rate on this street, less likely the cheaper the call.
            return max(0.0, min(0.95, stats.fold_on(street_index) * (0.6 + x[3])))
        z = self.b
        for value, mean, std, weight in zip(x, self.mean, self.std, self.w):
            z += weight * (value - mean) / std
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
