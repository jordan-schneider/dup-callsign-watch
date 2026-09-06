#!/usr/bin/env python3
"""
Duplicate-callsign watcher.

Two modes:

  live     Poll a public ADS-B aggregator (adsb.lol, no key needed) and alert when
           two different ICAO 24-bit addresses are broadcasting the same callsign
           within a given distance of each other. Lead time: minutes. This is the
           safety net, and it is completely free.

             python watch.py live --lat 33.43 --lon -112.01 --radius 200 --airline JIA AAL

  predict  Watch a list of out-and-back pairs (from exposure_pairs.csv) via
           FlightAware AeroAPI and alert when the inbound's estimated arrival is
           later than the outbound's scheduled departure and the assigned tails
           differ. Lead time: typically 60-120 minutes, i.e. before either aircraft
           is airborne together. Needs an AeroAPI key (AEROAPI_KEY env var; the
           Personal tier is a few dollars a month).

             python watch.py predict --pairs exposure_pairs.csv --top 200

Alerts print to stdout and, if ALERT_WEBHOOK is set, POST as JSON to that URL
(Slack/Discord/ntfy all work).
"""
import argparse, json, math, os, sys, time, datetime as dt
import urllib.request

ADSB_URL = "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}"   # dist in nm, max 250
AEROAPI = "https://aeroapi.flightaware.com/aeroapi"


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "dup-callsign-watch/0.1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def alert(kind, payload):
    line = f"[{dt.datetime.utcnow():%Y-%m-%d %H:%M:%SZ}] {kind}: {json.dumps(payload)}"
    print(line, flush=True)
    hook = os.environ.get("ALERT_WEBHOOK")
    if hook:
        try:
            data = json.dumps({"text": line, "kind": kind, **payload}).encode()
            urllib.request.urlopen(urllib.request.Request(hook, data=data,
                                   headers={"Content-Type": "application/json"}), timeout=10)
        except Exception as e:
            print(f"webhook failed: {e}", file=sys.stderr)


def haversine_nm(lat1, lon1, lat2, lon2):
    R = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- live mode

def find_duplicates(aircraft, airlines=None, max_sep_nm=150):
    """Group ADS-B targets by callsign; return groups with >1 distinct hex within max_sep_nm."""
    by_cs = {}
    for ac in aircraft:
        cs = (ac.get("flight") or "").strip().upper()
        hexid = ac.get("hex")
        if not cs or not hexid or ac.get("lat") is None:
            continue
        if airlines and not any(cs.startswith(a) for a in airlines):
            continue
        by_cs.setdefault(cs, {})[hexid] = ac   # dict dedups repeated hex
    out = []
    for cs, targets in by_cs.items():
        if len(targets) < 2:
            continue
        ts = list(targets.values())
        for i in range(len(ts)):
            for j in range(i + 1, len(ts)):
                a, b = ts[i], ts[j]
                sep = haversine_nm(a["lat"], a["lon"], b["lat"], b["lon"])
                if sep <= max_sep_nm:
                    out.append(dict(callsign=cs, sep_nm=round(sep, 1),
                                    a=dict(hex=a["hex"], reg=a.get("r"), alt=a.get("alt_baro"), lat=a["lat"], lon=a["lon"]),
                                    b=dict(hex=b["hex"], reg=b.get("r"), alt=b.get("alt_baro"), lat=b["lat"], lon=b["lon"])))
    return out


def live(args):
    seen = {}   # callsign -> last alert time, to avoid spamming every poll
    while True:
        try:
            data = get(ADSB_URL.format(lat=args.lat, lon=args.lon, dist=min(args.radius, 250)))
            dups = find_duplicates(data.get("ac", []), args.airline, args.max_sep)
            now = time.time()
            for d in dups:
                if now - seen.get(d["callsign"], 0) > 600:
                    alert("DUPLICATE_CALLSIGN_AIRBORNE", d)
                    seen[d["callsign"]] = now
            if args.verbose:
                print(f"{dt.datetime.utcnow():%H:%M:%S} {len(data.get('ac', []))} targets, {len(dups)} dups", file=sys.stderr)
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr)
        time.sleep(args.interval)


