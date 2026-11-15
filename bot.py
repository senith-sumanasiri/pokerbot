"""Kenula's poker bot (version 5: Mahoraga with counter wheels).

How it thinks, every time it's our turn:
  1. Cross off the cards we can see and simulate the rest of the hand hundreds of
     times (deck_odds.py) to get our chance of winning and our likely final hands.
     Opponents are dealt hands that match how they've played (opponent_tracker.py).
     Before the flop with nobody raising, it just looks the answer up in a table.
  2. Sort our hand into a tier (trash / normal / pair / premium before the flop,
     weak / medium / strong / monster after it).
  3. Each tier has a RANGE for the most we'll put in this hand, as a share of the
     chips actually at risk (the smaller of our stack and the biggest opponent's).
     We pick a random number inside it, so the same hand doesn't always bet the same.
  4. Facing a bet: only call when our chances pay for the price. Once we've put most
     of our chips in, we don't fold for a small last bet (we're "committed").
  5. Betting: a trained model (fold_model.py) predicts how likely each opponent is to
     fold to each bet size. We work out what each size is worth on average and pick
     randomly among the best ones. That's what finds bluffs that work, stops bluffs
     that don't, and makes calling stations pay.
  6. Steal the blinds from late position when the model says they'll fold, and play
     all-in-or-fold when short, choosing shoves by the same maths.

Every number lives in PARAMS. my_bots/training/bench.py and tune.py test changes.
"""

import random
import traceback

from poker_tournament import Action, PokerBot

from deck_odds import chance_of_at_least, current_category, preflop_equity, preflop_percentile, simulate
from fold_model import FoldModel, features
from mahoraga import STYLES, Mahoraga, NotchMahoraga
from opponent_tracker import OpponentTracker

NAME = None  # leaderboard uses the folder name anyway
STYLE = "Unpredictable"
DESCRIPTION = "Counts the deck, remembers every opponent, and predicts who will fold to which bet."

DEBUG = False  # True prints every decision and its reasoning (to the console with --in-process, else the bot log)

