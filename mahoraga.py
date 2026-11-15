"""Eight-Handled Sword Divergent Sila Divine General Mahoraga: the adaptation layer.

On top of the base logic (bot.py), every opponent gets a set of WHEELS, one for each
way they can hurt us. A wheel has 8 notches each way. The further it has turned,
the more we've adapted to that opponent's habit; at 8 we're fully adapted. If they
change and stop doing it, the wheel turns back.

How a wheel turns: every time the situation comes up, we compare what we PREDICTED
(the fold model's chance they fold, our win chance when we call) with what really
happened. The wheel's position is how wrong our predictions have been against that
player, mostly over the last ~50 times (older events fade out, so we re-adapt when
a bot changes style). A bot that behaves exactly as predicted never moves its wheel,
and one lucky hand barely moves it. Wheels also start slowly: the first few events
count for less (we assume "as predicted" until we've seen enough).

Mahoraga doesn't just defend: once a wheel has turned, we COUNTER the habit it found.

  wheel    turns + when...                        defend (+)                   counter
  -------  -------------------------------------  ---------------------------  --------------------------------
  calls    our turn/river calls against them win  need up to 8% more to call   at -: we win more than expected
           less often than our win chance said    their bets (1% per notch)    (they bluff us): call lighter
  bluffs   they fold to our flop+ bets less       expect them to fold less     +: they call wide: value-bet
           often than the fold model predicted    (at 8: half as often)        thinner. -: they fold a lot:
                                                                               bluff bigger, expect more folds
  steals   the same, for our preflop raises       expect fewer folds to our    call their re-raises up to 8%
                                                  raises                       lighter (they re-raise light)
  attacks  they bet when we check to them more    -                            call their bets after our check
           than an ordinary bot (~40%)                                         up to 8% lighter

Plus a STYLE wheel for the whole table (choose_style): play the style with the best
record this batch, sometimes switching, so bots that study us chase a moving target.

Wheels can start partly turned: profile_field.py saves what round 1's logs showed,
and we load it at half weight (see load).
"""

from __future__ import annotations

import random

TURNS = 8          # notches each way: full adaptation
FADE = 0.97        # each new event, older ones count this much less (~50-event memory)
# Events' worth of "as predicted" each wheel starts with: more for noisier wheels
# (a call wins or loses on the river card, so one result says very little).
PRIOR = {"calls": 20.0, "bluffs": 8.0, "steals": 8.0, "attacks": 6.0}
ATTACK_BASE = 0.4  # how often an ordinary bot bets when we check to it

WHEELS = ("calls", "bluffs", "steals", "attacks")

STYLES = {
    # name: settings that change from the base PARAMS
    "standard": {},
    "aggressive": {"open_raise": 0.95, "play_floor": 0.34, "random_bluff": 0.05, "cap_scale": 1.25},
    "tight": {"open_raise": 0.6, "play_floor": 0.48, "random_bluff": 0.0, "cap_scale": 0.85, "steal": False},
}


COUNTERS = ("call_light", "attack", "resteal", "value", "bluff")


