#!/usr/bin/env python3
"""XAU/USD-Kerzen (4H + 15min) aus dem kostenlosen Dukascopy-Datenfeed erzeugen.

Läuft in der GitHub Action, braucht keinen API-Key und schreibt data/xauusd.json.
Quelle: Dukascopy Bank SA (Genf), Spot XAU/USD, Bid-Preise.
- abgeschlossene Monate:  Stunden-Kerzen   (BID_candles_hour_1.bi5)
- abgeschlossene Tage:    Minuten-Kerzen   (BID_candles_min_1.bi5)
- heutiger Tag:           Ticks pro Stunde (HHh_ticks.bi5) -> eigene Minuten-Kerzen
Der Live-Kurs von gold-api.com dient nur zur Kontrolle der Preisskala.

MODE=cut    -> Stand 07:00 (Europe/Zurich); vor 07:00 und am Wochenende passiert nichts
MODE=latest -> aktuellste abgeschlossene Kerzen (manueller Lauf)
"""
import datetime as dt, json, lzma, os, struct, sys, time, urllib.error, urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
TZ_NAME = os.environ.get("TZ_NAME", "Europe/Zurich")
TZ = ZoneInfo(TZ_NAME)
RUN_TIME = os.environ.get("RUN_TIME", "07:00")
MODE = os.environ.get("MODE", "cut")
FEED = os.environ.get("DUKA_FEED", "https://datafeed.dukascopy.com/datafeed")
LIVE_URL = os.environ.get("LIVE_URL", "https://api.gold-api.com/price/XAU")
OUT = Path(os.environ.get("OUT_DIR", "data"))
SYMBOL, KEEP4, KEEP15 = "XAUUSD", 300, 1000
FMT = "%Y-%m-%d %H:%M:00"
HEADERS = {"User-Agent": "Mozilla/5.0 (xauusd-analyzer; GitHub Action)"}
CANDLE, TICK = ">5i1f", ">3i2f"   # Kerze: Zeit(s), Open, Close, Low, High, Volumen | Tick: Zeit(ms), Ask, Bid, AskVol, BidVol
requests_done = 0


def get(url: str) -> bytes:
    global requests_done
    err = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as r:
                requests_done += 1
                time.sleep(0.15)          # Server schonen
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return b""                # Datei (noch) nicht vorhanden
            err = e
        except Exception as e:            # Netzwerkfehler -> erneut versuchen
            err = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Download fehlgeschlagen: {url} ({err})")