PARAMS = {
    # --- Limits: most we'll put in this hand, as a share of the chips at risk.
    #     A random number between (low, high) is picked each street.
    "cap_trash":   (0.00, 0.00),
    "cap_normal":  (0.05, 0.10),   # your rule: 5-10% of our chips on a starting bet
    "cap_pair":    (0.10, 0.25),   # more leeway with a pair
    "cap_premium": (0.40, 0.80),   # QQ, JJ... AK (AA and KK can go all the way)
    "cap_weak":    (0.02, 0.06),
    "cap_medium":  (0.06, 0.18),
    "cap_strong":  (0.18, 0.45),
    "cap_monster": (0.60, 1.00),

    # --- Once we've decided to continue: chance we raise instead of calling.
    "raise_trash": 0.0, "raise_normal": 0.10, "raise_pair": 0.55, "raise_premium": 0.85,
    "raise_weak": 0.0, "raise_medium": 0.15, "raise_strong": 0.50, "raise_monster": 0.65,

    # --- Betting after the flop when nobody has bet.
    "use_fold_model": True,     # True: choose bets by predicted folds (v3). False: the v2 chance table below.
    "bet_sizes": (0.33, 0.5, 0.75, 1.0, 1.4, 2.0),  # bet sizes to consider, as shares of the pot
                                # (2x pot "overbet": the fold model also tells us how big a bet still gets called)
    "near_best": 0.75,          # pick randomly among sizes worth at least this share of the best one
    "check_realize": 0.9,       # how much of our win chance we keep by checking (we can be bet off it)
    "random_bluff": 0.02,       # small chance to bluff anyway, so we're never fully predictable
    "bet_weak": 0.0, "bet_medium": 0.35, "bet_strong": 0.80, "bet_monster": 0.75,  # v2 table
    "station_bonus": 0.20,

    # --- Before the flop.
    "open_raise": 0.75,         # first in: raise this often, otherwise just call the minimum (your idea)
    "open_size": (2.2, 3.2),    # opening raise in big blinds, +1 big blind per player who just called
    "reraise_size": (2.6, 3.4), # re-raise: this many times the bet we face
    "play_floor": 0.40,         # weakest starting hand we play (percentile) with one opponent...
    "floor_per_opp": 0.02,      # ...stricter for each extra opponent
    "late_bonus": 0.10,         # ...looser on the button or one seat before it
    "early_penalty": 0.06,      # ...stricter when we're one of the first to act
    "steal": True,              # raise any two cards from late position when the blinds will fold enough

    # --- Calling.
    "call_slack": 0.03,         # within our limit: call if win% >= pot odds - this
    "over_cap_margin": 0.07,    # over our limit: need win% >= pot odds + this
    "late_weak_margin": 0.05,   # ...plus this with a weak hand on the turn/river (the leak report's biggest leak)
    "maniac_margin": 0.02,      # ...against a known maniac, much less (the all-in failsafe)
    "draw_slack": 0.04,         # extra leeway with a flush/straight draw
    "commit_frac": 0.45,        # once this share of our chips is in the pot, don't fold to a bet the odds justify
    "two_street": True,         # on the flop, price in the turn bet a habitual double-barreller will make
    "bluff_raise": False,       # raise as a bluff when the model says they'll fold enough
                                # (tested: switching this off scored 0.312 vs 0.253, so it's off)

    # --- Tiers after the flop: our edge over an average hand (0 = average, 1 = certain win).
    "edge_monster": 0.60, "edge_strong": 0.35, "edge_medium": 0.10,

    # --- Bluffing (v2 table, used when use_fold_model is False).
    "bluff": 0.04, "semi_bluff": 0.25, "bluff_max_stack": 0.12, "bluff_size": (0.5, 0.9),

    # --- Disguise.
    "slowplay": 0.40,           # with a monster on the flop/turn, check or just call this often
    "value_size": (0.33, 1.0),

    # --- Tournament stages.
    "short_stack_bb": 10,       # at or below this many big blinds: all in or fold
    # Play to WIN: only 1st place counts, and almost every tournament reaches the
    # 100-hand limit, where the chip leader wins. So near the end:
    "late_hands": 20,           # the last this-many hands are "the endgame"
    "play_to_win": True,        # False = version 3's simple 1.4x boost
    "late_boost": 1.4,          # (version 3's boost, used when play_to_win is False)
    "chase_k": 0.3,             # behind: limits grow with how far behind we are and how few hands are left...
                                # (tuned: 0.3 won 34%, 0.6 30%, 1.2 27%: chase gently)
    "chase_max": 1.5,           # ...up to this much bigger (2.5x)
    "chase_margin": 0.06,       # ...and we call up to this much lighter (2nd place is worth nothing)
    "desperate_hands": 3,       # in the last hands, if behind, shove any playable hand preflop
    "lead_tighten": 1.0,        # leading: limits this much smaller...
    "lead_margin": 0.0,         # ...and we need this much more to call (protect the lead)
                                # (tuned: not tightening won 37%, x0.8 30%, x0.65 27%: keep playing normally)
    # Pick our fights by chip standing (in the endgame). Chips lost to the player in
    # 2nd count twice (they gain what we lose), and so do chips won from the leader.
    "pick_fights": False,       # (tested: no measurable gain, -2.9% +/- 5.9%: off)
    "rival_ratio": 0.6,         # leading: an opponent with 60%+ of our chips is a "rival"
    "rival_tighten": 0.8,       # leading, rival in the hand: limits this much smaller...
    "rival_margin": 0.04,       # ...and need this much more to call
    "bully_boost": 1.2,         # leading, only small stacks in the hand: limits this much bigger
    "hunt_boost": 1.2,          # behind, the leader is in the hand: limits this much bigger...
    "hunt_margin": 0.03,        # ...and call this much lighter

    # --- Mahoraga (mahoraga.py): adapt to each opponent, one wheel turn at a time.
    "mahoraga": True,
    "counter_wheels": True,     # once a wheel has turned, counter the habit it found (not just defend):
                                # True (all), False, or a list from mahoraga.COUNTERS
    "wheel_engine": "calibrated",  # "calibrated" (v5: predicted vs actual) or "notch" (v4: one notch per event)
    "wheel_priors": {},         # bigger cushion = slower, steadier wheel, e.g. {"calls": 60}
    "fold_scale": (1.0, 1.0),   # (preflop, after the flop): multiply every predicted fold chance.
                                # Version 4's wheels drifted to about (1.25, 1.1), and that
                                # extra aggression won chips at the sharks table.
    "style_shift": False,       # the style wheel: change play style between tournaments
    "cap_scale": 1.0,           # (set by styles) all limits times this
}