class Mahoraga:
    def __init__(self, counters=True, priors=None):
        """counters: True (all), False (none) or a list of names from COUNTERS.
        priors: change some wheels' starting cushion, e.g. {"calls": 60}."""
        self.prior = {**PRIOR, **(priors or {})}
        if counters is True:
            counters = COUNTERS
        self.on = set(counters or ())
        self.counters = bool(self.on)
        # (name, wheel) -> [sum of predictions, sum of outcomes, events], all fading
        self.records: dict[tuple[str, str], list[float]] = {}
        self.style = "standard"
        self.style_record = {name: [0.0, 0] for name in STYLES}  # [wins, tournaments]

    # --- saving and loading -------------------------------------------------------------
    def export(self) -> dict:
        out: dict = {}
        for (name, wheel), record in self.records.items():
            out.setdefault(name, {})[wheel] = [round(v, 3) for v in record]
        return out

    def load(self, saved: dict) -> None:
        """Start from what an earlier round showed, at half weight."""
        for name, wheels in (saved or {}).items():
            for wheel, record in wheels.items():
                if wheel in WHEELS and isinstance(record, list) and len(record) == 3:
                    self.records[(name, wheel)] = [v / 2 for v in record]

    # --- the wheels -----------------------------------------------------------------------
    def _observe(self, name: str, wheel: str, predicted: float, actual: float) -> None:
        r = self.records.setdefault((name, wheel), [0.0, 0.0, 0.0])
        r[0] = r[0] * FADE + predicted
        r[1] = r[1] * FADE + actual
        r[2] = r[2] * FADE + 1

    def _gap(self, name: str, wheel: str) -> tuple[float, float]:
        """(average prediction, average outcome), with PRIOR events of 'as predicted'."""
        r = self.records.get((name, wheel))
        if not r or r[2] <= 0:
            return 0.0, 0.0
        mean_pred = r[0] / r[2]
        prior = self.prior[wheel]
        return mean_pred, (r[1] + prior * mean_pred) / (r[2] + prior)

    def _fold_ratio(self, name: str, wheel: str) -> float:
        """How often they really fold compared with the prediction (1 = as predicted)."""
        pred, real = self._gap(name, wheel)
        if pred <= 0.02:
            return 1.0
        return max(0.5, min(1.25, real / pred))

    def position(self, name: str, wheel: str) -> int:
        """Notches turned, -8..+8 (+ = they hurt us that way)."""
        if wheel in ("bluffs", "steals"):
            ratio = self._fold_ratio(name, wheel)
            pos = (1 - ratio) / 0.5 * TURNS if ratio < 1 else -(ratio - 1) / 0.25 * TURNS
        elif wheel == "calls":
            pred, real = self._gap(name, wheel)
            pos = (pred - real) * 100          # 1 notch per 1% we've been too optimistic
        else:  # attacks
            pred, real = self._gap(name, wheel)
            pos = (real - pred) * 100 * 0.15   # 1 notch per ~7% more betting than ordinary
        return int(round(max(-TURNS, min(TURNS, pos))))

    # --- what the wheels change -------------------------------------------------------------
    def call_margin(self, name: str) -> float:
        """Extra win chance we need to call this player's bet (negative = call lighter)."""
        pos = self.position(name, "calls")
        if pos < 0 and "call_light" not in self.on:
            pos = 0
        return 0.01 * pos

    def attack_discount(self, name: str) -> float:
        """They bet whenever we check: how much lighter we call those bets."""
        return 0.01 * max(0, self.position(name, "attacks")) if "attack" in self.on else 0.0

    def resteal_discount(self, name: str) -> float:
        """They fight back against our preflop raises: how much lighter we call their re-raises."""
        return 0.01 * max(0, self.position(name, "steals")) if "resteal" in self.on else 0.0

    def fold_factor(self, name: str, preflop: bool) -> float:
        """Multiply the fold model's prediction for this player by this."""
        return self._fold_ratio(name, "steals" if preflop else "bluffs")

    def calls_wide(self, name: str) -> float:
        """0-1: how far their 'bluffs' wheel says they call us more than expected."""
        return max(0, self.position(name, "bluffs")) / TURNS if "value" in self.on else 0.0

    def folds_a_lot(self, name: str) -> float:
        """0-1: how far their 'bluffs' wheel says they fold to us more than expected."""
        return max(0, -self.position(name, "bluffs")) / TURNS if "bluff" in self.on else 0.0

    # --- learning from a finished hand -------------------------------------------------
    def learn(self, result, notes: dict | None = None) -> None:
        """notes (from bot.py): what we PREDICTED during the hand.
            notes["bets"][street]  = {opponent name: predicted chance they fold to our bet}
            notes["calls"][street] = (bettor name, our win chance when we called)
        Without notes (replaying old logs) only the attacks wheel can learn."""
        notes = notes or {}
        me = result.seat
        names = {p.seat: p.name for p in result.players}
        folded = {p.seat for p in result.players if p.folded}
        we_won = result.net > 0

        street = None
        last_aggressor = None      # who made the last bet/raise on this street
        we_checked = False         # we checked this street
        answered = set()           # opponents already counted since the last bet
        called = {}                # street -> seat whose bet we called
        for e in result.history:
            if e["street"] != street:
                street, last_aggressor, we_checked, answered = e["street"], None, False, set()
            seat, action = e["seat"], e["action"]
            if action in ("small_blind", "big_blind"):
                continue
            if seat == me:
                if action == "check" and last_aggressor is None and street != "preflop":
                    we_checked = True
                if action == "call" and last_aggressor is not None and street in ("turn", "river"):
                    called[street] = last_aggressor
            else:
                name = names[seat]
                if we_checked and last_aggressor is None and seat not in answered:
                    self._observe(name, "attacks", ATTACK_BASE, 1.0 if action in ("bet", "raise") else 0.0)
                    answered.add(seat)
                elif last_aggressor == me and seat not in answered:
                    predicted = notes.get("bets", {}).get(street, {}).get(name)
                    if predicted is not None:
                        wheel = "steals" if street == "preflop" else "bluffs"
                        self._observe(name, wheel, predicted, 1.0 if action == "fold" else 0.0)
                    answered.add(seat)
            if action in ("bet", "raise"):
                last_aggressor = seat
                answered = set()

        if not result.showdown:
            return
        for st, seat in called.items():
            note = notes.get("calls", {}).get(st)
            if seat in folded or note is None or note[0] != names[seat]:
                continue
            self._observe(names[seat], "calls", note[1], 1.0 if we_won else 0.0)

    # --- the style wheel -----------------------------------------------------------------
    def end_tournament(self, won: float) -> None:
        record = self.style_record[self.style]
        record[0] += won
        record[1] += 1

    def choose_style(self, rng: random.Random) -> str:
        """Before each tournament: usually the style with the best record so far in
        this batch, but 25% of the time a different one, so we never sit still."""
        def score(name):
            wins, played = self.style_record[name]
            return (wins + 0.3) / (played + 1)  # untried styles look decent, so each gets a go
        best = max(STYLES, key=score)
        if rng.random() < 0.25:
            self.style = rng.choice([s for s in STYLES if s != self.style])
        else:
            self.style = best
        return self.style