# ------------------------------------------------------------- predict mode

def aero(path, key):
    return get(AEROAPI + path, {"x-apikey": key})


def predict(args):
    import csv
    key = os.environ.get("AEROAPI_KEY") or sys.exit("set AEROAPI_KEY")
    with open(args.pairs) as f:
        pairs = list(csv.DictReader(f))[: args.top]
    print(f"watching {len(pairs)} out-and-back pairs", file=sys.stderr)
    seen = {}
    while True:
        for p in pairs:
            ident = p["callsign"]                       # e.g. AAL2482 / JIA5083
            try:
                # Today's legs for this ident, both directions
                fl = aero(f"/flights/{ident}", key).get("flights", [])
            except Exception as e:
                print(f"{ident}: {e}", file=sys.stderr); continue
            legs = [x for x in fl if x.get("origin", {}).get("code_iata") and x.get("destination", {}).get("code_iata")]
            inbound = [x for x in legs if x["destination"]["code_iata"] == p["turn_airport"] and x["origin"]["code_iata"] == p["origin"]]
            outbound = [x for x in legs if x["origin"]["code_iata"] == p["turn_airport"] and x["destination"]["code_iata"] == p["origin"]]
            for i in inbound:
                for o in outbound:
                    # Only same-day pairings where outbound is scheduled after inbound
                    if not (i.get("scheduled_off") and o.get("scheduled_off")) or o["scheduled_off"] < i["scheduled_off"]:
                        continue
                    if o.get("actual_on") or i.get("actual_on"):
                        continue                      # already resolved
                    eta_in = i.get("estimated_on") or i.get("scheduled_on")
                    dep_out = o.get("estimated_off") or o.get("scheduled_off")
                    if not (eta_in and dep_out):
                        continue
                    overlap = (dt.datetime.fromisoformat(eta_in.replace("Z", "+00:00"))
                               - dt.datetime.fromisoformat(dep_out.replace("Z", "+00:00"))).total_seconds() / 60
                    tail_i, tail_o = i.get("registration"), o.get("registration")
                    same_tail = tail_i and tail_o and tail_i == tail_o
                    if overlap > -args.margin and not same_tail:
                        k = (ident, i.get("fa_flight_id"), o.get("fa_flight_id"))
                        if time.time() - seen.get(k, 0) > 900:
                            alert("PREDICTED_DUPLICATE" if overlap > 0 else "MARGIN_LOW",
                                  dict(callsign=ident, turn_airport=p["turn_airport"],
                                       inbound_eta=eta_in, outbound_dep=dep_out,
                                       overlap_min=round(overlap), tail_in=tail_i, tail_out=tail_o,
                                       minutes_until_outbound_dep=round((dt.datetime.fromisoformat(dep_out.replace("Z", "+00:00")) - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60)))
                            seen[k] = time.time()
            time.sleep(args.pace)
        time.sleep(args.interval)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    l = sub.add_parser("live")
    l.add_argument("--lat", type=float, required=True); l.add_argument("--lon", type=float, required=True)
    l.add_argument("--radius", type=float, default=250, help="query radius nm (adsb.lol max 250)")
    l.add_argument("--max-sep", type=float, default=150, help="alert if duplicates within this many nm")
    l.add_argument("--airline", nargs="*", help="ICAO prefixes to watch, e.g. AAL JIA ENY RPA PDT")
    l.add_argument("--interval", type=int, default=30)
    l.add_argument("--verbose", action="store_true")
    pr = sub.add_parser("predict")
    pr.add_argument("--pairs", required=True, help="exposure_pairs.csv from exposure.py")
    pr.add_argument("--top", type=int, default=200)
    pr.add_argument("--margin", type=int, default=20, help="also warn when buffer is under this many minutes")
    pr.add_argument("--interval", type=int, default=600, help="seconds between full passes")
    pr.add_argument("--pace", type=float, default=0.5, help="seconds between AeroAPI calls")
    args = ap.parse_args()
    (live if args.mode == "live" else predict)(args)


if __name__ == "__main__":
    main()
