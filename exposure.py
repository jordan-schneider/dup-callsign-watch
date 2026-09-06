#!/usr/bin/env python3
"""
Duplicate-callsign exposure analysis from BTS On-Time Performance data.

Finds flight numbers used on both legs of an out-and-back (A->B then B->A,
same carrier, same day) and, for each day, checks whether the inbound leg's
*actual* arrival was later than the outbound leg's *actual* departure. When
it was, and the two legs used different tail numbers, two aircraft were
airborne simultaneously under the same flight number. That is exactly the
mechanism behind AA2482 (PHX), AA5083 (PVD), AA5383 (SDF), AA1275 (LAS).

IMPORTANT -- what this does and does not measure. BTS records the operating
flight number, not the ATC callsign that was actually filed. When dispatch sees
a pending duplicate it normally "stubs" one leg, filing a distinct callsign so
controllers never receive two identical ones. This script therefore counts the
*opportunity* for a duplicate callsign, not confirmed duplicates on frequency.
The publicised events are the subset where stubbing did not happen. Treat the
output as an exposure ranking for deconfliction, and do not describe a row as a
confirmed duplicate-callsign event without independent evidence (ATC audio,
ADS-B showing both aircraft squawking the same ident). watch.py live has no such
limitation: it reads broadcast callsigns and so sees genuine duplicates.

Usage:
  1. Download monthly zips from
     https://transtats.bts.gov/PREZIP/On_Time_Reporting_Carrier_On_Time_Performance_1987_present_YYYY_M.zip
     (or use --download to fetch them; BTS is usually 2-3 months behind).
  2. python exposure.py --data ./bts --carriers AA OH --start 2025-09 --end 2026-06

Outputs:
  exposure_pairs.csv   one row per (carrier, flight number, airport, direction pair)
                       with buffer stats and how often it was breached
  exposure_events.csv  one row per breach: a day where two aircraft flew the
                       same callsign at once
"""
import argparse, glob, io, os, sys, zipfile
import pandas as pd

BTS_URL = ("https://transtats.bts.gov/PREZIP/"
           "On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{y}_{m}.zip")

COLS = ["FlightDate", "Reporting_Airline", "Tail_Number", "Flight_Number_Reporting_Airline",
        "Origin", "Dest", "CRSDepTime", "DepTime", "CRSArrTime", "ArrTime",
        "DepDelay", "ArrDelay", "TaxiOut", "TaxiIn", "AirTime", "Cancelled", "Diverted"]

# ICAO carrier prefix so output reads as an ATC callsign
ICAO = {"AA": "AAL", "OH": "JIA", "MQ": "ENY", "YX": "RPA", "PT": "PDT", "OO": "SKW",
        "DL": "DAL", "UA": "UAL", "WN": "SWA", "B6": "JBU", "AS": "ASA", "NK": "NKS",
        "F9": "FFT", "G4": "AAY", "HA": "HAL", "9E": "EDV", "YV": "ASH", "ZW": "AWI"}


def download(data_dir, months):
    import urllib.request
    os.makedirs(data_dir, exist_ok=True)
    for y, m in months:
        out = os.path.join(data_dir, f"bts_{y}_{m:02d}.zip")
        if os.path.exists(out):
            continue
        url = BTS_URL.format(y=y, m=m)
        print(f"fetching {url}", file=sys.stderr)
        urllib.request.urlretrieve(url, out)


