"""
Generates an interactive index.html dashboard from sas_availability.db.
Destination-centric: search/browse by destination, click to expand and
compare which hub (OSL/CPH/ARN) to fly from, when, whether a 5-10 night
return exists, and the economy/premium/business breakdown per hub.

Also keeps the length-based "best trips" top sections (3-6 / 7 / 8-12
nights) across all hubs, and the open-jaw combined-trip groups.

No external dependencies — stdlib only.
"""

import datetime as dt
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = Path("sas_availability.db")
OUT_PATH = Path("index.html")

# Mirrors sas_award_scraper.py's HUBS — the three SAS hub airports
# reachable directly from BGO, each checked independently since points
# pricing/business availability to the same destination can differ.
HUBS = ["OSL", "CPH", "ARN"]
FEEDER_ORIGIN = "BGO"

HUB_NAMES = {"OSL": "Oslo", "CPH": "København", "ARN": "Stockholm"}

TARGET_NIGHTS = 7
TOP_TRIPS_COUNT = 6
MAX_TRIPS_PER_CATEGORY = 3
MAX_TRIPS_PER_HUB_IN_COMPARE = 2

# The specific window for the "click a destination, compare hubs" view
# — a 5-10 night return, per how that feature was asked for.
COMPARE_MIN_NIGHTS = 4
COMPARE_MAX_NIGHTS = 10

# Confirmed EuroBonus mechanic (via multiple independent sources, not a
# guess): with sufficient Fly Premium tier for the route's zone/cabin,
# a Business or Premium Economy bonus seat costs the SAME points as
# Economy on that route — not a percentage of the Business price. See
# pointsLine() in the page template for where this is actually applied.

# Minimum minutes between a feeder arrival and the next flight's
# departure for a same-day connection to be realistic (checking bags,
# getting through the terminal, etc). This is an approximation, not an
# official minimum connection time — shown as such in the UI.
MIN_CONNECTION_MINUTES = 60


def night_category(nights: int) -> str:
    if nights <= 6:
        return "kort"
    if nights == 7:
        return "uke"
    return "lang"


CATEGORY_LABELS = {
    "kort": "Kort tur (3–6 netter)",
    "uke": "Ukestur (7 netter)",
    "lang": "Lengre tur (8–12 netter)",
}