class NotchMahoraga(Mahoraga):
    """Version 4's wheels: one notch per event, no predictions needed.

    calls:  we called their turn/river bet and lost at showdown +1, won -1.
    bluffs: they folded to our flop+ bet -1; called it and beat us +1.
    steals: they folded to our preflop raise -1; re-raised our open +1.
    (No attacks wheel.) Fast to react, but ordinary events turn it too, so it
    drifts. The counters (COUNTERS) work on top of it exactly as for the
    calibrated wheels."""

    def __init__(self, counters=True, priors=None):
        super().__init__(counters, priors)
        self.wheels: dict[tuple[str, str], int] = {}

    def export(self) -> dict:
        return {}

    def load(self, saved: dict) -> None:
        pass  # saved wheels are calibrated records: not used by notches

    def position(self, name: str, wheel: str) -> int:
        return self.wheels.get((name, wheel), 0)

    def _fold_ratio(self, name: str, wheel: str) -> float:
        pos = self.position(name, wheel)
        return 1 - 0.5 * pos / TURNS if pos > 0 else 1 - 0.25 * pos / TURNS

    def _turn(self, name: str, wheel: str, step: int) -> None:
        key = (name, wheel)
        self.wheels[key] = max(-TURNS, min(TURNS, self.wheels.get(key, 0) + step))

    def learn(self, result, notes: dict | None = None) -> None:
        me = result.seat
        names = {p.seat: p.name for p in result.players}
        folded = {p.seat for p in result.players if p.folded}
        we_won = result.net > 0
        street = None
        last_aggressor = None
        we_called, we_bet_into = set(), set()
        our_open = False
        for e in result.history:
            if e["street"] != street:
                street, last_aggressor = e["street"], None
            seat, action = e["seat"], e["action"]
            if action in ("small_blind", "big_blind"):
                continue
            if seat == me:
                if action == "call" and last_aggressor is not None and street in ("turn", "river"):
                    we_called.add(last_aggressor)
                if action in ("bet", "raise") and street == "preflop" and last_aggressor is None:
                    our_open = True
            elif last_aggressor == me:
                if action == "fold":
                    self._turn(names[seat], "steals" if street == "preflop" else "bluffs", -1)
                elif action == "raise" and street == "preflop" and our_open:
                    self._turn(names[seat], "steals", +1)
                elif action == "call" and street != "preflop":
                    we_bet_into.add(seat)
            if action in ("bet", "raise"):
                last_aggressor = seat
        if not result.showdown:
            return
        for seat in we_called:
            if seat not in folded:
                self._turn(names[seat], "calls", -1 if we_won else +1)
        for seat in we_bet_into:
            if seat not in folded and not we_won:
                self._turn(names[seat], "bluffs", +1)