class Bot(PokerBot):
    def __init__(self, params=None):
        self.base = {**PARAMS, **(params or {})}  # the tuning script passes changed settings here
        self.p = dict(self.base)
        self.rng = random.Random()
        self.tracker = OpponentTracker()
        self.fold_model = FoldModel()
        engine = NotchMahoraga if self.base["wheel_engine"] == "notch" else Mahoraga
        self.mahoraga = engine(counters=self.base["counter_wheels"], priors=self.base["wheel_priors"])
        self.mahoraga.load({name: prior.get("wheels", {}) for name, prior in self.tracker.priors.items()})
        self.caps = {}          # the random limit we picked for each (hand, street)
        self.hand_no = 0
        self.num_hands = None
        self.is_tournament = True
        self.margin = 0.0       # extra win chance needed to call (from the endgame)
        self.last_result = None
        self.notes = {"bets": {}, "calls": {}}  # our predictions this hand, for Mahoraga
        self.last_equity = 0.0

    def on_match_start(self, info):
        if self.last_result is not None and self.is_tournament:
            self.mahoraga.end_tournament(self._won(self.last_result))
        self.hand_no = 0
        self.num_hands = info.num_hands
        self.is_tournament = info.format == "tournament"
        if self.base["style_shift"] and self.is_tournament:
            self.p = {**self.base, **STYLES[self.mahoraga.choose_style(self.rng)]}
        # self.tracker and self.mahoraga are NOT reset: what we learned carries into the next tournament.

    def on_hand_end(self, result):
        self.tracker.record_hand(result)
        self.mahoraga.learn(result, self.notes)
        self.notes = {"bets": {}, "calls": {}}
        self.last_result = result
        self.hand_no += 1
        self.caps.clear()

    @staticmethod
    def _won(result) -> float:
        """Did the last hand of a tournament leave us 1st? (1 = yes, split for ties)"""
        ends = [p.end_stack for p in result.players]
        mine = result.players[result.seat].end_stack if result.seat < len(result.players) else 0
        if mine <= 0 or mine < max(ends):
            return 0.0
        return 1.0 / ends.count(mine)

    def show_cards(self, result):
        return False  # never show

    def act(self, obs):
        try:
            action = self.decide(obs)
            self.take_notes(obs, action)
            return action
        except Exception:
            traceback.print_exc()  # goes to our log; play safe instead of crashing
            return Action.check() if obs.to_call == 0 else Action.fold()

    # =====================================================================================
    def decide(self, obs):
        p = self.p
        opponents = obs.active_opponents
        c1, c2 = obs.hole_card_ints

        # 1. The maths ------------------------------------------------------------------
        specs = [self.tracker.range_spec(o.name, o.seat, obs.history) for o in opponents]
        if obs.street == "preflop" and all(s[0] == 0 and s[1] == 0 for s in specs) and preflop_equity(c1, c2, 1):
            # Nobody has shown strength: look it up instead of simulating.
            sim = {"win": preflop_equity(c1, c2, len(opponents)), "hand_odds": {}, "top_three": []}
        else:
            iterations = max(200, 1200 // max(1, len(opponents)))
            sim = simulate(obs.hole_cards, obs.board, specs, iterations, self.rng)
        equity = sim["win"]
        self.last_equity = equity
        stats = [self.tracker.get(o.name) for o in opponents]

        maniac_betting = any(
            s.is_maniac and o.bet > 0 and o.bet >= obs.current_bet for s, o in zip(stats, opponents)
        )
        aggression, self.margin, desperate = self.endgame(obs)
        aggression *= p["cap_scale"]
        draw = self.has_draw(obs, sim)

        if desperate and obs.street == "preflop" and self.preflop_tier(obs, len(opponents)) != "trash":
            tier = "desperate"
            action, why = (Action.all_in(), "ENDGAME: behind with hands running out, all in") if obs.can_raise \
                else (Action.call(), "ENDGAME: behind, call")
        elif obs.street == "preflop" and self.effective_stack(obs) <= p["short_stack_bb"] * obs.big_blind:
            tier = "short"
            action, why = self.push_or_fold(obs, equity, specs)
        elif obs.street == "preflop":
            tier = self.preflop_tier(obs, len(opponents))
            action, why = self.preflop(obs, tier, equity, aggression, maniac_betting)
        else:
            tier = self.postflop_tier(equity, len(opponents))
            action, why = self.postflop(obs, tier, equity, sim, stats, aggression, maniac_betting, draw)

        self.last_why = (obs.street, tier, why)  # the leak report (training/leaks.py) reads this
        if DEBUG:
            top = ", ".join(f"{name} {chance:.0%}" for name, chance in sim["top_three"])
            print(f"[hand {obs.hand_id} {obs.street}] {obs.hole_cards} {obs.board} win {equity:.0%} "
                  f"| likely: {top} | tier {tier} | {why}")
        return action

    # --- Before the flop ------------------------------------------------------------------
    def preflop(self, obs, tier, equity, aggression, maniac_betting):
        p = self.p
        cap_total, cap_to = self.limits(obs, tier, aggression)
        raised = any(e["action"] in ("raise", "bet") for e in obs.history)
        limpers = sum(1 for e in obs.history if e["action"] == "call")
        raise_chance = p["raise_" + tier]

        if not raised:
            if obs.to_call == 0:  # we're the big blind and everyone just called
                if self.can_raise_within(obs, cap_to) and self.rng.random() < raise_chance:
                    amount = self.open_size(obs, cap_to, limpers)
                    return Action.raise_to(amount), f"raise the limpers to {amount}"
                return Action.check(), "check"
            if tier == "trash":
                steal = self.try_steal(obs, equity, limpers)
                if steal:
                    return Action.raise_to(steal[0]), f"STEAL to {steal[0]} (all fold {steal[1]:.0%})"
                return Action.fold(), "trash hand, fold"
            if self.can_raise_within(obs, cap_to) and (tier in ("pair", "premium") or self.rng.random() < p["open_raise"]):
                amount = self.open_size(obs, cap_to, limpers)
                return Action.raise_to(amount), f"open raise to {amount}"
            return Action.call(), "call the minimum"

        return self.facing_bet(obs, tier, equity, cap_total, cap_to, maniac_betting, draw=False,
                               raise_chance=raise_chance, size=lambda: self.reraise_size(obs, cap_to))

    def try_steal(self, obs, equity, limpers):
        """Late position, nobody raised: if the players left are likely enough to fold,
        raise with any two cards. Returns (amount, chance everyone folds) or None."""
        if not self.p["steal"] or limpers or not obs.can_raise or not self.in_late_position(obs):
            return None
        amount = max(obs.min_raise_to, min(int(self.rng.uniform(*self.p["open_size"]) * obs.big_blind), obs.max_raise_to))
        fold = self.fold_chance(obs, amount)
        if self.bet_value(obs, amount, equity, fold) > 0:
            return amount, fold
        return None

    # --- After the flop ---------------------------------------------------------------------
    def postflop(self, obs, tier, equity, sim, stats, aggression, maniac_betting, draw):
        p = self.p
        cap_total, cap_to = self.limits(obs, tier, aggression)
        slow = tier == "monster" and obs.street != "river" and self.rng.random() < p["slowplay"]

        if obs.to_call > 0:
            return self.facing_bet(obs, tier, equity, cap_total, cap_to, maniac_betting, draw,
                                   raise_chance=0.0 if slow else p["raise_" + tier],
                                   size=lambda: self.random_raise(obs, cap_to))
        if slow:
            return Action.check(), "slow-play check"
        if p["use_fold_model"]:
            return self.model_bet(obs, tier, equity, stats, cap_to)
        return self.table_bet(obs, tier, sim, stats, cap_to, aggression)

    def model_bet(self, obs, tier, equity, stats, cap_to):
        """Try every bet size: what is each worth on average, given how likely they are
        to fold? Bet if one beats checking, choosing randomly among the good ones."""
        p = self.p
        if not obs.can_raise:
            return Action.check(), "check"
        check_value = equity * obs.pot * p["check_realize"]
        # Mahoraga counters: against players who call us wide, a call doesn't mean we're
        # beaten, so value bets keep more of our chance. Against players who fold to us a
        # lot, bluffs may risk up to twice as much.
        wide, folder = 0.0, 0.0
        if p["mahoraga"] and obs.active_opponents:
            wide = min(self.mahoraga.calls_wide(o.name) for o in obs.active_opponents)
            folder = min(self.mahoraga.folds_a_lot(o.name) for o in obs.active_opponents)
        bluff_cap = int(p["bluff_max_stack"] * (1 + folder) * obs.me.start_stack)
        options = []
        for fraction in p["bet_sizes"]:
            amount = self._legal(obs, obs.raise_to_pot_fraction(fraction), cap_to)
            if tier == "weak":  # bluffs never risk more than bluff_max_stack of our chips
                amount = min(amount, max(obs.min_raise_to, bluff_cap))
            if amount < obs.min_raise_to or any(a == amount for _, a, _ in options):
                continue
            fold = self.fold_chance(obs, amount)
            options.append((self.bet_value(obs, amount, equity, fold, keep=0.5 + 0.4 * wide), amount, fold))
        good = [o for o in options if o[0] > check_value]
        if not good:
            if tier == "weak" and options and self.rng.random() < p["random_bluff"] \
                    and not any(s.is_calling_station for s in stats):
                value, amount, fold = self.rng.choice(options)
                return Action.raise_to(amount), f"random BLUFF {amount}"
            return Action.check(), f"check (no bet beats checking, worth {check_value:.0f})"
        best = max(o[0] for o in good)
        shortlist = [o for o in good if o[0] - check_value >= p["near_best"] * (best - check_value)]
        value, amount, fold = self.rng.choice(shortlist)
        kind = "BLUFF" if tier == "weak" else "bet"
        return Action.raise_to(amount), f"{kind} {amount} (they all fold {fold:.0%}, worth {value:.0f} vs check {check_value:.0f})"

    def table_bet(self, obs, tier, sim, stats, cap_to, aggression):
        """Version 2's way: a fixed chance to bet per tier, plus occasional bluffs."""
        p = self.p
        bet_chance = p["bet_" + tier]
        if tier == "medium" and stats and all(s.is_calling_station for s in stats):
            bet_chance += p["station_bonus"]
        if self.can_raise_within(obs, cap_to) and self.rng.random() < bet_chance:
            amount = self._legal(obs, obs.raise_to_pot_fraction(self.rng.uniform(*p["value_size"])), cap_to)
            return Action.raise_to(amount), f"value bet {amount}"
        bluff = self.table_bluff(obs, tier, sim, stats, aggression)
        if bluff:
            return Action.raise_to(bluff), f"BLUFF {bluff}"
        return Action.check(), "check"

    # --- Facing a bet: call, raise or fold? ---------------------------------------------
    def facing_bet(self, obs, tier, equity, cap_total, cap_to, maniac_betting, draw, raise_chance, size):
        p = self.p
        needed = self.price(obs, tier) + self.margin
        bettor = max(obs.active_opponents, key=lambda o: o.bet, default=None)
        if p["mahoraga"] and bettor is not None:
            needed += self.mahoraga.call_margin(bettor.name)  # this player's bets have beaten our calls before
            mine = [e for e in obs.history if e["street"] == obs.street and e["seat"] == obs.seat]
            if obs.street != "preflop" and any(e["action"] == "check" for e in mine):
                needed -= self.mahoraga.attack_discount(bettor.name)   # they bet whenever we check
            if obs.street == "preflop" and any(e["action"] == "raise" for e in mine):
                needed -= self.mahoraga.resteal_discount(bettor.name)  # they re-raise us light
        total_if_call = obs.me.contributed + obs.to_call
        draw_slack = p["draw_slack"] if draw else 0.0
        committed = obs.me.contributed >= p["commit_frac"] * (obs.me.contributed + obs.me.stack)

        if total_if_call <= cap_total or committed:
            if equity + p["call_slack"] + draw_slack >= needed:
                if self.can_raise_within(obs, cap_to) and self.rng.random() < raise_chance:
                    amount = size()
                    return Action.raise_to(amount), f"raise to {amount}"
                why = "committed, call" if committed and total_if_call > cap_total else "within limit, call"
                return Action.call(), f"{why} ({equity:.0%} vs {needed:.0%} needed)"
        else:
            margin = p["maniac_margin"] if maniac_betting else p["over_cap_margin"]
            if tier == "weak" and obs.street in ("turn", "river"):
                margin += p["late_weak_margin"]
            if equity >= needed + margin - draw_slack:
                return Action.call(), f"over limit but odds pay ({equity:.0%} vs {needed:.0%} needed)"
            if obs.street == "preflop" and self.is_aces_or_kings(obs):
                return Action.call(), "pocket aces/kings: never fold preflop"

        # About to fold. Would a bluff raise make them fold often enough to be worth it?
        bluff = self.try_bluff_raise(obs, equity)
        if bluff:
            return Action.raise_to(bluff[0]), f"BLUFF RAISE to {bluff[0]} (they fold {bluff[1]:.0%})"
        return Action.fold(), f"fold ({equity:.0%} vs {needed:.0%} needed)"

    def price(self, obs, tier):
        """The win chance we need to call. Normally the pot odds. On the flop with a
        hand that isn't strong, against a player who usually bets the turn again, we
        also pay for that turn bet, so the price is higher (thinking one street ahead)."""
        needed = obs.pot_odds
        if not self.p["two_street"] or obs.street != "flop" or tier in ("strong", "monster"):
            return needed
        bettor = max(obs.active_opponents, key=lambda o: o.bet, default=None)
        if bettor is None or bettor.all_in:
            return needed
        barrel = self.tracker.get(bettor.name).barrel_rate
        turn_bet = barrel * 0.66 * (obs.pot + obs.to_call)
        turn_bet = min(turn_bet, obs.me.stack - obs.to_call, bettor.stack)
        if turn_bet <= 0:
            return needed
        two_street = (obs.to_call + turn_bet) / (obs.pot + obs.to_call + 2 * turn_bet)
        return max(needed, two_street)

    def try_bluff_raise(self, obs, equity):
        if not self.p["bluff_raise"] or not obs.can_raise or obs.street == "preflop":
            return None
        amount = self._legal(obs, obs.current_bet * self.rng.uniform(2.5, 3.0) + obs.pot * 0.2, obs.max_raise_to)
        if amount - obs.bet > self.p["bluff_max_stack"] * obs.me.start_stack:
            return None
        fold = self.fold_chance(obs, amount)
        return (amount, fold) if self.bet_value(obs, amount, equity, fold) > 0 else None

    # --- Fold predictions and what a bet is worth -------------------------------------------
    def fold_each(self, obs, raise_to, adapted=True):
        """{opponent name: chance they fold} if we bet or raise to raise_to.
        adapted=False gives the fold model's own guess, without Mahoraga's wheels."""
        si = obs.street_index
        pot_after = obs.pot + (raise_to - obs.bet)
        raises = sum(1 for e in obs.history if e["street"] == obs.street and e["action"] in ("bet", "raise"))
        players_in = len(obs.active_opponents) + 1
        out = {}
        for o in obs.active_opponents:
            if o.all_in:
                out[o.name] = 0.0  # already all in: they can't fold
                continue
            call = min(raise_to - o.bet, o.stack)
            if call <= 0:
                continue
            was_aggressor = any(e["seat"] == o.seat and e["action"] in ("bet", "raise") for e in obs.history)
            s = self.tracker.get(o.name)
            x = features(si, call, pot_after, o.stack, players_in, raises + 1 >= 2, was_aggressor, s)
            fold = self.fold_model.predict(x, s, si)
            if adapted and self.p["mahoraga"]:
                fold = min(0.97, fold * self.mahoraga.fold_factor(o.name, preflop=si == 0)
                           * self.p["fold_scale"][0 if si == 0 else 1])
            out[o.name] = fold
        return out

    def fold_chance(self, obs, raise_to):
        """Chance that everyone still in folds if we bet or raise to raise_to."""
        everyone = 1.0
        for fold in self.fold_each(obs, raise_to).values():
            everyone *= fold
        return everyone

    def take_notes(self, obs, action):
        """Write down what we predicted, so Mahoraga can measure its surprise after the hand."""
        if action.type in ("raise", "all_in"):
            amount = obs.max_raise_to if action.type == "all_in" else action.amount
            self.notes["bets"][obs.street] = self.fold_each(obs, amount, adapted=False)
        elif action.type == "call" and obs.to_call > 0 and obs.street in ("turn", "river"):
            bettor = max(obs.active_opponents, key=lambda o: o.bet, default=None)
            if bettor is not None:
                self.notes["calls"][obs.street] = (bettor.name, self.last_equity)

    @staticmethod
    def bet_value(obs, raise_to, equity, fold, keep=0.5):
        """Average chips we end up with from the pot by betting (relative to folding now).
        If they all fold we win the pot. If someone calls, the hands that folded were the
        ones we beat, so our chance against the caller is lower than our overall chance:
        we assume halfway between our overall chance and that worst case (keep=0.5;
        Mahoraga raises it against players who call us with worse hands)."""
        added = raise_to - obs.bet
        if fold >= 0.999:
            return obs.pot
        worst = max(0.0, (equity - fold) / (1 - fold))
        when_called = keep * equity + (1 - keep) * worst
        called_pot = obs.pot + 2 * added - obs.to_call
        return fold * obs.pot + (1 - fold) * (when_called * called_pot - added)

    # --- Version 2 bluffing (used when use_fold_model is False) --------------------------
    def table_bluff(self, obs, tier, sim, stats, aggression):
        p = self.p
        if tier not in ("weak", "medium") or not obs.can_raise or not stats:
            return None
        if any(s.is_calling_station for s in stats):
            return None
        chance = p["semi_bluff"] if self.has_draw(obs, sim) else p["bluff"]
        chance *= sum(s.fold_to_bet for s in stats) / len(stats) / 0.4
        chance /= len(stats)
        if self.in_late_position(obs):
            chance *= 1.3
        chance *= aggression
        if self.rng.random() >= chance:
            return None
        size = obs.raise_to_pot_fraction(self.rng.uniform(*p["bluff_size"]))
        most = obs.bet + int(p["bluff_max_stack"] * obs.me.start_stack)
        return max(obs.min_raise_to, min(size, most, obs.max_raise_to))

    def has_draw(self, obs, sim):
        """A real chance of making a straight or better that we haven't made yet."""
        if obs.street not in ("flop", "turn") or not sim["hand_odds"]:
            return False
        made = chance_of_at_least({current_category(obs.hole_cards, obs.board): 1.0}, "Straight")
        threshold = 0.25 if obs.street == "flop" else 0.15
        return not made and chance_of_at_least(sim["hand_odds"], "Straight") >= threshold

    # --- Tiers --------------------------------------------------------------------------
    def preflop_tier(self, obs, num_opponents):
        c1, c2 = obs.hole_card_ints
        r1, r2 = c1 >> 2, c2 >> 2
        if (r1 == r2 and r1 >= 9) or {r1, r2} == {12, 11}:   # JJ, QQ, KK, AA or AK
            return "premium"
        if r1 == r2:
            return "pair"
        if preflop_percentile(c1, c2) >= self.play_floor(obs, num_opponents):
            return "normal"
        return "trash"

    def play_floor(self, obs, num_opponents):
        p = self.p
        floor = p["play_floor"] + p["floor_per_opp"] * (num_opponents - 1)
        if self.in_late_position(obs):
            floor -= p["late_bonus"]
        elif self.in_early_position(obs):
            floor += p["early_penalty"]
        return max(0.15, min(0.85, floor))

    def postflop_tier(self, equity, num_opponents):
        p = self.p
        fair = 1 / (num_opponents + 1)
        edge = (equity - fair) / (1 - fair)
        if edge >= p["edge_monster"]:
            return "monster"
        if edge >= p["edge_strong"]:
            return "strong"
        if edge >= p["edge_medium"]:
            return "medium"
        return "weak"

    # --- Position -----------------------------------------------------------------------
    @staticmethod
    def in_late_position(obs):
        offset = (obs.seat - obs.button) % obs.num_players
        return offset == 0 or (obs.num_players >= 4 and offset == obs.num_players - 1)

    @staticmethod
    def in_early_position(obs):
        offset = (obs.seat - obs.button) % obs.num_players
        return obs.num_players >= 6 and offset in (3, 4)

    # --- Limits and bet sizes -------------------------------------------------------------
    @staticmethod
    def effective_stack(obs):
        """The chips really at risk: we can't lose more than the biggest opponent still in has."""
        biggest = max((o.start_stack for o in obs.active_opponents), default=obs.me.start_stack)
        return min(obs.me.start_stack, biggest)

    def limits(self, obs, tier, aggression):
        """Returns (cap_total, cap_to): the most chips we'll have in the pot this hand,
        and the same limit as a 'raise to' amount on this street."""
        key = (obs.hand_id, obs.street)
        if key not in self.caps:
            low, high = self.p["cap_" + tier]
            fraction = self.rng.uniform(low, high) * aggression
            if obs.street == "preflop" and self.is_aces_or_kings(obs):
                fraction = 1.0
            stake = self.effective_stack(obs)
            cap = int(fraction * stake)
            if obs.street == "preflop" and tier != "trash":
                cap = max(cap, obs.big_blind)
            self.caps[key] = min(cap, stake)
        cap_total = self.caps[key]
        cap_to = cap_total - (obs.me.contributed - obs.bet)
        return cap_total, cap_to

    @staticmethod
    def can_raise_within(obs, cap_to):
        return obs.can_raise and obs.min_raise_to <= cap_to

    @staticmethod
    def _legal(obs, amount, cap_to):
        return max(obs.min_raise_to, min(int(amount), cap_to, obs.max_raise_to))

    def open_size(self, obs, cap_to, limpers):
        bbs = self.rng.uniform(*self.p["open_size"]) + limpers
        return self._legal(obs, bbs * obs.big_blind, cap_to)

    def reraise_size(self, obs, cap_to):
        return self._legal(obs, obs.current_bet * self.rng.uniform(*self.p["reraise_size"]), cap_to)

    def random_raise(self, obs, cap_to):
        """Your idea: a random raise between the minimum allowed and our limit
        (kept to about a pot-sized raise at most)."""
        top = min(cap_to, obs.raise_to_pot_fraction(1.2), obs.max_raise_to)
        return self.rng.randint(obs.min_raise_to, max(obs.min_raise_to, top))

    # --- Short stack: all in or fold ----------------------------------------------------
    def push_or_fold(self, obs, equity, specs):
        """Shove when its average value beats folding: they fold often enough, or we
        have enough of a chance when someone calls (callers are dealt strong hands)."""
        nobody_raised = obs.current_bet <= obs.big_blind
        if not nobody_raised:
            if equity > obs.pot_odds + 0.02:
                return (Action.all_in() if obs.can_raise else Action.call()), "short: call/shove, odds pay"
            return Action.fold(), "short: fold to the raise"
        if not obs.can_raise:
            return (Action.check() if obs.to_call == 0 else Action.fold()), "short: fold"
        shove = obs.max_raise_to
        fold = self.fold_chance(obs, shove)
        callers = [(max(s[0], 0.6), 0, 1.0) for s in specs]  # whoever calls an all in has a good hand
        vs_callers = simulate(obs.hole_cards, obs.board, callers[:1], 300, self.rng)["win"]
        added = shove - obs.bet
        value = fold * obs.pot + (1 - fold) * (vs_callers * (obs.pot + 2 * added - obs.to_call) - added)
        if value > 0:
            return Action.all_in(), f"short: all in (they fold {fold:.0%}, {vs_callers:.0%} if called)"
        return (Action.check() if obs.to_call == 0 else Action.fold()), "short: fold"

    # --- Tournament stage -----------------------------------------------------------------
    def endgame(self, obs):
        """Play to win. Returns (limit multiplier, extra win chance needed to call, desperate?).

        At the hand limit the chip leader wins and 2nd place is worth almost nothing.
        So in the last hands: if we're behind, take more risk the further behind we are
        and the fewer hands are left. If we're leading, tighten up and protect the lead."""
        p = self.p
        if not self.is_tournament or not self.num_hands:
            return 1.0, 0.0, False
        left = self.num_hands - self.hand_no  # hands left, including this one
        if left > p["late_hands"]:
            return 1.0, 0.0, False
        mine = obs.me.stack + obs.me.contributed
        leader = max((pl.stack + pl.contributed for pl in obs.players if pl.seat != obs.seat), default=0)
        if not p["play_to_win"]:
            return (1.0 if mine >= leader else p["late_boost"]), 0.0, False
        if mine >= leader:
            boost, margin, desperate = p["lead_tighten"], p["lead_margin"], False
        else:
            # How far behind, compared with how much time is left to catch up.
            urgency = (leader - mine) / max(1, mine) * p["late_hands"] / left
            boost = 1 + min(p["chase_max"], p["chase_k"] * urgency)
            margin = -min(p["chase_margin"], 0.02 * urgency)
            desperate = left <= p["desperate_hands"]
        if p["pick_fights"]:
            boost, margin = self.pick_fights(obs, mine, leader, boost, margin)
        return boost, margin, desperate

    def pick_fights(self, obs, mine, leader, boost, margin):
        """Who is in this hand matters. Leading: careful against the player in 2nd (every
        chip they win from us closes the gap twice), pushier against small stacks.
        Behind: go after the leader's pots (every chip won from them counts twice)."""
        p = self.p
        if obs.street == "preflop":
            # before the flop only players who've raised are "in" the fight yet
            involved = [o for o in obs.active_opponents if o.bet > obs.big_blind]
        else:
            involved = list(obs.active_opponents)
        if not involved:
            return boost, margin
        chips = {o.seat: o.stack + o.contributed for o in involved}
        if mine >= leader:
            if any(c >= p["rival_ratio"] * mine for c in chips.values()):
                return boost * p["rival_tighten"], margin + p["rival_margin"]
            return boost * p["bully_boost"], margin
        if max(chips.values()) >= leader:  # the leader is in this hand
            return boost * p["hunt_boost"], margin - p["hunt_margin"]
        return boost, margin

    @staticmethod
    def is_aces_or_kings(obs):
        c1, c2 = obs.hole_card_ints
        return c1 >> 2 == c2 >> 2 and c1 >> 2 >= 11