# Full destination set from the award-finder page — name, coordinates
# (for the map view), pulled from the same JSON the SAS site itself
# returned when we listed all CPH destinations. Some are domestic
# Nordic airports (Bodø, Stavanger, etc.) included because the user
# asked for the complete list, not just the curated surf/kite/ski subset.
DESTINATION_INFO = {
    "AAL": {"name": "Aalborg", "lon": 9.850, "lat": 57.095},
    "ALC": {"name": "Alicante", "lon": -0.558, "lat": 38.282},
    "ALF": {"name": "Alta", "lon": 23.372, "lat": 69.976},
    "AMS": {"name": "Amsterdam", "lon": 4.764, "lat": 52.309},
    "AYT": {"name": "Antalya", "lon": 30.801, "lat": 36.899},
    "ATH": {"name": "Athen", "lon": 23.945, "lat": 37.936},
    "ATL": {"name": "Atlanta", "lon": -84.428, "lat": 33.637},
    "BCN": {"name": "Barcelona", "lon": 2.078, "lat": 41.297},
    "BRI": {"name": "Bari", "lon": 16.761, "lat": 41.139},
    "BER": {"name": "Berlin", "lon": 13.501, "lat": 52.362},
    "BIO": {"name": "Bilbao", "lon": -2.911, "lat": 43.301},
    "BLL": {"name": "Billund", "lon": 9.157, "lat": 55.740},
    "BHX": {"name": "Birmingham", "lon": -1.748, "lat": 52.454},
    "BOO": {"name": "Bodø", "lon": 14.365, "lat": 67.269},
    "BLQ": {"name": "Bologna", "lon": 11.289, "lat": 44.535},
    "BOD": {"name": "Bordeaux", "lon": -0.716, "lat": 44.828},
    "BOS": {"name": "Boston", "lon": -71.008, "lat": 42.362},
    "BRU": {"name": "Brussel", "lon": 4.484, "lat": 50.901},
    "BUD": {"name": "Budapest", "lon": 19.262, "lat": 47.430},
    "CAG": {"name": "Cagliari", "lon": 9.054, "lat": 39.252},
    "CTA": {"name": "Catania (Sicilia)", "lon": 15.066, "lat": 37.467},
    "CHQ": {"name": "Chania", "lon": 24.150, "lat": 35.532},
    "ORD": {"name": "Chicago", "lon": -87.905, "lat": 41.979},
    "DLM": {"name": "Dalaman", "lon": 28.793, "lat": 36.713},
    "DUB": {"name": "Dublin", "lon": -6.262, "lat": 53.429},
    "DUS": {"name": "Düsseldorf", "lon": 6.767, "lat": 51.290},
    "EDI": {"name": "Edinburgh", "lon": -3.372, "lat": 55.950},
    "FAO": {"name": "Faro", "lon": -7.966, "lat": 37.014},
    "FLR": {"name": "Firenze", "lon": 11.203, "lat": 43.809},
    "FRA": {"name": "Frankfurt", "lon": 8.561, "lat": 50.030},
    "FUE": {"name": "Fuerteventura", "lon": -13.864, "lat": 28.453},
    "FAE": {"name": "Færøyene", "lon": -7.276, "lat": 62.063},
    "GZP": {"name": "Gazipasa", "lon": 32.301, "lat": 36.299},
    "GDN": {"name": "Gdansk", "lon": 18.466, "lat": 54.378},
    "GVA": {"name": "Genève", "lon": 6.109, "lat": 46.238},
    "GOT": {"name": "Gøteborg", "lon": 12.280, "lat": 57.663},
    "HAM": {"name": "Hamburg", "lon": 9.988, "lat": 53.630},
    "HAJ": {"name": "Hannover", "lon": 9.685, "lat": 52.461},
    "EVE": {"name": "Harstad-Narvik", "lon": 16.678, "lat": 68.491},
    "HAU": {"name": "Haugesund", "lon": 5.208, "lat": 59.345},
    "HEL": {"name": "Helsinki", "lon": 24.963, "lat": 60.318},
    "HER": {"name": "Heraklion", "lon": 25.180, "lat": 35.340},
    "KKN": {"name": "Kirkenes", "lon": 29.891, "lat": 69.726},
    "KRN": {"name": "Kiruna", "lon": 20.337, "lat": 67.822},
    "KRK": {"name": "Krakow", "lon": 19.785, "lat": 50.078},
    "KRS": {"name": "Kristiansand", "lon": 8.085, "lat": 58.204},
    "KSU": {"name": "Kristiansund", "lon": 7.825, "lat": 63.112},
    "LCA": {"name": "Larnaca", "lon": 33.625, "lat": 34.875},
    "LPA": {"name": "Las Palmas", "lon": -15.387, "lat": 27.932},
    "LIS": {"name": "Lisboa", "lon": -9.136, "lat": 38.781},
    "LHR": {"name": "London", "lon": -0.462, "lat": 51.471},
    "LYR": {"name": "Longyearbyen", "lon": 15.466, "lat": 78.246},
    "LAX": {"name": "Los Angeles", "lon": -118.408, "lat": 33.943},
    "LLA": {"name": "Luleå", "lon": 22.122, "lat": 65.544},
    "LUX": {"name": "Luxembourg", "lon": 6.204, "lat": 49.623},
    "LYS": {"name": "Lyon", "lon": 5.090, "lat": 45.726},
    "FNC": {"name": "Madeira", "lon": -16.775, "lat": 32.698},
    "MAD": {"name": "Madrid", "lon": -3.563, "lat": 40.472},
    "AGP": {"name": "Malaga", "lon": -4.499, "lat": 36.675},
    "MLA": {"name": "Malta", "lon": 14.478, "lat": 35.858},
    "MAN": {"name": "Manchester", "lon": -2.280, "lat": 53.349},
    "RAK": {"name": "Marrakesh", "lon": -8.036, "lat": 31.607},
    "MRS": {"name": "Marseille", "lon": 5.213, "lat": 43.438},
    "MIA": {"name": "Miami", "lon": -80.291, "lat": 25.793},
    "MXP": {"name": "Milano", "lon": 8.728, "lat": 45.631},
    "LIN": {"name": "Milano", "lon": 9.277, "lat": 45.445},
    "BOM": {"name": "Mumbai", "lon": 72.868, "lat": 19.089},
    "MUC": {"name": "München", "lon": 11.786, "lat": 48.354},
    "NAP": {"name": "Napoli", "lon": 14.291, "lat": 40.886},
    "EWR": {"name": "New York", "lon": -74.169, "lat": 40.693},
    "JFK": {"name": "New York", "lon": -73.779, "lat": 40.639},
    "NCE": {"name": "Nice", "lon": 7.216, "lat": 43.658},
    "GOH": {"name": "Nuuk", "lon": -51.678, "lat": 64.191},
    "OLB": {"name": "Olbia", "lon": 9.518, "lat": 40.899},
    "PLQ": {"name": "Palanga", "lon": 21.094, "lat": 55.973},
    "PMO": {"name": "Palermo", "lon": 13.091, "lat": 38.176},
    "PMI": {"name": "Palma Mallorca", "lon": 2.739, "lat": 39.552},
    "CDG": {"name": "Paris", "lon": 2.550, "lat": 49.013},
    "PSA": {"name": "Pisa", "lon": 10.393, "lat": 43.684},
    "OPO": {"name": "Porto", "lon": -8.681, "lat": 41.248},
    "POZ": {"name": "Poznan", "lon": 16.823, "lat": 52.422},
    "PRG": {"name": "Prague", "lon": 14.260, "lat": 50.101},
    "PUY": {"name": "Pula", "lon": 13.922, "lat": 44.894},
    "KEF": {"name": "Reykjavík", "lon": -22.606, "lat": 63.985},
    "RHO": {"name": "Rhodos", "lon": 28.086, "lat": 36.405},
    "RIX": {"name": "Riga", "lon": 23.971, "lat": 56.924},
    "FCO": {"name": "Rom", "lon": 12.252, "lat": 41.805},
    "RVN": {"name": "Rovaniemi", "lon": 25.830, "lat": 66.565},
    "SZG": {"name": "Salzburg", "lon": 13.004, "lat": 47.793},
    "SFO": {"name": "San Francisco", "lon": -122.375, "lat": 37.620},
    "SEA": {"name": "Seattle", "lon": -122.310, "lat": 47.448},
    "ICN": {"name": "Seoul", "lon": 126.451, "lat": 37.469},
    "SVQ": {"name": "Sevilla", "lon": -5.893, "lat": 37.418},
    "SFT": {"name": "Skellefteå", "lon": 21.077, "lat": 64.625},
    "SPU": {"name": "Split", "lon": 16.298, "lat": 43.539},
    "SVG": {"name": "Stavanger", "lon": 5.638, "lat": 58.877},
    "STR": {"name": "Stuttgart", "lon": 9.222, "lat": 48.690},
    "SDL": {"name": "Sundsvall", "lon": 17.444, "lat": 62.528},
    "SCR": {"name": "Sälen/Trysil", "lon": 12.843, "lat": 61.158},
    "TLL": {"name": "Tallinn", "lon": 24.833, "lat": 59.413},
    "TLV": {"name": "Tel Aviv Yafo", "lon": 34.887, "lat": 32.011},
    "TFS": {"name": "Tenerife", "lon": -16.573, "lat": 28.045},
    "SKG": {"name": "Thessaloniki", "lon": 22.970, "lat": 40.519},
    "JTR": {"name": "Thira (Santorini)", "lon": 25.479, "lat": 36.399},
    "TIV": {"name": "Tivat", "lon": 18.723, "lat": 42.405},
    "HND": {"name": "Tokyo", "lon": 139.780, "lat": 35.552},
    "YYZ": {"name": "Toronto", "lon": -79.631, "lat": 43.677},
    "TOS": {"name": "Tromsø", "lon": 18.919, "lat": 69.683},
    "TRD": {"name": "Trondheim", "lon": 10.924, "lat": 63.458},
    "TKU": {"name": "Turku", "lon": 22.263, "lat": 60.514},
    "UME": {"name": "Umeå", "lon": 20.283, "lat": 63.792},
    "VAA": {"name": "Vasa", "lon": 21.762, "lat": 63.051},
    "VCE": {"name": "Venezia", "lon": 12.352, "lat": 45.505},
    "VNO": {"name": "Vilnius", "lon": 25.286, "lat": 54.634},
    "VBY": {"name": "Visby", "lon": 18.346, "lat": 57.663},
    "WAW": {"name": "Warsaw", "lon": 20.967, "lat": 52.166},
    "IAD": {"name": "Washington", "lon": -77.456, "lat": 38.944},
    "VIE": {"name": "Wien", "lon": 16.570, "lat": 48.110},
    "WRO": {"name": "Wroclaw", "lon": 16.882, "lat": 51.104},
    "ZRH": {"name": "Zürich", "lon": 8.548, "lat": 47.458},
    "AES": {"name": "Ålesund", "lon": 6.120, "lat": 62.563},
    "AAR": {"name": "Århus", "lon": 10.618, "lat": 56.303},
    "OSD": {"name": "Østersund/Åre", "lon": 14.500, "lat": 63.194},
    "AGA": {"name": "Agadir", "lon": -9.412, "lat": 30.322},
}
DESTINATION_NAMES = {code: info["name"] for code, info in DESTINATION_INFO.items()}

