# How our bot thinks

This is every rule the bot follows, in plain English. The numbers are the current
settings; all of them live in `PARAMS` at the top of [bot.py](bot.py).

Steps 1–7 are the base logic (version 3). **Versions 4 and 5** add what's in the section
"Versions 4 and 5: playing to win, and Mahoraga" at the end.

---

## Step 1: work out our chances (every decision)

1. **Cross off the cards we can see.** We know our 2 cards and the board, so the
   other 52 − (2 + board) cards are what's left in the deck (`deck_odds.py`).
2. **Guess what each opponent holds**, from how they've played *this hand* plus
   what we remember about them:
   - Before the flop: if they **raised**, they're given good starting hands (their
     best ~1.5 × their usual raise rate). If they **called**, decent hands. If they
     only posted a blind, any two cards. If they **went all in**, only their best
     hands. Known bluffers get weaker hands than that.
   - After the flop: a **small bet** means "my cards touch this board" (a pair, a
     draw...). A **big bet (70%+ of the pot) or a raise** means "I hit it hard" (top
     pair or better, a big draw). A **call** means "something, but maybe floating".
     Every betting range keeps some bluffs in it, more for bots we've caught bluffing.
   - A **maniac** (goes all in or raises nearly every hand) is given any two cards.
     That's the failsafe against all-in bots.
3. **Simulate.** Deal the rest of the board and the opponents' hands hundreds of
   times, following those guesses, and count how often we win. That gives our
   **win chance** and the **top three hands** we're likely to finish with.
   - Before the flop with nobody raising, we skip the simulation and look our win
     chance up in a table worked out in advance for all 169 starting hands.
   - On the river heads-up, we check every possible opponent hand exactly.

## Step 2: sort our hand into a tier

**Before the flop** (by how our hand ranks among all starting hands):

| Tier | Hands |
|---|---|
| premium | AA, KK, QQ, JJ, AK |
| pair | any other pair |
| normal | good enough for our position (see "how loose we play") |
| trash | everything else |

How loose we play: we play the top 60% of hands heads-up (`play_floor` 0.40), 2%
tighter per extra opponent, 10% looser on the button or the seat before it, 6%
tighter in the first seats to act.

