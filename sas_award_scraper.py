"""
SAS EuroBonus award seat monitor — multi-hub, using the award-finder
destinations endpoint (real per-cabin seat counts, not dynamic pricing).

No login required. No browser required. One HTTP call per hub covers
the whole destination watchlist, since the destinations param takes a
comma-separated list.

Checks from all three SAS hubs reachable directly from BGO — Oslo (OSL),
Copenhagen (CPH), and Stockholm (ARN) — since business/points pricing
can differ meaningfully between them for the same destination. BGO<->hub
itself is a separate short domestic hop, checked via the exact same
mechanism (feeder legs).

Layout:
  1. Config      - hubs + destinations to watch
  2. Search      - hit the award-finder BFF endpoint
  3. Parse       - turn the JSON into normalized per-date/cabin records
  4. Store       - append to SQLite, compute diff vs last run
  5. Alert       - notify on newly-appeared seats
  6. Scheduler   - entrypoint
"""

import asyncio
import dataclasses
import datetime as dt
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

import aiohttp


# ---------------------------------------------------------------------------
# 1. Config
# ---------------------------------------------------------------------------

DB_PATH = Path("sas_availability.db")

# The three SAS hub airports reachable directly from BGO. Checking all
# three (not just CPH) because points pricing and business availability
# to the same destination can genuinely differ hub to hub.
HUBS = ["OSL", "CPH", "ARN"]

# Confirmed to exist in SAS' network via the full destinations list we
# pulled from the award-finder page. This is now the COMPLETE list, not
# just the curated surf/kite/ski subset — batching means going from 13
# to ~115 destinations still costs just 3 HTTP calls (one per hub),
# since `destinations` accepts an arbitrarily long comma-separated list.
DESTINATIONS = [
    "AAL", "ALC", "ALF", "AMS", "AYT", "ATH", "ATL", "BCN", "BRI", "BER",
    "BIO", "BLL", "BHX", "BOO", "BLQ", "BOD", "BOS", "BRU", "BUD", "CAG",
    "CTA", "CHQ", "ORD", "DLM", "DUB", "DUS", "EDI", "FAO", "FLR", "FRA",
    "FUE", "FAE", "GZP", "GDN", "GVA", "GOT", "HAM", "HAJ", "EVE", "HAU",
    "HEL", "HER", "KKN", "KRN", "KRK", "KRS", "KSU", "LCA", "LPA", "LIS",
    "LHR", "LYR", "LAX", "LLA", "LUX", "LYS", "FNC", "MAD", "AGP", "MLA",
    "MAN", "RAK", "MRS", "MIA", "MXP", "LIN", "BOM", "MUC", "NAP", "EWR",
    "JFK", "NCE", "GOH", "OLB", "PLQ", "PMO", "PMI", "CDG", "PSA", "OPO",
    "POZ", "PRG", "PUY", "KEF", "RHO", "RIX", "FCO", "RVN", "SZG", "SFO",
    "SEA", "ICN", "SVQ", "SFT", "SPU", "SVG", "STR", "SDL", "SCR", "TLL",
    "TLV", "TFS", "SKG", "JTR", "TIV", "HND", "YYZ", "TOS", "TRD", "TKU",
    "UME", "VAA", "VCE", "VNO", "VBY", "WAW", "IAD", "VIE", "WRO", "ZRH",
    "AES", "AAR", "OSD", "AGA",
]

# The BGO<->hub feeder hop for each of the three hubs, checked with the
# exact same endpoint and mechanism as the main destinations — just
# origin/destination swapped. Tells us WHETHER a feeder seat exists on
# the relevant dates, not WHEN it departs (see fetch_flight_times for
# actual times, fetched separately for a shortlist only).
FEEDER_ORIGIN = "BGO"

SEARCH_INTERVAL_SECONDS = 60 * 60 * 24  # once a day

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# 2. Search
# ---------------------------------------------------------------------------

# Confirmed live via DevTools. No auth needed. `destinations` accepts a
# comma-separated list — one call covers the entire watchlist.
SAS_AWARD_FINDER_URL = "https://www.sas.no/bff/award-finder/destinations/v1"