def load(data_dir, carriers):
    frames = []
    for z in sorted(glob.glob(os.path.join(data_dir, "*.zip"))) + sorted(glob.glob(os.path.join(data_dir, "*.csv"))):
        if z.endswith(".zip"):
            with zipfile.ZipFile(z) as zf:
                name = [n for n in zf.namelist() if n.endswith(".csv")][0]
                df = pd.read_csv(zf.open(name), usecols=COLS, low_memory=False)
        else:
            df = pd.read_csv(z, usecols=COLS, low_memory=False)
        if carriers:
            df = df[df.Reporting_Airline.isin(carriers)]
        frames.append(df)
    if not frames:
        sys.exit(f"no BTS files found in {data_dir}")
    df = pd.concat(frames, ignore_index=True)
    df = df[(df.Cancelled == 0) & (df.Diverted == 0)].copy()
    df["FlightDate"] = pd.to_datetime(df.FlightDate)
    df["Flight_Number_Reporting_Airline"] = df.Flight_Number_Reporting_Airline.astype(int)
    return df


def hhmm_to_min(s):
    """BTS times are local HHMM floats; 2400 means midnight. Returns minutes since local midnight."""
    s = pd.to_numeric(s, errors="coerce")
    return (s // 100) * 60 + (s % 100)


def build_pairs(df):
    """Join each flight with the same-carrier, same-number, same-day reverse-direction leg.

    All times for a pair are expressed as minutes from the *turn airport's* local midnight
    on FlightDate. That single anchor is what makes the arithmetic safe, and it is worth
    being explicit about why: BTS CRSDepTime is local at the origin while CRSArrTime is
    local at the destination, so differencing them yields the local *clock* change, not
    elapsed time. JFK-LAX reads 203 minutes against a true block of 383. Any quantity built
    by subtracting one from the other is wrong by the timezone gap.

    So durations come from AirTime/TaxiOut/TaxiIn, which are true elapsed minutes, and the
    only cross-leg comparisons are between two times that are both local at the turn
    airport. Nothing here needs a timezone database, but nothing may difference times
    measured at different airports either.
    """
    df = df.copy()
    for c in ["CRSDepTime", "CRSArrTime"]:
        df[c + "_m"] = hhmm_to_min(df[c])
    for c in ["DepDelay", "ArrDelay", "TaxiOut", "TaxiIn", "AirTime"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df[["CRSDepTime_m", "CRSArrTime_m", "DepDelay", "ArrDelay",
                "TaxiOut", "TaxiIn", "AirTime"]].notna().all(axis=1)]
    key = ["FlightDate", "Reporting_Airline", "Flight_Number_Reporting_Airline"]
    m = df.add_suffix("_in").merge(df.add_suffix("_out"),
                                   left_on=[k + "_in" for k in key],
                                   right_on=[k + "_out" for k in key])
    # inbound A->B, outbound B->A, and inbound is scheduled first
    m = m[(m.Origin_in == m.Dest_out) & (m.Dest_in == m.Origin_out)
          & (m.CRSDepTime_m_in < m.CRSDepTime_m_out)].copy()
    m["turn_airport"] = m.Dest_in

    # Scheduled buffer at the turn: the inbound's scheduled arrival and the outbound's
    # scheduled departure are both local at the turn airport, so this subtraction is one of
    # the few that is legitimate, and it needs no timezone correction.
    m["sched_buffer_min"] = m.CRSDepTime_m_out - m.CRSArrTime_m_in
    # A flight number may cover several legs a day (CLT-LEX-CLT-LEX); the self-join then
    # pairs leg 1 with leg 4 as well as with leg 2. Keep the real turn: the reverse-
    # direction leg scheduled to depart soonest after the inbound is scheduled to land.
    m = (m[m.sched_buffer_min >= 0]
         .sort_values("sched_buffer_min")
         .drop_duplicates(subset=["FlightDate_in", "Reporting_Airline_in",
                                  "Flight_Number_Reporting_Airline_in",
                                  "Origin_in", "Dest_in", "CRSDepTime_in"]))

    # --- everything below is minutes from turn-airport local midnight on FlightDate ---
    # Inbound, worked backwards from its arrival at the turn airport. Actual times come
    # from schedule + reported delay: BTS delays are signed and unbounded, so this is exact
    # across any midnight rollover, where reading the HHMM clock is not.
    m["t_arr_in"] = m.CRSArrTime_m_in + m.ArrDelay_in
    m["t_won_in"] = m.t_arr_in - m.TaxiIn_in
    m["t_woff_in"] = m.t_won_in - m.AirTime_in
    m["t_dep_in"] = m.t_woff_in - m.TaxiOut_in
    # Outbound, worked forwards from its departure at the turn airport.
    m["t_dep_out"] = m.CRSDepTime_m_out + m.DepDelay_out
    m["t_woff_out"] = m.t_dep_out + m.TaxiOut_out
    m["t_won_out"] = m.t_woff_out + m.AirTime_out
    m["t_arr_out"] = m.t_won_out + m.TaxiIn_out

    def intersect(start_a, end_a, start_b, end_b):
        """Length of the overlap of two intervals. A plain difference of one leg's end and
        the other's start assumes an ordering that a large delay can invert."""
        return (pd.concat([end_a, end_b], axis=1).min(axis=1)
                - pd.concat([start_a, start_b], axis=1).max(axis=1))

    # Airborne overlap is the headline: both aircraft actually in the air at once. BTS
    # DepTime/ArrTime are gate times, so a gate overlap shorter than the combined taxi
    # (median ~16 min out + ~7 min in) never put two aircraft up together.
    m["overlap_min"] = intersect(m.t_woff_in, m.t_won_in, m.t_woff_out, m.t_won_out)
    m["overlap_gate_min"] = intersect(m.t_dep_in, m.t_arr_in, m.t_dep_out, m.t_arr_out)
    m["same_tail"] = m.Tail_Number_in == m.Tail_Number_out
    m["dup_callsign"] = (m.overlap_min > 0) & (~m.same_tail)
    m["dup_callsign_gate"] = (m.overlap_gate_min > 0) & (~m.same_tail)
    # Near miss: inbound landed within 15 min of the outbound rotating (would have
    # overlapped with slightly more delay)
    m["near_miss"] = (m.overlap_min > -15) & (m.overlap_min <= 0) & (~m.same_tail)
    # Reported actual clock times, for the CSV only.
    m["ArrTime_in"] = m.ArrTime_in
    m["DepTime_out"] = m.DepTime_out
    return m


def validate(m):
    """Invariants on the minute arithmetic. Bare integers carry no units, so the anchor
    ("minutes from turn-airport local midnight") lives in a comment rather than a type;
    these checks are what actually enforces it. Each one below corresponds to a bug this
    script has shipped."""
    problems = []

    def check(name, bad):
        n = int(bad.sum())
        if n:
            problems.append(f"  {name}: {n} rows")

    air_in, air_out = m.AirTime_in, m.AirTime_out
    # A leg cannot land before it rotates.
    check("inbound airborne interval inverted", m.t_woff_in >= m.t_won_in)
    check("outbound airborne interval inverted", m.t_woff_out >= m.t_won_out)
    # Wheels times must sit inside the gate-to-gate window.
    check("inbound wheels outside gate window",
          (m.t_woff_in < m.t_dep_in) | (m.t_won_in > m.t_arr_in))
    check("outbound wheels outside gate window",
          (m.t_woff_out < m.t_dep_out) | (m.t_won_out > m.t_arr_out))
    # Shared airborne time cannot exceed either leg's own air time. Both the -720 wrap bug
    # and the subtract-instead-of-intersect bug violated this.
    check("overlap exceeds inbound air time", m.overlap_min > air_in + 1e-6)
    check("overlap exceeds outbound air time", m.overlap_min > air_out + 1e-6)
    # A local-clock difference across two timezones masquerading as a duration shows up
    # here: no domestic narrowbody leg runs 18h.
    check("implausible inbound air time (>18h)", air_in > 1080)
    check("negative scheduled buffer survived the turn filter", m.sched_buffer_min < 0)

    if problems:
        print("VALIDATION FAILURES:", file=sys.stderr)
        for p in problems:
            print(p, file=sys.stderr)
    else:
        print("validation: all timing invariants hold")
    return not problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="./bts")
    ap.add_argument("--carriers", nargs="*", default=["AA", "OH", "MQ", "YX", "PT"],
                    help="BTS carrier codes; OH=PSA, MQ=Envoy, YX=Republic, PT=Piedmont")
    ap.add_argument("--start", help="YYYY-MM, with --download")
    ap.add_argument("--end", help="YYYY-MM, with --download")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--out", default=".")
    args = ap.parse_args()

    if args.download:
        s = pd.Period(args.start, "M"); e = pd.Period(args.end, "M")
        download(args.data, [(p.year, p.month) for p in pd.period_range(s, e)])

    df = load(args.data, args.carriers)
    m = build_pairs(df)
    validate(m)

    m["callsign"] = m.Reporting_Airline_in.map(ICAO).fillna(m.Reporting_Airline_in) + m.Flight_Number_Reporting_Airline_in.astype(str)

    events = m[m.dup_callsign].sort_values("overlap_min", ascending=False)
    events_out = events[["FlightDate_in", "callsign", "Reporting_Airline_in", "Flight_Number_Reporting_Airline_in",
                         "Origin_in", "turn_airport", "Tail_Number_in", "Tail_Number_out",
                         "CRSArrTime_in", "ArrTime_in", "CRSDepTime_out", "DepTime_out",
                         "ArrDelay_in", "sched_buffer_min", "overlap_gate_min", "overlap_min"]]
    events_out.columns = ["date", "callsign", "carrier", "flight", "origin", "turn_airport",
                          "tail_in", "tail_out", "sched_arr_in", "actual_arr_in", "sched_dep_out",
                          "actual_dep_out", "inbound_arr_delay", "sched_buffer_min",
                          "overlap_gate_min", "overlap_min"]
    events_out.to_csv(os.path.join(args.out, "exposure_events.csv"), index=False)

    g = m.groupby(["callsign", "Reporting_Airline_in", "Flight_Number_Reporting_Airline_in", "Origin_in", "turn_airport"])
    pairs = g.agg(days=("FlightDate_in", "nunique"),
                  sched_buffer_min=("sched_buffer_min", "median"),
                  pct_diff_tail=("same_tail", lambda s: 100 * (1 - s.mean())),
                  inbound_delay_p90=("ArrDelay_in", lambda s: s.quantile(0.9)),
                  breaches=("dup_callsign", "sum"),
                  near_misses=("near_miss", "sum")).reset_index()
    pairs["breach_rate_pct"] = 100 * pairs.breaches / pairs.days
    pairs["p90_margin_min"] = pairs.sched_buffer_min - pairs.inbound_delay_p90
    pairs.columns = ["callsign", "carrier", "flight", "origin", "turn_airport", "days", "sched_buffer_min",
                     "pct_diff_tail", "inbound_delay_p90", "breaches", "near_misses", "breach_rate_pct", "p90_margin_min"]
    pairs = pairs.sort_values(["breaches", "p90_margin_min"], ascending=[False, True])
    pairs.to_csv(os.path.join(args.out, "exposure_pairs.csv"), index=False)

    print(f"\n{len(df):,} flights -> {len(m):,} out-and-back pair-days across {len(pairs):,} distinct pairs")
    print(f"{len(events):,} pair-days where two aircraft were AIRBORNE under the same callsign at once")
    print(f"{int(m.dup_callsign_gate.sum()):,} pair-days on the looser gate-time test (outbound pushed back before inbound arrived)")
    print(f"{int(m.near_miss.sum()):,} near misses (inbound landed <15 min before outbound departed, different tail)\n")
    print("Top exposed pairs:")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(pairs.head(25).to_string(index=False))
    print("\nWorst events:")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(events_out.head(15).to_string(index=False))


if __name__ == "__main__":
    main()