# Static climate/season knowledge — not scraped, doesn't change day to
# day, so it's simplest to just maintain by hand here.
SEASON_INFO = {
    "FAO": "Sør-Portugal (Algarve). ~18–23°C sjøtemperatur høst, jevn Atlanterhavs-bølge, best surf sep–nov.",
    "LIS": "Lisboa/Peniche. Pålitelig bølge hele høsten, litt kaldere vann enn Algarve. Høysesong sep–nov.",
    "OPO": "Porto/Matosinhos-Espinho. Kaldere vann enn Lisboa/Algarve (våtdrakt året rundt), konsistent bølge, mindre folksomt.",
    "BIO": "Baskerland (Mundaka, Zarautz). Kraftigere, mer konsistent swell enn Portugal, kaldere — våtdrakt okt–jan.",
    "BOD": "Bordeaux — inngang til Hossegor/Lacanau, noe av det mest kjente surfet i Europa. Kraftig swell, kaldere vann, våtdrakt sep–feb.",
    "AGP": "Innfallsport til Tarifa. Sterk, pålitelig vind (levante/poniente) store deler av året — en av Europas beste kitespots.",
    "CTA": "Lo Stagnone-lagunen (Trapani/Sicilia). Flatvann, termisk ettermiddagsvind, nybegynnervennlig. Sesong mai–sep.",
    "FUE": "Kanariøyene. Passatvind hele året, spesielt pålitelig om vinteren når Europa ellers er vindstille.",
    "RHO": "Hellas. Mildere, mer ustabil vind enn Tarifa/Kanariøyene. Best jun–sep, varmt badevann.",
    "AGA": "Taghazout/Agadir. Surfbart hele året, mildt klima — pålitelig vinterdestinasjon i Nord-Afrika.",
    "GVA": "Innfallsport til Alpene (Sveits/Frankrike). Snøsikkert des–mar over ~1500m.",
    "MUC": "Innfallsport til Bayern/Østerrike-alpene. Snøsikkert des–mar, kortere transfer enn GVA til mange bakker.",
    "ZRH": "Innfallsport til sveitsiske Alpene. Snøsikkert des–mar, dyrere on-site enn Frankrike/Østerrike.",
}

WEEKDAY_NO = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]
MONTH_NO = ["", "jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des"]

# Groups of destinations close enough together to realistically combine
# into one open-jaw trip (fly into one, travel overland, fly home from
# the other). Only include pairs where that overland leg is genuinely
# practical — e.g. BIO and AGP are both "Spain" but ~1000km apart, not
# a reasonable pairing, so they're deliberately NOT grouped together.
OPEN_JAW_GROUPS = {
    "Portugal (FAO/LIS/OPO, ~280–370 km, tog eller leiebil)": ["FAO", "LIS", "OPO"],
    "Baskerland/SV-Frankrike (BIO/BOD, ~300 km, tog eller leiebil)": ["BIO", "BOD"],
    "Alpene: Genève–Zürich (~280 km)": ["GVA", "ZRH"],
    "Alpene: Zürich–München (~300 km)": ["ZRH", "MUC"],
    # GVA–MUC er bevisst UTELATT — ~600 km / 6t kjøring er ikke praktisk
    # for en ukes skitur, selv om alle tre teknisk sett er "Alpene".
}


def is_feeder_row(origin: str, destination: str) -> bool:
    return (origin == FEEDER_ORIGIN and destination in HUBS) or (origin in HUBS and destination == FEEDER_ORIGIN)


def format_date_no_year(date_str: str) -> str:
    d = dt.date.fromisoformat(date_str)
    return f"{d.day}. {MONTH_NO[d.month]}"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_latest_rows(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("""
        SELECT origin, destination, date, direction, seats_economy,
               seats_business, seats_premium, seats_total, checked_at
        FROM availability
    """)
    latest: dict[tuple, tuple] = {}
    for row in cur.fetchall():
        key = row[:4]
        if key not in latest or row[-1] > latest[key][-1]:
            latest[key] = row
    return list(latest.values())


def load_points(conn: sqlite3.Connection) -> dict[tuple, dict]:
    """(origin, destination, date, direction) -> {points, priceNok,
    isStandard}, latest snapshot only. Table may not exist yet (old DB,
    or the standard-award column pre-dates this) — handled gracefully."""
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT origin, destination, date, direction, points, price_nok,
                   is_standard_award, checked_at
            FROM points_price
        """)
        rows = cur.fetchall()
        has_standard_col = True
    except sqlite3.OperationalError:
        try:
            cur.execute("""
                SELECT origin, destination, date, direction, points, price_nok, checked_at
                FROM points_price
            """)
            rows = [(*r[:6], 0, r[6]) for r in cur.fetchall()]
            has_standard_col = False
        except sqlite3.OperationalError:
            return {}
    latest: dict[tuple, tuple] = {}
    for row in rows:
        key = row[:4]
        if key not in latest or row[-1] > latest[key][-1]:
            latest[key] = row
    return {k: {"points": v[4], "priceNok": v[5], "isStandard": bool(v[6])} for k, v in latest.items()}


def load_detailed_offers(conn: sqlite3.Connection) -> dict[tuple, list[dict]]:
    """(origin, destination) -> list of confirmed offer dicts (one per
    direction/cabin/carrier combo, latest snapshot). Powers the SAS-vs-
    partner comparison for shortlisted trips. Table may not exist yet."""
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT origin, destination, date, direction, cabin, points,
                   seats_available, departure_time, arrival_time,
                   marketing_carrier, num_stops, checked_at
            FROM detailed_offers
        """)
    except sqlite3.OperationalError:
        return {}

    latest: dict[tuple, tuple] = {}
    for row in cur.fetchall():
        key = row[:5] + (row[9],)  # origin,destination,date,direction,cabin,carrier
        if key not in latest or row[-1] > latest[key][-1]:
            latest[key] = row

    by_route: dict[tuple, list[dict]] = defaultdict(list)
    for origin, destination, date_str, direction, cabin, points, seats, dep, arr, carrier, stops, checked_at in latest.values():
        by_route[(origin, destination)].append({
            "date": date_str, "direction": direction, "cabin": cabin,
            "points": points, "seats": seats, "departure": dep, "arrival": arr,
            "carrier": carrier, "stops": stops,
        })
    return by_route