# Also confirmed live via DevTools — a DIFFERENT endpoint, per specific
# date, returning actual flight times. Only called for a small shortlist
# of dates (see fetch_shortlist_times below), never for the whole
# watchlist — at one call per date this would explode in volume otherwise.
SAS_ROUTES_URL = "https://www.sas.no/bff/award-finder/routes/v1"

# The very first endpoint we found, back before we knew about award-finder.
# Returns a blended points+NOK-price calendar (NOT split by cabin — this
# is believed to reflect economy/cheapest-cabin pricing, not confirmed).
# Re-added here specifically to backfill a points figure onto shortlisted
# trips, since award-finder gives real seat counts but no price at all.
SAS_DATEPICKER_URL = "https://www.sas.no/bff/datepicker/flights/offers/v1"


# CONFIRMED to work with entirely fake sas-user-session-id/sas-correlation-id
# values (verified live via browser console) — the endpoint validates the
# HEADER SCHEMA, not actual session authenticity. This is the best data
# source we've found: real per-cabin points AND real flight times AND
# SkyTeam partner options (confirmed: KLM appeared alongside SAS), all in
# one call per specific date pair. Only used for the shortlist (see below),
# same reasoning as the other per-date endpoints — one call per date pair,
# not the whole watchlist.
SAS_AWARD_API_FLIGHTS_URL = "https://www.sas.no/award-api/flights"

AWARD_API_HEADERS = {
    **REQUEST_HEADERS,
    "channel": "WEB",
    "language": "no",
    "locale": "no-no",
    "pos": "NO",
}


async def fetch_award_api_flights(
    session: aiohttp.ClientSession, origin: str, destination: str,
    out_date: dt.date, in_date: dt.date,
) -> str:
    """
    The best data source found so far — real per-cabin points, real
    flight times, and SkyTeam partner flights, all for one round trip
    in a single call. Requires sas-user-session-id/sas-correlation-id
    headers to be PRESENT (schema check), but their values are never
    actually validated — a random UUID works fine.
    """
    import uuid
    headers = {
        **AWARD_API_HEADERS,
        "sas-user-session-id": str(uuid.uuid4()),
        "sas-correlation-id": str(uuid.uuid4()),
    }
    params = {
        "origin": origin,
        "destination": destination,
        "outboundDate": out_date.isoformat(),
        "inboundDate": in_date.isoformat(),
        "tripType": "round-trip",
        "selectedCouponCodes": "",
        "adults": "1", "children": "0", "infants": "0", "youths": "0",
    }
    async with session.get(SAS_AWARD_API_FLIGHTS_URL, params=params, headers=headers) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Unexpected status {resp.status} from award-api/flights for {origin}->{destination}")
        return await resp.text()


@dataclasses.dataclass(frozen=True)
class DetailedOfferRecord:
    origin: str
    destination: str
    date: dt.date         # the actual flight date for this leg
    direction: str        # "outbound" or "inbound"
    cabin: str             # "economy" or "business"
    points: int
    seats_available: int
    departure_time: str   # "HH:MM" local
    arrival_time: str     # "HH:MM" local
    marketing_carrier: str  # e.g. "SK" (SAS) or "KL" (KLM) — partner detection
    num_stops: int
    checked_at: dt.datetime


