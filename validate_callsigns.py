#!/usr/bin/env python3
"""
Check exposure_events.csv against the callsigns that were actually broadcast.

exposure.py can only prove that a *flight number* was airborne twice. It cannot say
whether ATC ever received two identical callsigns, because airlines routinely "stub" one
leg -- filing a distinct callsign -- when dispatch spots a pending duplicate. The
publicised 2026 events are the cases where stubbing did not happen.

The Flightradar24 API separates the two things exposure.py cannot:

    flight     the marketing number, e.g. "AA5083"
    callsign   what ATC actually addressed, e.g. "JIA5083" -- or the stub

So for each event we look up both tail numbers on that date and compare the two legs'
callsigns:

    SAME callsign      -> a genuine duplicate reached ATC
    DIFFERENT callsign -> the leg was stubbed and the system worked as designed
    NOT FOUND          -> no coverage; counts as unknown, never as either outcome

FR24 also returns datetime_takeoff / datetime_landed in UTC. Those give an airborne
overlap on an absolute timeline, independent of the local-midnight arithmetic in
exposure.py, so this doubles as an external check on that math.

Cost control, because this spends real money:
  - every response is cached under --cache, so a re-run costs nothing
  - --max-queries caps spend; the default is deliberately small
  - --estimate performs no API calls at all and just prints the query plan
Registrations are batched 15 per call (the API maximum), so a full run of ~430 events is
roughly 200-250 calls.

Setup:
    export FR24_TOKEN=...        # Flightradar24 API subscription token
    python validate_callsigns.py --estimate
    python validate_callsigns.py --max-queries 250
"""
import argparse, json, os, sys, time, hashlib
import datetime as dt
import urllib.request, urllib.error, urllib.parse
from collections import defaultdict

import pandas as pd

API = "https://fr24api.flightradar24.com/api/flight-summary/light"
BATCH = 15          # API maximum for the registrations parameter
PAUSE = 1.0         # seconds between calls


# ---------------------------------------------------------------- http

def fetch(params, token, cache_dir, dry_run=False):
    """One flight-summary call, cached on disk by the exact parameter set."""
    key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]
    path = os.path.join(cache_dir, key + ".json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f), True          # cached: costs nothing
    if dry_run:
        return None, False

    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Accept-Version": "v1",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        # 401/403 almost always means the token or the Accept-Version header is wrong;
        # 429 means the credit budget or rate limit is gone. Neither is worth retrying.
        raise SystemExit(f"FR24 API {e.code} on {params}\n{body}")
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)
    return data, False


# ---------------------------------------------------------------- matching

def same_airport(icao, iata):
    """BTS uses IATA, FR24 returns ICAO. Contiguous-US ICAO is K+IATA; Alaska and Hawaii
    use PA/PH prefixes. Compare on the trailing three characters, which holds for all of
    them, rather than hardcoding a prefix."""
    if not icao or not isinstance(icao, str):
        return False
    return icao[-3:].upper() == str(iata).upper()