def bi5(url: str, fmt: str) -> list:
    raw = get(url)
    if not raw:
        return []
    data = lzma.decompress(raw)
    size = struct.calcsize(fmt)
    return list(struct.iter_unpack(fmt, data[: len(data) // size * size]))


def day_path(d: dt.date) -> str:
    return f"{FEED}/{SYMBOL}/{d.year:04d}/{d.month - 1:02d}/{d.day:02d}"   # Monat ist 0-basiert


def candles(url: str, base: dt.datetime) -> list:
    b = base.timestamp()
    # -> (ms, open, high, low, close, volumen) als Rohwerte
    return [(int((b + t) * 1000), o, h, l, c, v) for t, o, c, l, h, v in bi5(url, CANDLE)]


def minutes_of_day(d: dt.date) -> list:
    return candles(f"{day_path(d)}/BID_candles_min_1.bi5", dt.datetime(d.year, d.month, d.day, tzinfo=UTC))


def hours_of_month(y: int, m: int) -> list:
    return candles(f"{FEED}/{SYMBOL}/{y:04d}/{m - 1:02d}/BID_candles_hour_1.bi5", dt.datetime(y, m, 1, tzinfo=UTC))


def minutes_from_ticks(d: dt.date, hour: int) -> list:
    base = int(dt.datetime(d.year, d.month, d.day, hour, tzinfo=UTC).timestamp() * 1000)
    bars = {}
    for ms, _ask, bid, _av, _bv in bi5(f"{day_path(d)}/{hour:02d}h_ticks.bi5", TICK):
        k = (base + ms) // 60000 * 60000
        b = bars.get(k)
        if b is None:
            bars[k] = [k, bid, bid, bid, bid, 1]
        else:
            b[2] = max(b[2], bid); b[3] = min(b[3], bid); b[4] = bid; b[5] += 1
    return [tuple(v) for v in bars.values()]


def aggregate(rows: list, start_of) -> dict:
    """Fasst Kerzen (ms,o,h,l,c,v) zu grösseren Kerzen zusammen; Kerzen ohne Volumen (Markt zu) fallen weg."""
    out = {}
    for ms, o, h, l, c, v in sorted(rows):
        if v <= 0:
            continue
        k = start_of(ms)
        b = out.get(k)
        if b is None:
            out[k] = [o, h, l, c]
        else:
            b[1] = max(b[1], h); b[2] = min(b[2], l); b[3] = c
    return out


def month_back(y: int, m: int, n: int):
    m -= n
    while m <= 0:
        m += 12; y -= 1
    return y, m


def live_price():
    try:
        with urllib.request.urlopen(urllib.request.Request(LIVE_URL, headers=HEADERS), timeout=15) as r:
            return float(json.load(r)["price"])
    except Exception as e:
        print(f"Hinweis: Live-Kurs von gold-api.com nicht abrufbar ({e})")
        return None


def pick_divisor(raw_close: int, live) -> int:
    """Dukascopy liefert Ganzzahlen. Teiler so wählen, dass der Kurs zum Live-Kurs bzw. zu einem Goldpreis passt."""
    options = [1000, 100, 10000, 10, 100000]
    if live:
        return min(options, key=lambda d: abs(raw_close / d - live))
    return next((d for d in options if 300 <= raw_close / d <= 30000), 1000)


def main() -> None:
    now = dt.datetime.now(TZ)
    target = OUT / "xauusd.json"
    mode = MODE if target.exists() else "latest"      # erster Lauf erzeugt immer Daten
    if mode == "cut":
        h, m = map(int, RUN_TIME.split(":"))
        cut = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now.weekday() >= 5 or now < cut:
            print(f"Übersprungen: {now:%a %H:%M} ({TZ_NAME}) ist Wochenende oder vor {RUN_TIME}.")
            return
    else:
        cut = now
    cut_utc = cut.astimezone(UTC)
    today = cut_utc.date()

    # 1) Stunden-Kerzen der letzten drei abgeschlossenen Monate (für 4H)
    hour_rows = []
    first_min_day = min(today - dt.timedelta(days=16), today.replace(day=1))
    for n in (3, 2, 1):
        y, m = month_back(today.year, today.month, n)
        rows = hours_of_month(y, m)
        if not rows and n == 1:          # Vormonat noch nicht veröffentlicht -> Tageswerte nehmen
            first_min_day = min(first_min_day, dt.date(y, m, 1))
        hour_rows += rows
    print(f"Stunden-Kerzen aus Monatsdateien: {len(hour_rows)}")

    # 2) Minuten-Kerzen: abgeschlossene Tage aus Tagesdateien, heute aus Tick-Dateien
    minute_rows, d = [], first_min_day
    while d < today:
        rows = minutes_of_day(d) if d.weekday() != 5 else []        # samstags kein Handel
        if not any(r[5] > 0 for r in rows) and d >= today - dt.timedelta(days=2) and d.weekday() != 5:
            for hh in range(24):                                     # Tagesdatei fehlt noch -> Ticks
                rows += minutes_from_ticks(d, hh)
        minute_rows += rows
        d += dt.timedelta(days=1)
    for hh in range(24):
        if dt.datetime(today.year, today.month, today.day, hh, tzinfo=UTC) >= cut_utc:
            break
        minute_rows += minutes_from_ticks(today, hh)
    print(f"Minuten-Kerzen: {len(minute_rows)} · Downloads: {requests_done}")
    if not minute_rows:
        sys.exit("Keine Daten von Dukascopy erhalten – Datei bleibt unverändert.")

    # 3) Preisskala bestimmen
    last_raw = max(minute_rows)[4]
    live = live_price()
    div = pick_divisor(last_raw, live)
    print(f"Preisteiler {div}: letzter Kurs {last_raw / div:.2f}" + (f", Live gold-api.com {live:.2f}" if live else ""))

    # 4) 15min-Kerzen
    m15 = aggregate(minute_rows, lambda ms: ms // 900000 * 900000)
    # 5) 4H-Kerzen (Zürich-Zeit 03/07/11/15/19/23 Uhr -> die 03-07-Kerze endet genau um 07:00)
    hours = aggregate(hour_rows, lambda ms: ms // 3600000 * 3600000)
    for k, b in aggregate(minute_rows, lambda ms: ms // 3600000 * 3600000).items():
        hours.setdefault(k, b)
    hour_list = [(k, *b, 1) for k, b in hours.items()]

    def start4(ms):
        loc = dt.datetime.fromtimestamp(ms / 1000, UTC).astimezone(TZ).replace(minute=0, second=0, microsecond=0)
        return int((loc - dt.timedelta(hours=(loc.hour - 3) % 4)).timestamp() * 1000)
    h4 = aggregate(hour_list, start4)

    def to_list(bars: dict, minutes: int, keep: int) -> list:
        out = []
        for k in sorted(bars):
            start = dt.datetime.fromtimestamp(k / 1000, UTC)
            end = start + dt.timedelta(minutes=minutes)
            if end > cut_utc:
                continue                  # nur abgeschlossene Kerzen
            o, h, l, c = (round(x / div, 3) for x in bars[k])
            out.append({"t": start.astimezone(TZ).strftime(FMT), "te": end.astimezone(TZ).strftime(FMT),
                        "o": o, "h": h, "l": l, "c": c})
        return out[-keep:]

    result = {"source": "XAU/USD Spot (Dukascopy, Bid)", "tz": TZ_NAME, "mode": mode,
              "cut": cut.strftime(FMT) if mode == "cut" else None, "generated": now.strftime("%Y-%m-%d %H:%M"),
              "live_check": live, "c4": to_list(h4, 240, KEEP4), "c15": to_list(m15, 15, KEEP15)}
    print(f"4H: {len(result['c4'])} Kerzen · 15min: {len(result['c15'])} Kerzen · letzte 15min {result['c15'][-1]['t'] if result['c15'] else '-'}")
    if len(result["c4"]) < 60 or len(result["c15"]) < 60:
        sys.exit("Zu wenige Kerzen – Datei wird nicht überschrieben.")

    OUT.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, separators=(",", ":"))
    target.write_text(payload)
    if mode == "cut":
        (OUT / "archive").mkdir(exist_ok=True)
        (OUT / "archive" / f"{now:%Y-%m-%d}.json").write_text(payload)
    print(f"Gespeichert: {target} (Stand {result['c15'][-1]['te']})")


if __name__ == "__main__":
    main()