def parse_award_api_flights(raw: str, origin: str, destination: str) -> list[DetailedOfferRecord]:
    """
    Confirmed shape (real capture): top-level outboundFlights/inboundFlights,
    each a flight option with segments (real times, marketingCarrier) and
    a cabins list (economy/business, each with a price.SKY.points figure
    and availableSeats). Multiple flight options may exist per direction —
    kept separately PER CARRIER (not collapsed to one global cheapest),
    so SAS and partner (e.g. KLM) options can be shown side by side
    instead of the partner silently hiding a SAS option that cost more.
    """
    data = json.loads(raw)
    now = dt.datetime.now(dt.timezone.utc)
    best: dict[tuple, DetailedOfferRecord] = {}  # (direction, cabin, carrier) -> best record

    for direction, key in (("outbound", "outboundFlights"), ("inbound", "inboundFlights")):
        for flight in data.get(key, []):
            segments = flight.get("segments", [])
            if not segments:
                continue
            marketing_carrier = segments[0].get("marketingCarrier", {}).get("code", "")
            flight_date = dt.date.fromisoformat(flight["startDateTimeInLocal"][:10])
            for cabin_info in flight.get("cabins", []):
                cabin = cabin_info.get("cabin", "")
                price_ctx = cabin_info.get("price", {}).get("SKY") or cabin_info.get("price", {}).get("BILATERAL")
                if not price_ctx:
                    continue
                points = price_ctx.get("points", 0)
                record = DetailedOfferRecord(
                    origin=origin, destination=destination, date=flight_date,
                    direction=direction, cabin=cabin, points=points,
                    seats_available=cabin_info.get("availableSeats", 0),
                    departure_time=flight.get("startTimeInLocal", ""),
                    arrival_time=flight.get("endTimeInLocal", ""),
                    marketing_carrier=marketing_carrier,
                    num_stops=flight.get("stops", 0),
                    checked_at=now,
                )
                dict_key = (direction, cabin, marketing_carrier)
                if dict_key not in best or points < best[dict_key].points:
                    best[dict_key] = record
    return list(best.values())


async def fetch_points(session: aiohttp.ClientSession, origin: str, destination: str, departure_date: dt.date, return_date: dt.date) -> str:
    """
    One call, using the trip's actual two dates as departureDate/returnDate,
    returns a whole MONTH calendar for each: outbound covers departure_date's
    month, inbound covers return_date's month. We only keep the two exact
    dates we asked about, but the extra dates in the response cost nothing
    extra to fetch.
    """
    params = {
        "market": "no-no",
        "departureDate": departure_date.isoformat(),
        "returnDate": return_date.isoformat(),
        "bookingFlow": "points",
        "origin": origin,
        "destination": destination,
        "adult": "1", "child": "0", "infant": "0", "youth": "0",
        "tripType": "RT",
    }
    async with session.get(SAS_DATEPICKER_URL, params=params, headers=REQUEST_HEADERS) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Unexpected status {resp.status} from SAS datepicker for {origin}->{destination}")
        return await resp.text()


async def run_search(session: aiohttp.ClientSession, origin: str, destinations: list[str]) -> str:
    params = {
        "market": "no-no",
        "origin": origin,
        "destinations": ",".join(destinations),
        "selectedMonth": "",
        "passengers": "1",
        "direct": "false",
        "availability": "true",
    }
    async with session.get(SAS_AWARD_FINDER_URL, params=params, headers=REQUEST_HEADERS) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Unexpected status {resp.status} from SAS award-finder")
        return await resp.text()


async def fetch_flight_times(session: aiohttp.ClientSession, origin: str, destination: str, date: dt.date) -> str:
    """
    One specific origin/destination/date -> actual flight options with
    real departure/arrival times. Confirmed shape (real example):

        [
          {"flightId": "SK1921-20260910-OSL-AAL",
           "departureTime": "17:15", "arrivalTime": "18:10",
           "availability": {"AG": 10, "AB": 10},
           "noOfFlights": 1, "flyTime": 55, "totalTime": 55},
          ...
        ]

    noOfFlights > 1 means a connection is involved; totalTime (minutes)
    includes any connection wait, flyTime doesn't.
    """
    params = {
        "market": "no-no",
        "origin": origin,
        "destination": destination,
        "departureDate": date.isoformat(),
        "direct": "false",
    }
    async with session.get(SAS_ROUTES_URL, params=params, headers=REQUEST_HEADERS) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Unexpected status {resp.status} from SAS routes for {origin}->{destination} {date}")
        return await resp.text()


# ---------------------------------------------------------------------------
# 3. Parse
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class AvailabilityRecord:
    origin: str
    destination: str
    date: dt.date
    direction: str  # "outbound" or "inbound"
    seats_economy: int   # "AG" — SAS' economy fare bucket
    seats_business: int  # "AB" — business
    seats_premium: int   # "AP" — premium economy, only on long-haul routes
    seats_total: int
    checked_at: dt.datetime