def parse_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def pick_leg(records, reg, origin, dest):
    """Find the record for this tail flying origin->dest. Tails fly several legs a day, so
    the route is what disambiguates."""
    for r in records:
        if str(r.get("reg", "")).upper() != str(reg).upper():
            continue
        if same_airport(r.get("orig_icao"), origin) and same_airport(r.get("dest_icao"), dest):
            return r
    return None


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default="exposure_events.csv")
    ap.add_argument("--cache", default="./fr24_cache")
    ap.add_argument("--out", default="validated_events.csv")
    ap.add_argument("--max-queries", type=int, default=50,
                    help="hard cap on billable API calls (cached calls are free)")
    ap.add_argument("--estimate", action="store_true",
                    help="print the query plan and exit without calling the API")
    ap.add_argument("--limit-events", type=int, default=0,
                    help="only validate the first N events, cheapest-first sanity check")
    args = ap.parse_args()

    ev = pd.read_csv(args.events)
    if args.limit_events:
        # smallest overlap first: both aircraft in the turn airport's terminal area, the
        # geometry of the publicised events and the most informative rows to spend on
        ev = ev.sort_values("overlap_min").head(args.limit_events)

    # Group the registrations we need by date. A leg can take off and land either side of
    # UTC midnight, so each date is queried as a ~48h window.
    by_date = defaultdict(set)
    for _, e in ev.iterrows():
        by_date[e.date].add(str(e.tail_in))
        by_date[e.date].add(str(e.tail_out))

    plan = []
    for date, regs in sorted(by_date.items()):
        regs = sorted(r for r in regs if r and r.lower() != "nan")
        for i in range(0, len(regs), BATCH):
            plan.append((date, regs[i:i + BATCH]))

    print(f"{len(ev)} events -> {len(by_date)} dates -> {len(plan)} API calls "
          f"({BATCH} registrations each)")
    if args.estimate:
        print("\n--estimate: no API calls made. Re-run without it to execute.")
        print(f"FR24 Essential ($90/mo) includes 333,000 credits and 2 years of history; "
              f"{len(plan)} calls is a small fraction of that.")
        return

    token = os.environ.get("FR24_TOKEN")
    if not token:
        sys.exit("set FR24_TOKEN to your Flightradar24 API subscription token")

    os.makedirs(args.cache, exist_ok=True)
    records_by_date, spent = {}, 0
    for date, regs in plan:
        d0 = dt.date.fromisoformat(date)
        params = {
            "flight_datetime_from": f"{d0 - dt.timedelta(days=1)}T12:00:00Z",
            "flight_datetime_to": f"{d0 + dt.timedelta(days=1)}T12:00:00Z",
            "registrations": ",".join(regs),
            "limit": 200,
        }
        probe, cached = fetch(params, token, args.cache, dry_run=True)
        if probe is None and spent >= args.max_queries:
            print(f"stopping: hit --max-queries={args.max_queries}", file=sys.stderr)
            break
        data, cached = fetch(params, token, args.cache)
        if not cached:
            spent += 1
            time.sleep(PAUSE)
        records_by_date.setdefault(date, []).extend(data.get("data", []) or [])

    print(f"billable API calls this run: {spent}")

    rows = []
    for _, e in ev.iterrows():
        recs = records_by_date.get(e.date)
        if recs is None:
            continue
        a = pick_leg(recs, e.tail_in, e.origin, e.turn_airport)
        b = pick_leg(recs, e.tail_out, e.turn_airport, e.origin)
        if not a or not b:
            verdict, cs_in, cs_out, fr_overlap = "not_found", None, None, None
        else:
            cs_in = (a.get("callsign") or "").strip().upper()
            cs_out = (b.get("callsign") or "").strip().upper()
            # Independent overlap from FR24's UTC wheels times. No local-midnight anchor,
            # no timezone handling -- a genuinely separate calculation from exposure.py.
            off_a, on_a = parse_ts(a.get("datetime_takeoff")), parse_ts(a.get("datetime_landed"))
            off_b, on_b = parse_ts(b.get("datetime_takeoff")), parse_ts(b.get("datetime_landed"))
            fr_overlap = None
            if all([off_a, on_a, off_b, on_b]):
                fr_overlap = (min(on_a, on_b) - max(off_a, off_b)).total_seconds() / 60.0
            if not cs_in or not cs_out:
                verdict = "no_callsign"
            elif cs_in == cs_out:
                verdict = "duplicate_reached_atc"
            else:
                verdict = "stubbed"
        rows.append({
            "date": e.date, "bts_callsign": e.callsign, "origin": e.origin,
            "turn_airport": e.turn_airport, "tail_in": e.tail_in, "tail_out": e.tail_out,
            "fr24_callsign_in": cs_in, "fr24_callsign_out": cs_out,
            "verdict": verdict,
            "bts_overlap_min": e.overlap_min,
            "fr24_overlap_min": None if fr_overlap is None else round(fr_overlap, 1),
        })

    out = pd.DataFrame(rows)
    out.to_csv(args.out, index=False)

    print(f"\nresolved {len(out)} of {len(ev)} events -> {args.out}\n")
    if len(out):
        print(out.verdict.value_counts().to_string())
        real = out[out.verdict == "duplicate_reached_atc"]
        known = out[out.verdict.isin(["duplicate_reached_atc", "stubbed"])]
        if len(known):
            print(f"\nof {len(known)} events with both legs resolved, "
                  f"{len(real)} ({100*len(real)/len(known):.0f}%) put an identical callsign on ATC")
        # Cross-check the timing math against FR24's UTC wheels times.
        cmp = out.dropna(subset=["fr24_overlap_min"])
        if len(cmp):
            d = (cmp.fr24_overlap_min - cmp.bts_overlap_min).abs()
            print(f"\noverlap agreement vs BTS-derived value (n={len(cmp)}): "
                  f"median |diff| {d.median():.1f} min, 90th pct {d.quantile(0.9):.1f} min")
            print(f"  within 15 min: {100*(d<=15).mean():.0f}%")


if __name__ == "__main__":
    main()