**After the flop** (by our "edge": how much better our win chance is than an
average hand's at this table):

| Tier | Edge |
|---|---|
| monster | 60%+ |
| strong | 35–60% |
| medium | 10–35% |
| weak | under 10% |

## Step 3: our random limit (your idea)

Each tier has a **range** for the most we'll put into this hand, as a share of the
chips really at risk (the smaller of our stack and the biggest opponent's stack).
Each street we pick a random number inside it, so the same hand doesn't always
play the same way.

| Tier | Limit |
|---|---|
| trash | 0% |
| normal | 5–10% (your starting-bet rule) |
| pair | 10–25% |
| premium | 40–80% (AA and KK: everything) |
| weak | 2–6% |
| medium | 6–18% |
| strong | 18–45% |
| monster | 60–100% |

## Step 4: before the flop

- **Nobody has raised and it's our turn:**
  - Trash: fold, unless we can **steal** (below).
  - Pairs and premium hands: raise. Other playable hands: raise 75% of the time,
    otherwise just call the minimum (your "bet the minimum" idea).
  - Raise size: a random 2.2–3.2 big blinds, plus 1 for each player who just called.
  - In the big blind with everyone just calling: check, or sometimes raise with
    a good hand.
- **Blind steal:** in late position with nobody in yet, raise any two cards when
  the fold model says the blinds will fold often enough to make it pay.
- **Someone has raised:** go to "facing a bet" below. Re-raises are a random
  2.6–3.4 times their bet.
- **Short stack (10 big blinds or less): all in or fold.** We shove when, on
  average, it beats folding: the chance they fold, plus our chance when called
  (we assume whoever calls has a good hand).

## Step 5: after the flop, when nobody has bet

1. **Slow-play:** with a monster on the flop or turn, check 40% of the time to
   trap them.
2. Otherwise, try **five bet sizes** (⅓, ½, ¾, 1 and 1.4 × the pot). For each,
   the **fold model** predicts the chance everyone folds, and we work out what the
   bet is worth on average:
   - they fold → we win the pot;
   - they call → we win or lose the bigger pot. Our chance is a bit lower than
     usual, because the hands that fold are the ones we were beating.
3. Compare each bet with checking, which keeps about 90% of our win chance.
   - If a bet beats checking, pick **randomly among the sizes that are nearly as
     good as the best** (within 75%). We stay unpredictable without giving away chips.
   - If nothing beats checking, check. With a weak hand there's a 2% chance of a
     random bluff anyway (never against calling stations).
4. Bluffs never risk more than 12% of our chips, and no bet goes above our limit.

## Step 6: facing a bet

1. Work out the **price**: the share of the final pot we'd be paying (the pot odds).
   - **Two-street pricing:** on the flop, against a bot that usually bets the turn
     again, we also count that turn bet in the price (thinking one street ahead).
2. **Within our limit:** call if our win chance is at least the price minus 3%
   (4% more leeway with a flush or straight draw). Sometimes raise instead (the
   tier's raise chance: 55% with pairs preflop, 85% premium, 50% strong...). Raises
   are a random amount between the minimum and our limit (your raise idea).
3. **Over our limit:** call only if our chance beats the price by 7%. Against a
   known maniac it's only 2% (the all-in failsafe). Never fold AA or KK before the flop.
4. **Committed:** once 45% of our chips are in the pot, we don't fold to a bet the
   odds justify, even over our limit.
5. Otherwise fold. (Bluff raises are built in but switched off: they tested worse.)

## Step 7: tournament stage

- In the last 15 hands, if we're not the chip leader, all our limits are 1.4×
  bigger. (Version 4 replaces this with proper "play to win" logic.)

## Memory: what we record about each opponent

After every hand, for every opponent (by name, since seats change):

- how often they **play** a hand, **raise** before the flop, **go all in**, and **bet or raise** at all;
- how often they **fold when bet into**, overall and separately on each street;
- after betting the flop, how often they **bet the turn again**;
- when we see their cards after they bet the turn or river, whether it was a **bluff**.

Labels come from those numbers: **maniac**, **calling station** (folds under 15%
of the time), **bluffer**. All the rates start from a sensible guess and move
towards what we actually see, so one odd hand doesn't fool us.

We **never show our cards** when we don't have to (your rule).

## The fold model

A small machine-learning model, trained on about 66,000 moments where a bot faced
a bet. It predicts **the chance a given opponent folds to a given bet**, from:

- the street, the price of calling, and whether calling puts them all in;
- their stack compared with the pot, and how many players are still in;
- whether it's a raise, and whether they were the aggressor;
- their habits (fold rates, aggression, how often they play, how often they shove).

It was right 78% of the time on tournaments it never saw. The most important
things it found: **fewer players in the hand means more folds**, and each bot's own
fold habit multiplied by the price.

Because "doesn't fold" means "calls", the same model is also our **call predictor**:
it tells us how big a bet still gets called. Version 4 adds a **2× pot overbet** to
the sizes we try, so against bots that call almost anything, a monster can win a
much bigger pot.

---

# Versions 4 and 5: playing to win, and Mahoraga

## Why: what the leak report found

- **96% of tournaments reach the 100-hand limit**, and then the chip leader wins.
- Chip leader 15 hands from the end: **won 80%**. Not the leader: **won 22%**.
- 2nd place is worth almost nothing (we're ranked by tournaments won).
- Our biggest leak: **calling turn and river bets with weak hands**, about −2,700
  chips per tournament.

## Play to win (the last 20 hands)

- **Behind the leader:** take more risk, the further behind we are and the fewer
  hands are left. "Urgency" = (how far behind ÷ our chips) × (20 ÷ hands left).
  Our limits grow by 0.3 × urgency, up to 2.5× normal. We call up to 6% lighter.
- **The last 3 hands, behind:** go all in before the flop with any playable hand.
  Finishing 2nd with chips is no better than busting.
- **Leading:** keep playing normally.

Tuned in version 5 (80 tournaments × 3 tables, one change at a time). Chasing
gently beat chasing hard (34% > 30% > 27%). A leader who keeps playing normally
beat one who tightens up (37% > 30% > 27%). Dropping the desperate shoves was
the worst change of all (26%).

## Fixing the leak

- With a **weak hand on the turn or river**, a call over our limit now needs an
  extra 5% (12% in total over the pot odds).

## Learning from shown hands (showdown learning)

Every time an opponent shows their cards, we write down:

- what starting hand they had, and whether they **raised** or just **called** with
  it. After 3+ shown hands, our guess of "their weakest raising hand" moves towards
  what they've really raised with;
- whether their **big turn/river bets** (70%+ of the pot, or raises) were bluffs,
  kept apart from their small bets. Some bots only bluff small, some only bluff big.

## Eight-Handled Sword Divergent Sila Divine General Mahoraga ([mahoraga.py](mahoraga.py))

The adaptation layer (version 5). Every opponent gets four **wheels**, one for each
way they can hurt us. Each wheel has **8 notches each way**. The further it has
turned, the more we've adapted to that habit; at 8 we're fully adapted.

**How a wheel turns: on surprise, not on every event.** Every time the situation
comes up, we compare what we *predicted* with what *really happened*:

- When we bet, the fold model predicted how often they'd fold. Did they fold more or less?
- When we called, our simulation gave us a win chance. Did we win more or less?

The wheel's position is **how wrong our predictions have been against that
player**. That means:

- a bot that behaves exactly as predicted never moves its wheel;
- one lucky hand barely moves it. Each wheel starts with a cushion of "as predicted"
  events (20 for calls, which are coin-flippy; 6–8 for the others);
- older events fade out (about the last 50 count), so if a bot **changes style,
  the wheel turns back**. The Shape-shifter test bot checks this.

**Mahoraga doesn't just defend, it counters.** Once a wheel has turned, we attack
the habit it found:

| Wheel | Turns + when... | Defend (+) | Counter |
|---|---|---|---|
| **calls** | our turn/river calls against them win less than our odds said | need up to 8% more to call their bets | at −: we keep winning, so they bluff us. Call them up to 8% lighter |
| **bluffs** | they fold to our flop+ bets less than predicted | expect them to fold less (at 8: half as often) | +: they call wide, so value-bet thinner (a call doesn't mean we're beaten). −: they fold a lot, so expect more folds and bluff up to twice as big |
| **steals** | they fold to our preflop raises less than predicted | expect fewer folds to our raises | call their re-raises up to 8% lighter |
| **attacks** | they bet when we check to them more than an ordinary bot (~40%) | — | call their bets after our check up to 8% lighter |

The wheels last for the whole batch of tournaments (the engine keeps our bot
alive for up to 20).

**Tested** (100 tournaments × 4 tables, including the adaptive table):

| Version | Wins |
|---|---|
| **version 5 (Mahoraga with counters)** | **38.5%** |
| version 5 with the style wheel | 36.0% |
| version 4 (the old wheels) | 35.0% |
| version 5, no Mahoraga | 32.5% |
| version 5, wheels only defend | 32.0% |

The **counters** are what make Mahoraga pay off. Defending alone did nothing.

**Big confirmation run** (300 tournaments × 4 tables, fresh cards): **a tie.**

| Table | version 4 | version 5 |
|---|---|---|
| builtins | 49.0% | 51.7% |
| sharks | 45.7% | 40.3% |
| hard | 34.3% | 34.0% |
| adaptive | 19.3% | 21.3% |
| **average** | **37.1%** | **36.8%** |

The 3.5-point lead in the smaller test was luck. Version 5 is a little better
against bots that adapt, and worse at the sharks table (lots of equity bots).

**Hybrid test** (mixing v4 and v5 piece by piece, 100 tournaments × 4 tables).
Settings: `wheel_engine` ("calibrated" or "notch") and `counter_wheels` (True,
False, or a list such as ["call_light", "attack", "resteal"]).

| Mix | Wins | adaptive table |
|---|---|---|
| version 5 | 35.5% | 21% |
| v5, calling counters only | 35.5% | 20% |
| v4 notch wheels + counters | 34.2% | 16% |
| v5, betting counters only | 34.0% | 10% |
| version 4 | 34.0% | 15% |
| v5 + v4 endgame | 34.0% | 17% |
| v4 notch wheels, no counters | 32.2% | 10% |

Every average is within luck of the others: no mix is a clear upgrade. The one
pattern that keeps showing up is that **the calling counters are what beat adaptive
bots** (about 20% with them, 10% without). The betting counters add nothing measurable.

**The style wheel** (`style_shift`, off): before each tournament, play the style
with the best record this batch (**standard**, **aggressive** or **tight**), and
switch 25% of the time. It didn't help in testing, so it stays off.

## Knowing the real field before round 2

After round 1, `profile_field.py` reads the published hand logs and saves a profile
of every real bot (`field_priors.json`). Our bot loads it at startup, so each batch
starts already knowing every opponent, counted as at most 40 hands' worth so that
what we see live soon takes over.

It also replays the hands *we* played in those logs and saves Mahoraga's
**attacks** wheel for every opponent, which we load at half strength. The other
wheels need the predictions we made during play, which aren't in the logs, so they
start fresh.

## Heads-up check

The organiser may also run one-on-one matches. `headsup.py` plays 1,000 deals from
both seats against every test bot. Version 5 beat **all 14** (+284 big blinds per
100 hands on average, nothing losing), so heads-up needs no special logic for now.

## Round 3 of ideas: leak report, fight picking, steadier wheels (all switched off)

**Leak report at the sharks table** (v5 vs v4, 160 tournaments). v4 won 45%, v5 41%.
v4 was chip leader 15 hands from the end more often (56 against 47) and busted less
(61 against 70). Two causes were suspected:

- v4's notch wheels drift to "this bot folds a lot" for nearly everyone, so v4
  bets and raises more than v5;
- v5's **calls** wheel is noisy: three identical equity bots ended at −7, +7 and −2.

**Fixes tried** (all are settings in `PARAMS`, all off by default):

| Setting | What it does |
|---|---|
| `pick_fights` | last 20 hands. Leading: careful against anyone with 60%+ of our chips, pushier against small stacks. Behind: go after the leader's pots |
| `wheel_priors` {"calls": 60} | the calls wheel starts with 60 "as predicted" events instead of 20, so it turns more slowly |
| `fold_scale` (1.25, 1.1) | v4's extra aggression on purpose: assume 25% more folds preflop, 10% more after the flop |

**Tested** (120 tournaments × 4 tables, deal by deal against v5):

| Version | Wins | vs v5, same deals |
|---|---|---|
| all three together | 38.1% | +1.5% (± 5.7%) |
| **v5** | **36.7%** | — |
| version 4 | 36.3% | −0.4% (± 5.5%) |
| v4's aggression | 34.6% | −2.1% (± 5.8%); sharks −13% (the opposite of the guess) |
| pick fights | 33.8% | −2.9% (± 5.9%) |
| steadier calls wheel | 32.3% | −4.4% (± 5.6%) |

None is measurably better or worse. Each fix on its own came out slightly behind, so
the "all three" lead is almost certainly luck. **The bot stays as version 5.**

**What we learned about testing:** comparing deal by deal hardly narrows the luck
range (± 5.7% instead of about ± 6%). Once one decision differs, the two bots' games
go different ways. Our tests can only see differences of about 5 points or more.
Anything smaller needs runs about 4× longer, roughly 6 hours per pair.