def parse_results(raw: str, origin: str) -> list[AvailabilityRecord]:
    """
    Parse the award-finder response. Confirmed shape (real example):

        [
          {
            "airportCode": "AGA",
            "availability": {
              "outbound": [
                {"key": 261128, "date": "2026-11-28",
                 "availableSeatsTotal": 2, "AG": 2},
                {"key": 261205, "date": "2026-12-05",
                 "availableSeatsTotal": 18, "AG": 9, "AB": 9},
                ...
              ],
              "inbound": [ ... same shape ... ]
            }
          },
          ... one object per requested destination ...
        ]

    A date only appears here at all if award space actually exists that
    day — this is real bookable seat inventory per cabin, not a dynamic
    price. Missing AG/AB/AP keys mean 0 seats in that cabin, not that the
    field is inapplicable.
    """
    data = json.loads(raw)
    now = dt.datetime.now(dt.timezone.utc)
    records: list[AvailabilityRecord] = []

    for dest_obj in data:
        destination = dest_obj["airportCode"]
        availability = dest_obj.get("availability", {})
        for direction in ("outbound", "inbound"):
            for entry in availability.get(direction, []):
                records.append(AvailabilityRecord(
                    origin=origin,
                    destination=destination,
                    date=dt.date.fromisoformat(entry["date"]),
                    direction=direction,
                    seats_economy=entry.get("AG", 0),
                    seats_business=entry.get("AB", 0),
                    seats_premium=entry.get("AP", 0),
                    seats_total=entry.get("availableSeatsTotal", 0),
                    checked_at=now,
                ))
    return records


@dataclasses.dataclass(frozen=True)
class PointsRecord:
    origin: str  # the hub used for the search
    destination: str
    date: dt.date
    direction: str  # "outbound" or "inbound"
    points: int
    price_nok: int
    is_standard_award: bool  # SAS' flat fixed-tier rate (seen as 15,000 in
    # real captures) vs a genuinely dynamic price on non-standard days —
    # the fixed tiers (15k/30k/60k...) only apply on standard-award days
    checked_at: dt.datetime


def parse_points(raw: str, origin: str, destination: str) -> list[PointsRecord]:
    """
    Confirmed shape (from earlier real capture):
        {"currency": "NOK",
         "outbound": {"2026-09-19": {"totalPrice": 2094, "points": 28032}, ...},
         "inbound":  {"2026-10-07": {"totalPrice": 437, "points": 15000,
                                       "isStandardAward": true}, ...}}
    NOT split by cabin — treat as an approximate/likely-economy reference
    price. isStandardAward marks SAS' flat fixed-tier rate (observed as
    exactly 15,000 in real data) — the fixed 15k/30k/60k tier progression
    a user pointed out is believed to apply ONLY on these flagged days;
    everything else is genuinely dynamic pricing with no clean tier.
    """
    data = json.loads(raw)
    now = dt.datetime.now(dt.timezone.utc)
    records = []
    for direction in ("outbound", "inbound"):
        for date_str, info in data.get(direction, {}).items():
            records.append(PointsRecord(
                origin=origin, destination=destination,
                date=dt.date.fromisoformat(date_str), direction=direction,
                points=info["points"], price_nok=info.get("totalPrice", 0),
                is_standard_award=info.get("isStandardAward", False),
                checked_at=now,
            ))
    return records


@dataclasses.dataclass(frozen=True)
class FlightTimeRecord:
    origin: str
    destination: str
    date: dt.date
    flight_id: str
    departure_time: str  # "HH:MM", local time at origin
    arrival_time: str    # "HH:MM", local time at destination
    seats_economy: int
    seats_business: int
    num_flights: int  # >1 means a connection
    total_time_minutes: int
    checked_at: dt.datetime


def parse_flight_times(raw: str, origin: str, destination: str, date: dt.date) -> list[FlightTimeRecord]:
    data = json.loads(raw)
    now = dt.datetime.now(dt.timezone.utc)
    records = []
    for entry in data:
        avail = entry.get("availability", {})
        records.append(FlightTimeRecord(
            origin=origin, destination=destination, date=date,
            flight_id=entry.get("flightId", ""),
            departure_time=entry.get("departureTime", ""),
            arrival_time=entry.get("arrivalTime", ""),
            seats_economy=avail.get("AG", 0),
            seats_business=avail.get("AB", 0),
            num_flights=entry.get("noOfFlights", 1),
            total_time_minutes=entry.get("totalTime", 0),
            checked_at=now,
        ))
    return records


