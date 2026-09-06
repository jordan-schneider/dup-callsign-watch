# dup-callsign-watch

Detects two aircraft operating under the same ATC callsign at the same time, using only public data.

Motivated by four American Airlines / PSA events in 2026 (JIA5383 at SDF on Apr 22, AAL1275 at LAS on Apr 25, JIA5083 at PVD on Aug 12, AAL2482 at PHX on Aug 14), all with the same mechanism: a flight number reused on both legs of an out-and-back, an aircraft swap, and a delay on the first leg.

## Three lead times

| Script | Data | Lead time | Cost |
|---|---|---|---|
| `exposure.py` | BTS On-Time Performance | days to months (exposure ranking + historical events) | free |
| `watch.py predict` | FlightAware AeroAPI | ~60-120 min | AeroAPI Personal tier |
| `watch.py live` | adsb.lol (community ADS-B) | ~10-20 min | free, no key |

## Quick start

```bash
pip install -r requirements.txt

# 1. Historical exposure: which out-and-back pairs are at risk, and which days already breached
python exposure.py --download --start 2025-09 --end 2026-05 --carriers AA OH MQ YX PT
#    -> exposure_pairs.csv, exposure_events.csv

# 2. Live safety net around an airport (Phoenix shown)
python watch.py live --lat 33.43 --lon -112.01 --radius 200 --airline AAL JIA ENY RPA PDT

# 3. Hour-ahead prediction for the riskiest pairs
export AEROAPI_KEY=...
python watch.py predict --pairs exposure_pairs.csv --top 200
```

Set `ALERT_WEBHOOK` to a Slack/Discord/ntfy URL to get pushed alerts.

## Validating the exposure list against real callsigns

`exposure.py` proves a *flight number* was airborne twice; it cannot tell you whether ATC
ever heard two identical callsigns, because a stubbed leg looks identical in BTS.
`validate_callsigns.py` closes that gap using the Flightradar24 API, which returns the
marketing number and the ATC callsign as separate fields:

```bash
export FR24_TOKEN=...
python validate_callsigns.py --estimate            # query plan, no API calls, no cost
python validate_callsigns.py --limit-events 40     # cheap first pass on the riskiest rows
python validate_callsigns.py --max-queries 250     # full run: ~213 calls for ~430 events
```

Each event is classified `duplicate_reached_atc` (both legs broadcast the same callsign),
`stubbed` (they differ, so the deconfliction worked), or `not_found`. Responses are cached,
so re-runs cost nothing.

It also recomputes the airborne overlap from FR24's `datetime_takeoff`/`datetime_landed`,
which are UTC. That is an absolute timeline with no local-midnight anchor and no timezone
handling, so it independently checks the arithmetic in `exposure.py`.

**Data source note.** Historical broadcast callsigns are the expensive part of this
problem. OpenSky is free but its REST API only reaches 30 days back, and the full
historical database is restricted to academic, government and aviation-authority users.
FlightAware AeroAPI gates historical data behind its Standard tier. ADS-B Exchange sells
historical backfills to subscription customers only, with annual minimums. The
Flightradar24 Essential tier ($90/mo, 333k credits, two years of history) is the cheapest
route to this specific question; it is a recurring subscription, so cancel it when the
backtest is done.

## Notes

- BTS data lags ~3 months. Carrier codes: OH=PSA, MQ=Envoy, YX=Republic, PT=Piedmont.
- BTS keeps the departure date for overnight flights; `exposure.py` handles the midnight wrap.
- Blank tail numbers in BTS are treated as "different aircraft." Check `exposure_events.csv` for 2026-04-22 JIA5383 at SDF as a known-positive.
- `exposure.py` scores on *airborne* overlap (outbound wheels-off to inbound wheels-on, via `TaxiOut`/`TaxiIn`), since BTS `DepTime`/`ArrTime` are gate times and a gate overlap shorter than the combined taxi never put two aircraft up together. `overlap_gate_min` keeps the looser gate reading.

### What the exposure numbers mean

BTS records the operating **flight number**, not the **ATC callsign** that was filed.
When dispatch sees a pending duplicate it normally *stubs* one leg — files a distinct
callsign — so controllers never hear two identical ones. `exposure.py` therefore counts
the *opportunity* for a duplicate callsign; the publicised events are the subset where
stubbing did not happen. Do not call a row a confirmed duplicate-callsign event without
independent evidence (ATC audio, or ADS-B showing both aircraft broadcasting the same
ident). `watch.py live` reads broadcast callsigns and has no such limitation.

Backtest over 2025-09..2026-06 for AA/OH/MQ/YX/PT: 1,529,610 flights, 307,544
out-and-back pair-days, **416** with an airborne overlap and different tails (729 on the
looser gate test). Median overlap 36 min; 134 of the 416 overlap by <=20 min, meaning both
aircraft were still inside the turn airport's terminal area — the geometry of the PHX and
PVD events. Both known-positives inside BTS coverage are detected: JIA5383/SDF 2026-04-22
(11 min airborne) and AAL1275/LAS 2026-04-25 (4 min).

For calibration, the reported AA5083/PVD event on 2026-08-12 was ~26 minutes of shared
airborne time, which sits in the modal bucket of this distribution rather than at its tail.

## Where to report a finding

NASA ASRS (confidential, read by the industry), the airline's corporate safety contact, and the FAA Aviation Safety Hotline. Eurocontrol's Call Sign Similarity Service is the existence proof that schedule-level deconfliction works; the US has no equivalent.