def load_flight_times(conn: sqlite3.Connection) -> dict[tuple, list[dict]]:
    """(origin, destination, date) -> list of flight option dicts. Table
    may not exist yet if the scraper hasn't run its shortlist-times phase
    — handled gracefully rather than crashing the whole dashboard build."""
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT origin, destination, date, flight_id, departure_time,
                   arrival_time, seats_economy, seats_business,
                   num_flights, checked_at
            FROM flight_times
        """)
    except sqlite3.OperationalError:
        return {}

    latest: dict[tuple, tuple] = {}
    for row in cur.fetchall():
        key = row[:4]
        if key not in latest or row[-1] > latest[key][-1]:
            latest[key] = row

    by_leg: dict[tuple, list[dict]] = defaultdict(list)
    for origin, destination, date_str, flight_id, dep, arr, econ, biz, num_flights, checked_at in latest.values():
        by_leg[(origin, destination, date_str)].append({
            "flightId": flight_id, "departure": dep, "arrival": arr,
            "economy": econ, "business": biz, "numFlights": num_flights,
        })
    return by_leg


# ---------------------------------------------------------------------------
# Connection checking
# ---------------------------------------------------------------------------

def _time_to_minutes(hhmm: str) -> int | None:
    if not hhmm or ":" not in hhmm:
        return None
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def check_connection(feeder_flights: list[dict], main_flights: list[dict], feeder_first: bool) -> dict:
    """
    feeder_first=True: feeder arrives, then main flight departs (outbound
    leg: BGO->hub lands, then hub->destination takes off).
    feeder_first=False: the reverse (inbound leg home: destination->hub
    lands, then hub->BGO takes off).
    """
    if not feeder_flights or not main_flights:
        return {"status": "unknown", "bufferMinutes": None}

    best_ok_buffer = None
    best_any_buffer = None
    for f in feeder_flights:
        for m in main_flights:
            if feeder_first:
                arr_min = _time_to_minutes(f["arrival"])
                dep_min = _time_to_minutes(m["departure"])
            else:
                arr_min = _time_to_minutes(m["arrival"])
                dep_min = _time_to_minutes(f["departure"])
            if arr_min is None or dep_min is None:
                continue
            buffer = dep_min - arr_min
            if buffer < 0:
                continue
            if best_any_buffer is None or buffer < best_any_buffer:
                best_any_buffer = buffer
            if buffer >= MIN_CONNECTION_MINUTES:
                if best_ok_buffer is None or buffer < best_ok_buffer:
                    best_ok_buffer = buffer

    if best_ok_buffer is not None:
        return {"status": "ok", "bufferMinutes": best_ok_buffer}
    if best_any_buffer is not None:
        return {"status": "tight", "bufferMinutes": best_any_buffer}
    return {"status": "none", "bufferMinutes": None}


def build_feeder_lookup(rows) -> dict[tuple, bool]:
    """(hub, "outbound"|"inbound", date) -> True if BGO<->hub had any
    seats that day. Fallback for when no flight_times data exists yet."""
    lookup = {}
    for origin, destination, date_str, direction, econ, biz, prem, total, checked_at in rows:
        if origin == FEEDER_ORIGIN and destination in HUBS:
            lookup[(destination, "outbound", date_str)] = total > 0
        elif origin in HUBS and destination == FEEDER_ORIGIN:
            lookup[(origin, "inbound", date_str)] = total > 0
    return lookup


def build_connection_lookup(flight_times: dict[tuple, list[dict]]) -> dict[tuple, dict]:
    """(origin, destination, date, "out"|"in") -> connection check result."""
    results = {}
    by_date: dict[str, list[tuple]] = defaultdict(list)
    for (origin, destination, date_str) in flight_times:
        by_date[date_str].append((origin, destination))

    for date_str, legs in by_date.items():
        for origin, destination in legs:
            if is_feeder_row(origin, destination):
                continue  # the feeder leg itself, not a trip leg to annotate
            main_flights = flight_times[(origin, destination, date_str)]
            if origin in HUBS:  # hub -> destination = outbound trip leg
                feeder_flights = flight_times.get((FEEDER_ORIGIN, origin, date_str), [])
                results[(origin, destination, date_str, "out")] = check_connection(feeder_flights, main_flights, feeder_first=True)
            elif destination in HUBS:  # destination -> hub = inbound trip leg
                feeder_flights = flight_times.get((destination, FEEDER_ORIGIN, date_str), [])
                results[(origin, destination, date_str, "in")] = check_connection(feeder_flights, main_flights, feeder_first=False)
    return results


def connection_info(connection_lookup, feeder_lookup, origin: str, destination: str, date_str: str, leg: str) -> dict:
    result = connection_lookup.get((origin, destination, date_str, leg))
    if result is not None:
        return result
    hub = origin if leg == "out" else destination
    direction = "outbound" if leg == "out" else "inbound"
    has_feeder_seats = feeder_lookup.get((hub, direction, date_str), False)
    return {"status": "unknown", "bufferMinutes": None, "feederSeatsSeen": has_feeder_seats}


def _nights_and_workdays(out_date: dt.date, in_date: dt.date) -> tuple[int, int]:
    nights = (in_date - out_date).days
    workdays = sum(
        1 for n in range(1, nights)
        if (out_date + dt.timedelta(days=n)).weekday() < 5
    )
    return nights, workdays


# ---------------------------------------------------------------------------
# Trip building
# ---------------------------------------------------------------------------

def build_trips(rows, feeder_lookup: dict, connection_lookup: dict | None = None, points_lookup: dict | None = None) -> dict[tuple, dict]:
    """
    Group rows by (hub, destination) — since rows now include multiple
    hubs as "origin", this naturally produces one entry per hub per
    destination. Returns both a length-categorized/capped trip list
    (for the "best trips" sections) and the FULL uncapped trip list
    (for the 5-10 night hub-comparison view, which needs its own window
    that doesn't line up with the kort/uke/lang categories).
    """
    connection_lookup = connection_lookup or {}
    points_lookup = points_lookup or {}
    by_route: dict[tuple, dict] = defaultdict(lambda: {"outbound": [], "inbound": [], "lastChecked": ""})
    for origin, destination, date_str, direction, econ, biz, prem, total, checked_at in rows:
        if is_feeder_row(origin, destination):
            continue
        r = by_route[(origin, destination)]
        r[direction].append({"date": date_str, "economy": econ, "premium": prem, "business": biz})
        if checked_at > r["lastChecked"]:
            r["lastChecked"] = checked_at

    result = {}
    for route, info in by_route.items():
        hub, destination = route
        all_trips = []
        for out in info["outbound"]:
            out_date = dt.date.fromisoformat(out["date"])
            for inb in info["inbound"]:
                in_date = dt.date.fromisoformat(inb["date"])
                nights = (in_date - out_date).days
                if nights < 3 or nights > 12:
                    continue
                _, workdays = _nights_and_workdays(out_date, in_date)
                all_trips.append({
                    "hub": hub, "destination": destination,
                    "outDate": out["date"], "outWeekday": WEEKDAY_NO[out_date.weekday()],
                    "inDate": inb["date"], "inWeekday": WEEKDAY_NO[in_date.weekday()],
                    "nights": nights, "workdays": workdays,
                    "category": night_category(nights),
                    "outEconomy": out["economy"], "outPremium": out["premium"], "outBusiness": out["business"],
                    "inEconomy": inb["economy"], "inPremium": inb["premium"], "inBusiness": inb["business"],
                    "outPoints": points_lookup.get((hub, destination, out["date"], "outbound")),
                    "inPoints": points_lookup.get((hub, destination, inb["date"], "inbound")),
                    "outConnection": connection_info(connection_lookup, feeder_lookup, hub, destination, out["date"], "out"),
                    "inConnection": connection_info(connection_lookup, feeder_lookup, destination, hub, inb["date"], "in"),
                })

        by_cat: dict[str, list] = defaultdict(list)
        for t in all_trips:
            by_cat[t["category"]].append(t)
        kept = []
        for cat_trips in by_cat.values():
            cat_trips.sort(key=lambda t: (-(t["outBusiness"] + t["inBusiness"]), t["workdays"]))
            kept.extend(cat_trips[:MAX_TRIPS_PER_CATEGORY])

        result[route] = {"trips": kept, "allTrips": all_trips, "lastChecked": info["lastChecked"]}
    return result


def build_open_jaw_trips(rows, feeder_lookup: dict, connection_lookup: dict | None = None) -> list[dict]:
    """
    For each OPEN_JAW_GROUPS entry and each hub, pair outbound dates from
    ONE destination in the group with inbound dates from a DIFFERENT
    destination in the same group — both legs via the SAME hub.
    """
    connection_lookup = connection_lookup or {}
    by_hub_dest: dict[tuple, dict] = defaultdict(lambda: {"outbound": [], "inbound": []})
    for origin, destination, date_str, direction, econ, biz, prem, total, checked_at in rows:
        if is_feeder_row(origin, destination):
            continue
        by_hub_dest[(origin, destination)][direction].append({"date": date_str, "economy": econ, "premium": prem, "business": biz})

    results = []
    for group_name, codes in OPEN_JAW_GROUPS.items():
        group_trips = []
        for hub in HUBS:
            for out_code in codes:
                for in_code in codes:
                    if out_code == in_code:
                        continue
                    for out in by_hub_dest.get((hub, out_code), {}).get("outbound", []):
                        out_date = dt.date.fromisoformat(out["date"])
                        for inb in by_hub_dest.get((hub, in_code), {}).get("inbound", []):
                            in_date = dt.date.fromisoformat(inb["date"])
                            nights = (in_date - out_date).days
                            if nights < 3 or nights > 12:
                                continue
                            _, workdays = _nights_and_workdays(out_date, in_date)
                            group_trips.append({
                                "hub": hub, "outDestination": out_code, "inDestination": in_code,
                                "outDate": out["date"], "outWeekday": WEEKDAY_NO[out_date.weekday()],
                                "inDate": inb["date"], "inWeekday": WEEKDAY_NO[in_date.weekday()],
                                "nights": nights, "workdays": workdays,
                                "category": night_category(nights),
                                "outEconomy": out["economy"], "outPremium": out["premium"], "outBusiness": out["business"],
                                "inEconomy": inb["economy"], "inPremium": inb["premium"], "inBusiness": inb["business"],
                                "outConnection": connection_info(connection_lookup, feeder_lookup, hub, out_code, out["date"], "out"),
                                "inConnection": connection_info(connection_lookup, feeder_lookup, in_code, hub, inb["date"], "in"),
                            })
        by_cat: dict[str, list] = defaultdict(list)
        for t in group_trips:
            by_cat[t["category"]].append(t)
        kept = []
        for cat_trips in by_cat.values():
            cat_trips.sort(key=lambda t: (-(t["outBusiness"] + t["inBusiness"]), t["workdays"]))
            kept.extend(cat_trips[:MAX_TRIPS_PER_CATEGORY])
        if kept:
            results.append({"group": group_name, "trips": kept})
    return results


def classify_destination(hubs: dict[str, list[dict]]) -> str | None:
    """
    Map-marker color rule (as specified):
      blue   = 2+ business seats found EACH way (out AND in) on some trip
      orange = business found on only ONE way (out xor in), or fewer than
               2 seats on the side that has it
      green  = trips exist but only ever pure economy, no business at all
      None   = no trip at all in the compare window for this destination
    Takes the single BEST trip across all hubs (business found, if any)
    to decide the color, not every trip.
    """
    best = None  # (rank, trip) — rank 2=blue,1=orange,0=green
    for hub, trips in hubs.items():
        for t in trips:
            out_biz, in_biz = t["outBusiness"], t["inBusiness"]
            if out_biz >= 2 and in_biz >= 2:
                rank = 2
            elif out_biz >= 1 or in_biz >= 1:
                rank = 1
            else:
                rank = 0
            if best is None or rank > best:
                best = rank
    if best is None:
        return None
    return {2: "blue", 1: "orange", 0: "green"}[best]


def split_confirmed_offers(offers: list[dict]) -> dict:
    """Split detailed_offers entries for one destination into SAS-operated
    ('SK') vs any partner carrier, each further split by direction."""
    sas = [o for o in offers if o["carrier"] == "SK"]
    partner = [o for o in offers if o["carrier"] != "SK"]
    return {"sas": sas, "partner": partner}


def build_destination_view(trips_by_hub_dest: dict[tuple, dict], detailed_offers: dict | None = None) -> list[dict]:
    """
    Regroup the (hub, destination)-keyed trip data into one entry per
    DESTINATION, with a sub-list per hub — this is what powers the
    click-to-expand "which hub should I fly from" comparison. Only
    trips within the COMPARE_MIN_NIGHTS-COMPARE_MAX_NIGHTS window are
    included here.
    """
    detailed_offers = detailed_offers or {}
    by_destination: dict[str, dict] = defaultdict(lambda: {"hubs": {}, "lastChecked": ""})
    for (hub, destination), info in trips_by_hub_dest.items():
        window_trips = [t for t in info["allTrips"] if COMPARE_MIN_NIGHTS <= t["nights"] <= COMPARE_MAX_NIGHTS]
        window_trips.sort(key=lambda t: (-(t["outBusiness"] + t["inBusiness"]), t["workdays"]))
        d = by_destination[destination]
        d["hubs"][hub] = window_trips[:MAX_TRIPS_PER_HUB_IN_COMPARE]
        if info["lastChecked"] > d["lastChecked"]:
            d["lastChecked"] = info["lastChecked"]

    payload = []
    for destination, info in sorted(by_destination.items()):
        best_summary = None
        for hub, trips in info["hubs"].items():
            for t in trips:
                biz = t["outBusiness"] + t["inBusiness"]
                if best_summary is None or biz > best_summary["biz"]:
                    best_summary = {"hub": hub, "nights": t["nights"], "biz": biz}
        loc = DESTINATION_INFO.get(destination)
        # Confirmed (real, not estimated) offers, only present for the
        # capped shortlist of top destinations the scraper deep-dives on.
        confirmed_raw = [o for (o_hub, o_dest), offers in detailed_offers.items()
                          if o_dest == destination for o in offers]
        payload.append({
            "destination": destination,
            "season": SEASON_INFO.get(destination, ""),
            "hubs": {hub: trips for hub, trips in info["hubs"].items()},
            "bestSummary": best_summary,
            "lastChecked": info["lastChecked"],
            "marker": classify_destination(info["hubs"]),
            "lon": loc["lon"] if loc else None,
            "lat": loc["lat"] if loc else None,
            "confirmedOffers": split_confirmed_offers(confirmed_raw) if confirmed_raw else None,
        })
    return payload


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SAS award dashboard</title>
<style>
  :root {
    --bg: #f7f7f5; --card: #ffffff; --border: #e2e2e0; --text: #1a1a1a;
    --muted: #777; --green-bg: #e1f5ee; --green-text: #085041;
    --blue-bg: #e6f1fb; --blue-text: #0c447c; --amber-bg: #faeeda; --amber-text: #854f0b;
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: var(--bg); margin: 0; padding: 1.5rem; color: var(--text);
         max-width: 640px; margin-left: auto; margin-right: auto; }
  h1 { font-size: 20px; font-weight: 500; margin: 2rem 0 4px; }
  h1:first-of-type { margin-top: 0; }
  .updated { color: var(--muted); font-size: 13px; margin-bottom: 1.25rem; }
  .list { display: flex; flex-direction: column; gap: 12px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; }
  .card h2 { font-size: 17px; margin: 0 0 2px; font-weight: 500; }
  .route-sub { color: var(--muted); font-size: 12px; margin-bottom: 8px; }
  .season { font-size: 12px; color: var(--muted); margin-bottom: 8px; line-height: 1.5; }
  .search-box { width: 100%; padding: 10px 14px; border-radius: 10px; border: 1px solid var(--border);
                font-size: 14px; margin-bottom: 14px; background: #fff; }
  .trip { border-top: 1px solid #eee; padding: 10px 0; font-size: 13px; }
  .trip:first-of-type { border-top: none; }
  .trip-dates { font-weight: 500; }
  .trip-weekdays { color: var(--muted); font-size: 12px; margin-top: 1px; }
  .trip-meta { color: var(--muted); font-size: 12px; margin-top: 2px; }
  .badge { display: inline-block; font-size: 11px; padding: 2px 7px; border-radius: 6px; margin-right: 4px; margin-top: 4px; }
  .badge.best { background: var(--green-bg); color: var(--green-text); }
  .badge.ok { background: var(--blue-bg); color: var(--blue-text); }
  .badge.feeder-warn { background: var(--amber-bg); color: var(--amber-text); }
  .badge.hub { background: #eee; color: #444; }
  .seats { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .none { color: var(--muted); font-size: 13px; }
  .top-card { border: 1px solid var(--green-bg); }
  details.dest-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; }
  details.dest-card summary { cursor: pointer; list-style: none; }
  details.dest-card summary::-webkit-details-marker { display: none; }
  .dest-summary-line { font-size: 12px; color: var(--green-text); margin-top: 4px; }
  .hub-block { border-top: 1px solid #eee; margin-top: 12px; padding-top: 10px; }
  .hub-block h3 { font-size: 13px; margin: 0 0 6px; font-weight: 500; }
  .confirmed-block { background: var(--green-bg); border-radius: var(--radius); padding: 10px 12px; margin-top: 12px; }
  .confirmed-block h3 { font-size: 12px; margin: 0 0 6px; font-weight: 500; color: var(--green-text); }
  .confirmed-group { margin-bottom: 6px; font-size: 12px; }
  .confirmed-group strong { display: block; margin-bottom: 2px; }
  .offer-line { color: var(--text); font-size: 12px; line-height: 1.5; }
  .view-toggle { display: flex; gap: 6px; margin-bottom: 12px; }
  .view-toggle button { padding: 6px 14px; border-radius: var(--radius); border: 1px solid var(--border);
                         background: #fff; font-size: 13px; cursor: pointer; }
  .view-toggle button.active { background: var(--text); color: #fff; border-color: var(--text); }
  .legend { display: none; gap: 14px; margin: 10px 0; font-size: 12px; color: var(--muted); align-items: center; }
  .legend .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 4px; }
</style>
</head>
<body>

<div class="updated">Generert __GENERATED__ · huber: Oslo/København/Stockholm, feeder fra BGO</div>

<h1>Destinasjoner</h1>
<div class="view-toggle">
  <button id="viewToggleList" class="active">Liste</button>
  <button id="viewToggleMap">Kart</button>
</div>
<input type="text" class="search-box" id="searchBox" placeholder="Søk etter destinasjon...">
<div class="list" id="destGrid"></div>
<div id="mapContainer" style="display:none;"></div>
<div class="legend" id="mapLegend" style="display:none;">
  <span><span class="dot" style="background:#378ADD;"></span>2+ business hver vei</span>
  <span><span class="dot" style="background:#EF9F27;"></span>Business én vei</span>
  <span><span class="dot" style="background:#639922;"></span>Kun økonomi</span>
</div>

<h1>Kombinerte turer (fly inn ett sted, hjem fra et annet)</h1>
<div class="list" id="openJawGrid"></div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/topojson/3.0.2/topojson.min.js"></script>
<script>
const DEST_VIEW = __DESTVIEW_JSON__;   // destination-centric, 4-10 night hub comparison
const OPEN_JAW = __OPENJAW_JSON__;
const DESTINATION_NAMES_JS = __DESTNAMES_JSON__;
const HUB_NAMES_JS = __HUBNAMES_JSON__;
const MONTH_NO = ["", "jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des"];

function formatDateNoYear(iso) {
  const parts = iso.split("-");
  return parseInt(parts[2], 10) + ". " + MONTH_NO[parseInt(parts[1], 10)];
}

function connectionBadge(conn, label) {
  if (!conn) return "";
  if (conn.status === "ok") {
    const h = Math.floor(conn.bufferMinutes / 60), m = conn.bufferMinutes % 60;
    return '<span class="badge best">' + label + ': god forbindelse (' + h + 't ' + m + 'm)</span>';
  }
  if (conn.status === "tight") {
    const h = Math.floor(conn.bufferMinutes / 60), m = conn.bufferMinutes % 60;
    return '<span class="badge feeder-warn">' + label + ': stram forbindelse (' + h + 't ' + m + 'm)</span>';
  }
  if (conn.status === "none") {
    return '<span class="badge feeder-warn">' + label + ': ingen forbindelse samme dag — vurder overnatting</span>';
  }
  return '<span class="badge feeder-warn">' + label + ': sjekk manuelt (tider ikke hentet ennå)</span>';
}

function cabinLine(econ, prem, biz) {
  let parts = [econ + ' øko'];
  if (prem > 0) parts.push('<strong>' + prem + ' premium</strong>');
  if (biz > 0) parts.push('<strong>' + biz + ' business</strong>');
  return parts.join(' · ');
}

function pointsLine(points, businessSeats) {
  if (!points) return '<span style="color:var(--muted);">poengpris ikke hentet ennå</span>';
  const priceType = points.isStandard ? 'fast standardsats' : 'dynamisk pris, ca.';
  const econStr = points.points.toLocaleString("no-NO").replace(/,/g, " ") + ' p økonomi (' + priceType + ')';
  if (businessSeats > 0) {
    // Confirmed EuroBonus mechanic (not a guess): with sufficient Fly
    // Premium tier for this zone/cabin, Business/Premium costs the SAME
    // points as Economy on the same route — not a fraction of the
    // Business price. Source: SAS Mastercard Fly Premium benefit terms.
    return econStr + ' · med Fly Premium: business koster likt som økonomiprisen over ('
      + points.points.toLocaleString("no-NO").replace(/,/g, " ")
      + ' p) — forutsatt riktig FP-nivå for denne sonen';
  }
  return econStr;
}

function renderTrip(t) {
  const distFromTarget = Math.abs(t.nights - 7);
  const badgeClass = distFromTarget === 0 ? "best" : "ok";
  const badgeLabel = distFromTarget === 0 ? "7 netter" : t.nights + " netter";
  return '<div class="trip">'
    + '<div class="trip-dates">' + formatDateNoYear(t.outDate) + ' – ' + formatDateNoYear(t.inDate) + '</div>'
    + '<div class="trip-weekdays">' + t.outWeekday + ' → ' + t.inWeekday + '</div>'
    + '<div class="trip-meta">' + t.workdays + ' fridager fra jobb nødvendig</div>'
    + '<span class="badge hub">' + (HUB_NAMES_JS[t.hub] || t.hub) + '</span>'
    + '<span class="badge ' + badgeClass + '">' + badgeLabel + '</span>'
    + connectionBadge(t.outConnection, "Ut") + connectionBadge(t.inConnection, "Hjem")
    + '<div class="seats">Ut: ' + cabinLine(t.outEconomy, t.outPremium, t.outBusiness) + ' · ' + pointsLine(t.outPoints, t.outBusiness) + '</div>'
    + '<div class="seats">Hjem: ' + cabinLine(t.inEconomy, t.inPremium, t.inBusiness) + ' · ' + pointsLine(t.inPoints, t.inBusiness) + '</div>'
    + '</div>';
}

function renderOpenJawTrip(t) {
  const distFromTarget = Math.abs(t.nights - 7);
  const badgeClass = distFromTarget === 0 ? "best" : "ok";
  const badgeLabel = distFromTarget === 0 ? "7 netter" : t.nights + " netter";
  const outName = DESTINATION_NAMES_JS[t.outDestination] || t.outDestination;
  const inName = DESTINATION_NAMES_JS[t.inDestination] || t.inDestination;
  return '<div class="trip">'
    + '<div class="trip-dates">Fly til ' + outName + ': ' + formatDateNoYear(t.outDate) + '</div>'
    + '<div class="trip-weekdays">' + t.outWeekday + '</div>'
    + '<div class="trip-dates">Hjem fra ' + inName + ': ' + formatDateNoYear(t.inDate) + '</div>'
    + '<div class="trip-weekdays">' + t.inWeekday + '</div>'
    + '<div class="trip-meta">' + t.workdays + ' fridager fra jobb nødvendig</div>'
    + '<span class="badge hub">' + (HUB_NAMES_JS[t.hub] || t.hub) + '</span>'
    + '<span class="badge ' + badgeClass + '">' + badgeLabel + '</span>'
    + connectionBadge(t.outConnection, "Ut") + connectionBadge(t.inConnection, "Hjem")
    + '<div class="seats">Ut: ' + cabinLine(t.outEconomy, t.outPremium, t.outBusiness)
    + ' · Hjem: ' + cabinLine(t.inEconomy, t.inPremium, t.inBusiness) + '</div>'
    + '</div>';
}

const CARRIER_NAMES = { SK: "SAS", KL: "KLM", AF: "Air France", DL: "Delta",
  UX: "Air Europa", MU: "China Eastern", CI: "China Airlines", GA: "Garuda Indonesia",
  KQ: "Kenya Airways", KE: "Korean Air", ME: "MEA", AR: "Aerolíneas Argentinas",
  AM: "Aeroméxico", RO: "TAROM", VN: "Vietnam Airlines", MF: "Xiamen Airlines",
  TP: "TAP Air Portugal" };

function renderConfirmedOfferLine(o) {
  const carrierName = CARRIER_NAMES[o.carrier] || o.carrier;
  const dirLabel = o.direction === "outbound" ? "Ut" : "Hjem";
  const stopsLabel = o.stops > 0 ? o.stops + " mellomlanding" : "direkte";
  return '<div class="offer-line">' + dirLabel + ' ' + formatDateNoYear(o.date) + ' '
    + o.departure + '–' + o.arrival + ' (' + stopsLabel + ') · ' + o.cabin
    + ' · ' + o.points.toLocaleString("no-NO").replace(/,/g, " ") + ' p · '
    + o.seats + ' seter · ' + carrierName + '</div>';
}

function renderConfirmedOffers(d) {
  if (!d.confirmedOffers) return "";
  const sas = d.confirmedOffers.sas || [];
  const partner = d.confirmedOffers.partner || [];
  if (sas.length === 0 && partner.length === 0) return "";
  let html = '<div class="confirmed-block"><h3>Bekreftet (ekte pris og tid)</h3>';
  if (sas.length) {
    html += '<div class="confirmed-group"><strong>SAS</strong>' + sas.map(renderConfirmedOfferLine).join("") + '</div>';
  }
  if (partner.length) {
    html += '<div class="confirmed-group"><strong>Partnerselskap</strong>' + partner.map(renderConfirmedOfferLine).join("") + '</div>';
  }
  html += '</div>';
  return html;
}

function renderDestinationCard(d) {
  const name = DESTINATION_NAMES_JS[d.destination] || d.destination;
  const summaryLine = d.bestSummary
    ? '<div class="dest-summary-line">Best: ' + d.bestSummary.nights + ' netter fra '
      + (HUB_NAMES_JS[d.bestSummary.hub] || d.bestSummary.hub)
      + (d.bestSummary.biz > 0 ? ' · ' + d.bestSummary.biz + ' business-seter totalt' : '') + '</div>'
    : '<div class="dest-summary-line" style="color:var(--muted);">Ingen 5–10 netters kombinasjon funnet ennå</div>';

  let hubsHtml = "";
  HUB_ORDER.forEach(hub => {
    const trips = d.hubs[hub];
    if (!trips || trips.length === 0) return;
    hubsHtml += '<div class="hub-block"><h3>Fra ' + (HUB_NAMES_JS[hub] || hub) + '</h3>'
      + trips.map(renderTrip).join("") + '</div>';
  });
  if (!hubsHtml) hubsHtml = '<div class="none">Ingen 5–10 netters alternativ funnet fra noen hub ennå.</div>';

  return '<details class="dest-card" data-name="' + name.toLowerCase() + '" data-code="' + d.destination + '">'
    + '<summary><h2>' + name + '</h2>'
    + '<div class="season">' + (d.season || "") + '</div>'
    + summaryLine + '</summary>'
    + renderConfirmedOffers(d)
    + hubsHtml
    + '</details>';
}

const HUB_ORDER = ["OSL", "CPH", "ARN"];
const MARKER_COLOR = { blue: "#378ADD", orange: "#EF9F27", green: "#639922" };

function openDestination(code) {
  document.getElementById("viewToggleList").click();
  requestAnimationFrame(() => {
    const el = document.querySelector('#destGrid details[data-code="' + code + '"]');
    if (!el) return;
    el.open = true;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

function renderMap() {
  const container = document.getElementById("mapContainer");
  container.innerHTML = "";
  const svg = d3.select(container).append("svg").attr("viewBox", "0 0 680 420").attr("width", "100%");
  const projection = d3.geoNaturalEarth1().center([15, 35]).scale(420).translate([340, 220]);
  const path = d3.geoPath(projection);
  const isDark = (document.documentElement.dataset.mode || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark";

  d3.json("https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json").then(world => {
    svg.selectAll("path").data(topojson.feature(world, world.objects.countries).features).join("path")
      .attr("d", path)
      .attr("fill", isDark ? "#2c2c2a" : "#e1e0d9")
      .attr("stroke", isDark ? "#1a1a19" : "#fcfcfb")
      .attr("stroke-width", 0.5);

    const points = DEST_VIEW.filter(d => d.lon !== null && d.lat !== null);
    svg.selectAll("circle").data(points).join("circle")
      .attr("cx", d => projection([d.lon, d.lat])[0])
      .attr("cy", d => projection([d.lon, d.lat])[1])
      .attr("r", 5)
      .attr("fill", d => MARKER_COLOR[d.marker] || "#898781")
      .attr("stroke", isDark ? "#1a1a19" : "#fcfcfb")
      .attr("stroke-width", 1.2)
      .style("cursor", "pointer")
      .on("click", (event, d) => openDestination(d.destination));
  });
}

function render() {
  const destGrid = document.getElementById("destGrid");
  destGrid.innerHTML = DEST_VIEW.length
    ? DEST_VIEW.map(renderDestinationCard).join("")
    : '<div class="none">Ingen destinasjoner funnet ennå.</div>';

  document.getElementById("searchBox").addEventListener("input", (e) => {
    const q = e.target.value.toLowerCase().trim();
    document.querySelectorAll("#destGrid details.dest-card").forEach(el => {
      el.style.display = el.dataset.name.includes(q) ? "" : "none";
    });
  });

  const listBtn = document.getElementById("viewToggleList");
  const mapBtn = document.getElementById("viewToggleMap");
  const destGridEl = document.getElementById("destGrid");
  const searchBoxEl = document.getElementById("searchBox");
  const mapContainerEl = document.getElementById("mapContainer");
  const mapLegendEl = document.getElementById("mapLegend");
  let mapRendered = false;

  listBtn.addEventListener("click", () => {
    listBtn.classList.add("active"); mapBtn.classList.remove("active");
    destGridEl.style.display = ""; searchBoxEl.style.display = "";
    mapContainerEl.style.display = "none"; mapLegendEl.style.display = "none";
  });
  mapBtn.addEventListener("click", () => {
    mapBtn.classList.add("active"); listBtn.classList.remove("active");
    destGridEl.style.display = "none"; searchBoxEl.style.display = "none";
    mapContainerEl.style.display = ""; mapLegendEl.style.display = "flex";
    if (!mapRendered) { renderMap(); mapRendered = true; }
  });

  const ojGrid = document.getElementById("openJawGrid");
  ojGrid.innerHTML = OPEN_JAW.length === 0
    ? '<div class="none">Ingen kombinasjoner funnet ennå.</div>'
    : "";
  OPEN_JAW.forEach(g => {
    const card = document.createElement("div");
    card.className = "card";
    const tripsHtml = g.trips.length
      ? g.trips.map(renderOpenJawTrip).join("")
      : '<div class="none">Ingen kombinasjoner i riktig lengde ennå.</div>';
    card.innerHTML = '<h2>' + g.group + '</h2>' + tripsHtml;
    ojGrid.appendChild(card);
  });
}

render();
</script>
</body>
</html>
"""


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    rows = load_latest_rows(conn)
    flight_times = load_flight_times(conn)
    points_lookup = load_points(conn)
    detailed_offers = load_detailed_offers(conn)
    conn.close()

    feeder_lookup = build_feeder_lookup(rows)
    connection_lookup = build_connection_lookup(flight_times)
    trips_by_hub_dest = build_trips(rows, feeder_lookup, connection_lookup, points_lookup)

    dest_view_payload = build_destination_view(trips_by_hub_dest, detailed_offers)
    open_jaw_payload = build_open_jaw_trips(rows, feeder_lookup, connection_lookup)

    page = (PAGE_TEMPLATE
            .replace("__GENERATED__", dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
            .replace("__DESTVIEW_JSON__", json.dumps(dest_view_payload, ensure_ascii=False))
            .replace("__OPENJAW_JSON__", json.dumps(open_jaw_payload, ensure_ascii=False))
            .replace("__DESTNAMES_JSON__", json.dumps(DESTINATION_NAMES, ensure_ascii=False))
            .replace("__HUBNAMES_JSON__", json.dumps(HUB_NAMES, ensure_ascii=False)))
    OUT_PATH.write_text(page, encoding="utf-8")
    print(f"Wrote {OUT_PATH} with {len(dest_view_payload)} destinations and {len(open_jaw_payload)} open-jaw groups")


if __name__ == "__main__":
    main()