# ---------------------------------------------------------------------------
# 4. Store + diff
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS availability (
            origin TEXT, destination TEXT, date TEXT, direction TEXT,
            seats_economy INTEGER, seats_business INTEGER, seats_premium INTEGER,
            seats_total INTEGER, checked_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flight_times (
            origin TEXT, destination TEXT, date TEXT, flight_id TEXT,
            departure_time TEXT, arrival_time TEXT,
            seats_economy INTEGER, seats_business INTEGER,
            num_flights INTEGER, total_time_minutes INTEGER, checked_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS points_price (
            origin TEXT, destination TEXT, date TEXT, direction TEXT,
            points INTEGER, price_nok INTEGER, is_standard_award INTEGER, checked_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS detailed_offers (
            origin TEXT, destination TEXT, date TEXT, direction TEXT, cabin TEXT,
            points INTEGER, seats_available INTEGER, departure_time TEXT, arrival_time TEXT,
            marketing_carrier TEXT, num_stops INTEGER, checked_at TEXT
        )
    """)
    conn.commit()


def save_detailed_offers(conn: sqlite3.Connection, records: list[DetailedOfferRecord]) -> None:
    cur = conn.cursor()
    for r in records:
        cur.execute(
            """INSERT INTO detailed_offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r.origin, r.destination, r.date.isoformat(), r.direction, r.cabin,
             r.points, r.seats_available, r.departure_time, r.arrival_time,
             r.marketing_carrier, r.num_stops, r.checked_at.isoformat()),
        )
    conn.commit()


def save_points(conn: sqlite3.Connection, records: list[PointsRecord]) -> None:
    cur = conn.cursor()
    for r in records:
        cur.execute(
            """INSERT INTO points_price VALUES (?,?,?,?,?,?,?,?)""",
            (r.origin, r.destination, r.date.isoformat(), r.direction,
             r.points, r.price_nok, int(r.is_standard_award), r.checked_at.isoformat()),
        )
    conn.commit()


def save_flight_times(conn: sqlite3.Connection, records: list[FlightTimeRecord]) -> None:
    cur = conn.cursor()
    for r in records:
        cur.execute(
            """INSERT INTO flight_times VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (r.origin, r.destination, r.date.isoformat(), r.flight_id,
             r.departure_time, r.arrival_time, r.seats_economy, r.seats_business,
             r.num_flights, r.total_time_minutes, r.checked_at.isoformat()),
        )
    conn.commit()


def save_and_diff(
    conn: sqlite3.Connection, records: Iterable[AvailabilityRecord]
) -> list[AvailabilityRecord]:
    """
    Insert new snapshot rows and return records worth alerting on: dates
    where BUSINESS seats just appeared where there were none before (the
    highest-value signal). On the very first run, everything currently
    open counts as "new" — that's expected, it's a one-time full picture.
    """
    new_hits = []
    cur = conn.cursor()
    for r in records:
        cur.execute(
            """SELECT seats_business FROM availability
               WHERE origin=? AND destination=? AND date=? AND direction=?
               ORDER BY checked_at DESC LIMIT 1""",
            (r.origin, r.destination, r.date.isoformat(), r.direction),
        )
        prev = cur.fetchone()
        had_business_before = bool(prev[0]) if prev else False
        if r.seats_business > 0 and not had_business_before:
            new_hits.append(r)
        cur.execute(
            """INSERT INTO availability VALUES (?,?,?,?,?,?,?,?,?)""",
            (r.origin, r.destination, r.date.isoformat(), r.direction,
             r.seats_economy, r.seats_business, r.seats_premium,
             r.seats_total, r.checked_at.isoformat()),
        )
    conn.commit()
    return new_hits


# ---------------------------------------------------------------------------
# 5. Alert
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[ALERT - no telegram configured] {text}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")


def notify(new_hits: list[AvailabilityRecord]) -> None:
    for hit in new_hits:
        send_telegram(
            f"SAS business award åpnet: {hit.origin}->{hit.destination} "
            f"({hit.direction}) {hit.date}: {hit.seats_business} business-seter "
            f"(og {hit.seats_economy} økonomi)"
        )


# ---------------------------------------------------------------------------
# 6. Scheduler entrypoint
# ---------------------------------------------------------------------------

# Caps how many destinations get the EXPENSIVE per-date deep-dive
# (real flight times + points price) out of the now much larger ~118
# destination watchlist. The broad availability check (build_shortlist
# uses) is one cheap batched call per hub regardless of destination
# count, but flight-times/points are one call PER DATE — uncapped, 118
# destinations × 3 hubs could mean hundreds of calls. Only the
# top-ranked (by business seats found) destinations get this treatment.
MAX_SHORTLIST_DESTINATIONS = 20


def _rank_destinations_by_business(dest_view: list[dict]) -> list[str]:
    ranked = sorted(
        dest_view,
        key=lambda d: (d["bestSummary"]["biz"] if d["bestSummary"] else 0),
        reverse=True,
    )
    return [d["destination"] for d in ranked[:MAX_SHORTLIST_DESTINATIONS]]


def build_shortlist_dates(conn: sqlite3.Connection) -> set[tuple[str, str, dt.date]]:
    """
    Reuse the dashboard's own trip-shortlisting logic (same repo, same
    directory) to figure out which (origin, destination, date) legs are
    actually worth fetching real flight times for — the top-ranked
    candidate trips across ALL hubs, capped to MAX_SHORTLIST_DESTINATIONS
    destinations (see comment above), not every date across the whole
    ~118-destination watchlist.
    """
    import generate_dashboard as dash

    rows = dash.load_latest_rows(conn)
    feeder_lookup = dash.build_feeder_lookup(rows)
    trips_by_hub_dest = dash.build_trips(rows, feeder_lookup)
    dest_view = dash.build_destination_view(trips_by_hub_dest)
    top_destinations = set(_rank_destinations_by_business(dest_view))
    open_jaw = dash.build_open_jaw_trips(rows, feeder_lookup)

    needed: set[tuple[str, str, dt.date]] = set()
    for (hub, destination), info in trips_by_hub_dest.items():
        if destination not in top_destinations:
            continue
        for t in info["trips"]:
            out_d = dt.date.fromisoformat(t["outDate"])
            in_d = dt.date.fromisoformat(t["inDate"])
            needed.add((hub, destination, out_d))        # main leg out
            needed.add((destination, hub, in_d))          # main leg home
            needed.add((FEEDER_ORIGIN, hub, out_d))        # feeder out
            needed.add((hub, FEEDER_ORIGIN, in_d))         # feeder home
    for group in open_jaw:
        for t in group["trips"]:
            if t["outDestination"] not in top_destinations and t["inDestination"] not in top_destinations:
                continue
            hub = t.get("hub", HUBS[0])
            out_dest, in_dest = t["outDestination"], t["inDestination"]
            out_d = dt.date.fromisoformat(t["outDate"])
            in_d = dt.date.fromisoformat(t["inDate"])
            needed.add((hub, out_dest, out_d))
            needed.add((in_dest, hub, in_d))
            needed.add((FEEDER_ORIGIN, hub, out_d))
            needed.add((hub, FEEDER_ORIGIN, in_d))
    return needed


async def fetch_shortlist_detailed(session: aiohttp.ClientSession, conn: sqlite3.Connection) -> None:
    """
    Replaces the old fetch_shortlist_times + fetch_shortlist_points pair.
    For each shortlisted trip, 2 calls to award-api/flights (main round
    trip + feeder round trip) now yield real per-cabin points, real
    times, AND partner-carrier detection — richer data in FEWER calls
    than the two separate endpoints we used before.
    """
    shortlist = build_shortlist_trips(conn)
    print(f"Fetching detailed offers for {len(shortlist)} shortlisted trips (2 calls each: main + feeder)")
    for hub, destination, out_date, in_date in shortlist:
        try:
            raw = await fetch_award_api_flights(session, hub, destination, out_date, in_date)
            records = parse_award_api_flights(raw, hub, destination)
            save_detailed_offers(conn, records)
        except Exception as e:
            print(f"[ERROR] detailed offers {hub}->{destination} {out_date}/{in_date}: {e}")
        await asyncio.sleep(1)

        try:
            feeder_raw = await fetch_award_api_flights(session, FEEDER_ORIGIN, hub, out_date, in_date)
            feeder_records = parse_award_api_flights(feeder_raw, FEEDER_ORIGIN, hub)
            save_detailed_offers(conn, feeder_records)
        except Exception as e:
            print(f"[ERROR] feeder detailed offers {FEEDER_ORIGIN}->{hub} {out_date}/{in_date}: {e}")
        await asyncio.sleep(1)


def build_shortlist_trips(conn: sqlite3.Connection) -> set[tuple[str, str, dt.date, dt.date]]:
    """
    Same idea as build_shortlist_dates, but returns (hub, destination,
    out_date, in_date) TRIP pairs instead of individual legs — that's
    what fetch_points needs, since one datepicker call takes both dates
    at once and returns both months in a single response. Same
    MAX_SHORTLIST_DESTINATIONS cap applies.
    """
    import generate_dashboard as dash

    rows = dash.load_latest_rows(conn)
    feeder_lookup = dash.build_feeder_lookup(rows)
    trips_by_hub_dest = dash.build_trips(rows, feeder_lookup)
    dest_view = dash.build_destination_view(trips_by_hub_dest)
    top_destinations = set(_rank_destinations_by_business(dest_view))

    needed: set[tuple[str, str, dt.date, dt.date]] = set()
    for d in dest_view:
        if d["destination"] not in top_destinations:
            continue
        for hub, trips in d["hubs"].items():
            for t in trips:
                needed.add((
                    hub, d["destination"],
                    dt.date.fromisoformat(t["outDate"]),
                    dt.date.fromisoformat(t["inDate"]),
                ))
    return needed


async def fetch_shortlist_points(session: aiohttp.ClientSession, conn: sqlite3.Connection) -> None:
    """Kept as a fallback data source (blended/dynamic economy price on
    dates the detailed endpoint's shortlist doesn't cover), but no longer
    the primary source for business pricing — award-api/flights above
    gives real per-cabin prices now."""
    shortlist = build_shortlist_trips(conn)
    print(f"Fetching points price for {len(shortlist)} shortlisted trips")
    for hub, destination, out_date, in_date in shortlist:
        try:
            raw = await fetch_points(session, hub, destination, out_date, in_date)
            records = parse_points(raw, hub, destination)
            save_points(conn, records)
        except Exception as e:
            print(f"[ERROR] points {hub}->{destination} {out_date}/{in_date}: {e}")
        await asyncio.sleep(1)


async def run_once(conn: sqlite3.Connection) -> None:
    async with aiohttp.ClientSession() as session:
        for hub in HUBS:
            try:
                raw = await run_search(session, hub, DESTINATIONS)
                records = parse_results(raw, hub)
                hits = save_and_diff(conn, records)
                notify(hits)
                print(f"[{hub}] Checked {len(DESTINATIONS)} destinations, {len(records)} date records, {len(hits)} new business hits")
            except Exception as e:
                print(f"[ERROR] main destinations via {hub}: {e}")
            await asyncio.sleep(1)

            try:
                feeder_raw = await run_search(session, FEEDER_ORIGIN, [hub])
                feeder_records = parse_results(feeder_raw, FEEDER_ORIGIN)
                save_and_diff(conn, feeder_records)  # stored for cross-referencing; not alerted on separately
                print(f"[{hub}] Checked feeder {FEEDER_ORIGIN}->{hub}, {len(feeder_records)} date records")
            except Exception as e:
                print(f"[ERROR] feeder route {FEEDER_ORIGIN}->{hub}: {e}")
            await asyncio.sleep(1)

        try:
            await fetch_shortlist_detailed(session, conn)
        except Exception as e:
            print(f"[ERROR] shortlist detailed offers: {e}")


async def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.once:
        await run_once(conn)
        return

    while True:
        await run_once(conn)
        await asyncio.sleep(SEARCH_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
