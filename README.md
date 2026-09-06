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

## Notes

- BTS data lags ~3 months. Carrier codes: OH=PSA, MQ=Envoy, YX=Republic, PT=Piedmont.
- BTS keeps the departure date for overnight flights; `exposure.py` handles the midnight wrap.
- Blank tail numbers in BTS are treated as "different aircraft." Check `exposure_events.csv` for 2026-04-22 JIA5383 at SDF as a known-positive.
- Tested on synthetic data reproducing the PHX timing; not yet validated against a full BTS month or live feeds.

## Where to report a finding

NASA ASRS (confidential, read by the industry), the airline's corporate safety contact, and the FAA Aviation Safety Hotline. Eurocontrol's Call Sign Similarity Service is the existence proof that schedule-level deconfliction works; the US has no equivalent.
