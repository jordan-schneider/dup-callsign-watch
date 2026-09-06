#!/usr/bin/env python3
"""
Duplicate-callsign exposure analysis from BTS On-Time Performance data.

Finds flight numbers used on both legs of an out-and-back (A->B then B->A,
same carrier, same day) and, for each day, checks whether the inbound leg's
*actual* arrival was later than the outbound leg's *actual* departure. When
it was, and the two legs used different tail numbers, two aircraft were
airborne simultaneously under the same callsign. That is exactly the
mechanism behind AA2482 (PHX), AA5083 (PVD), AA5383 (SDF), AA1275 (LAS).

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
        "DepDelay", "ArrDelay", "Cancelled", "Diverted"]

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
    df = df[(df.Cancelled == 0)].copy()
    df["FlightDate"] = pd.to_datetime(df.FlightDate)
    df["Flight_Number_Reporting_Airline"] = df.Flight_Number_Reporting_Airline.astype(int)
    return df


def hhmm_to_min(s):
    """BTS times are local HHMM floats; 2400 means midnight. Returns minutes since local midnight."""
    s = pd.to_numeric(s, errors="coerce")
    return (s // 100) * 60 + (s % 100)


def build_pairs(df):
    """Join each flight with the same-carrier, same-number, same-day flight going the reverse direction."""
    df = df.copy()
    for c in ["CRSDepTime", "DepTime", "CRSArrTime", "ArrTime"]:
        df[c + "_m"] = hhmm_to_min(df[c])
    # BTS keeps FlightDate = departure date; an arrival after midnight shows as e.g. 0013.
    # If arrival minutes < departure minutes the flight crossed midnight: add a day.
    for dep, arr in [("CRSDepTime_m", "CRSArrTime_m"), ("DepTime_m", "ArrTime_m")]:
        wrap = df[arr] < df[dep]
        df.loc[wrap, arr] += 1440
    # Same for a departure that itself is after midnight relative to the schedule day
    # (rare; e.g. sched 2350, actual 0020) so the outbound's actual dep isn't 24h early.
    late = (df["DepTime_m"] - df["CRSDepTime_m"]) < -720
    df.loc[late, "DepTime_m"] += 1440
    df.loc[late, "ArrTime_m"] += 1440
    key = ["FlightDate", "Reporting_Airline", "Flight_Number_Reporting_Airline"]
    a = df.add_suffix("_in")
    b = df.add_suffix("_out")
    m = a.merge(b, left_on=[k + "_in" for k in key], right_on=[k + "_out" for k in key])
    # inbound A->B, outbound B->A, and inbound is scheduled first
    m = m[(m.Origin_in == m.Dest_out) & (m.Dest_in == m.Origin_out)
          & (m.CRSDepTime_m_in < m.CRSDepTime_m_out)].copy()
    m["turn_airport"] = m.Dest_in
    # Scheduled buffer: outbound sched dep minus inbound sched arr. Times are local at turn airport
    # for both (inbound arrival local, outbound departure local), so no tz correction needed.
    m["sched_buffer_min"] = m.CRSDepTime_m_out - m.CRSArrTime_m_in
    # Actual overlap: inbound actual arrival later than outbound actual departure
    m["overlap_min"] = m.ArrTime_m_in - m.DepTime_m_out
    m["same_tail"] = m.Tail_Number_in == m.Tail_Number_out
    m["dup_callsign"] = (m.overlap_min > 0) & (~m.same_tail)
    # Near miss: inbound landed within 15 min of outbound departure (would have overlapped with slightly more delay)
    m["near_miss"] = (m.overlap_min > -15) & (m.overlap_min <= 0) & (~m.same_tail)
    return m


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

    m["callsign"] = m.Reporting_Airline_in.map(ICAO).fillna(m.Reporting_Airline_in) + m.Flight_Number_Reporting_Airline_in.astype(str)

    events = m[m.dup_callsign].sort_values("overlap_min", ascending=False)
    events_out = events[["FlightDate_in", "callsign", "Reporting_Airline_in", "Flight_Number_Reporting_Airline_in",
                         "Origin_in", "turn_airport", "Tail_Number_in", "Tail_Number_out",
                         "CRSArrTime_in", "ArrTime_in", "CRSDepTime_out", "DepTime_out",
                         "ArrDelay_in", "sched_buffer_min", "overlap_min"]]
    events_out.columns = ["date", "callsign", "carrier", "flight", "origin", "turn_airport",
                          "tail_in", "tail_out", "sched_arr_in", "actual_arr_in", "sched_dep_out",
                          "actual_dep_out", "inbound_arr_delay", "sched_buffer_min", "overlap_min"]
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
    print(f"{len(events):,} pair-days where two aircraft flew the same callsign simultaneously")
    print(f"{int(m.near_miss.sum()):,} near misses (inbound landed <15 min before outbound departed, different tail)\n")
    print("Top exposed pairs:")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(pairs.head(25).to_string(index=False))
    print("\nWorst events:")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(events_out.head(15).to_string(index=False))


if __name__ == "__main__":
    main()
