#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gassi-Wetter Magdeburg
======================
Zieht die stuendliche Wettervorhersage von Open-Meteo (kostenlos, kein API-Key),
bewertet fuer heute und morgen die besten Zeitfenster fuer Gassirunden und baut
ein mobiles HTML-Dashboard (index.html) im Magazin-Stil.

Zu vermeiden:
  - Regen (Wahrscheinlichkeit + Menge)
  - Hitze (Lufttemperatur als Naeherung fuer heissen Asphalt; Pfotenschutz)
  - Pralle Sonne (wenig Wolken bei Tag + hoher UV) -> Extra-Hinweis

Alle Schwellwerte stehen zentral in CONFIG (siehe unten) und lassen sich ohne
Code-Umbau nachjustieren.

Aufruf:
    python gassi_wetter.py                # schreibt ./public/index.html
    python gassi_wetter.py --out ./dist   # eigenes Zielverzeichnis
"""

from __future__ import annotations

import argparse
import html
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

# ---------------------------------------------------------------------------
# KONFIGURATION  --  hier gefahrlos nachjustieren
# ---------------------------------------------------------------------------
CONFIG = {
    # Fester Standort (keine GPS-Ortung noetig)
    "location": {"name": "Magdeburg", "lat": 52.1205, "lon": 11.6276},
    "timezone": "Europe/Berlin",

    # Nur Stunden in diesem Fenster gelten als sinnvolle Gassi-Zeit (0-23 Uhr).
    "walk_hours": {"start": 5, "end": 22},

    # Regen: Wahrscheinlichkeit in % und Menge in mm/h.
    "rain": {
        "prob_ok":  20,   # bis hier: unkritisch
        "prob_bad": 50,   # ab hier: schlecht
        "mm_bad":   0.5,  # ab dieser Regenmenge (mm/h): schlecht, egal welche %
    },

    # Hitze: Lufttemperatur in Grad C (Naeherung fuer heissen Asphalt/Pfoten).
    "heat": {
        "warn": 25,   # ab hier: mittel (Pfoten im Blick behalten)
        "bad":  30,   # ab hier: schlecht (Asphalt zu heiss)
    },

    # Pralle Sonne: wenig Wolken bei Tag -> Zusatz-Hinweis (Pfoten/Sonnenschutz).
    "sun": {
        "cloud_max": 30,  # Bewoelkung <= diesem Wert (%) gilt als "sonnig"
        "uv_warn":   6,   # UV-Index ab hier deutlich -> Hinweis
    },

    # Anzeige
    "days": 2,  # heute + morgen
}

# API
API_URL = "https://api.open-meteo.com/v1/forecast"
# Fairer, wiedererkennbarer User-Agent (Open-Meteo verlangt keinen Key, wir
# halten uns aber an gute Sitten).
USER_AGENT = "Gassi-Wetter/1.0 (+https://github.com/) privater Hunde-Planer"

# Bewertungs-Stufen (Rang: hoeher = schlechter)
GUT, MITTEL, SCHLECHT = "gut", "mittel", "schlecht"
RANK = {GUT: 0, MITTEL: 1, SCHLECHT: 2}
LABEL = {GUT: "Gut", MITTEL: "Mittel", SCHLECHT: "Schlecht"}

WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
              "Freitag", "Samstag", "Sonntag"]
MONATE = ["", "Januar", "Februar", "Maerz", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]


# ---------------------------------------------------------------------------
# WETTER ZIEHEN
# ---------------------------------------------------------------------------
def fetch_weather(cfg: dict) -> dict:
    """Holt die stuendliche Vorhersage von Open-Meteo (mit kleinem Retry)."""
    loc = cfg["location"]
    params = {
        "latitude": loc["lat"],
        "longitude": loc["lon"],
        "hourly": ",".join([
            "temperature_2m",
            "apparent_temperature",
            "precipitation_probability",
            "precipitation",
            "cloud_cover",
            "uv_index",
            "is_day",
        ]),
        "timezone": cfg["timezone"],
        "forecast_days": cfg["days"],
    }
    last_err = None
    for attempt in range(3):
        try:
            r = httpx.get(API_URL, params=params,
                          headers={"User-Agent": USER_AGENT}, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001  (Netz-/HTTP-Fehler -> Backoff)
            last_err = e
            wait = 2 ** attempt
            print(f"Warnung: Abruf fehlgeschlagen ({e}); erneut in {wait}s ...",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Open-Meteo nicht erreichbar: {last_err}")


# ---------------------------------------------------------------------------
# STUNDEN PARSEN + BEWERTEN
# ---------------------------------------------------------------------------
def parse_hours(data: dict, cfg: dict, now: datetime) -> list[dict]:
    """Wandelt die parallelen Arrays in eine Liste von Stunden-Dicts um.
    Vergangene Stunden (vor der aktuellen) werden verworfen."""
    h = data["hourly"]
    times = h["time"]
    out = []
    tz = ZoneInfo(cfg["timezone"])
    for i, t in enumerate(times):
        dt = datetime.fromisoformat(t).replace(tzinfo=tz)
        if dt < now.replace(minute=0, second=0, microsecond=0):
            continue  # Vergangenheit ueberspringen
        hour = dt.hour
        if not (cfg["walk_hours"]["start"] <= hour <= cfg["walk_hours"]["end"]):
            continue  # ausserhalb der Gassi-Zeiten
        out.append({
            "dt": dt,
            "hour": hour,
            "temp": _num(h["temperature_2m"][i]),
            "feels": _num(h["apparent_temperature"][i]),
            "rain_prob": _num(h["precipitation_probability"][i]),
            "rain_mm": _num(h["precipitation"][i]),
            "cloud": _num(h["cloud_cover"][i]),
            "uv": _num(h["uv_index"][i]),
            "is_day": bool(h["is_day"][i]),
        })
    return out


def _num(v, default=0.0):
    return default if v is None else v


def rate_hour(h: dict, cfg: dict) -> dict:
    """Bewertet eine Stunde -> Rang (gut/mittel/schlecht), Penalty & Hinweise."""
    rain, heat, sun = cfg["rain"], cfg["heat"], cfg["sun"]
    rating = GUT
    penalty = 0.0
    badges: list[str] = []

    # --- Regen ---  (Prozentwert steht in der Metrik-Kachel, nicht im Badge)
    if h["rain_mm"] >= rain["mm_bad"] or h["rain_prob"] >= rain["prob_bad"]:
        rating = _worse(rating, SCHLECHT)
        badges.append("🌧️ Regen")
    elif h["rain_prob"] >= rain["prob_ok"]:
        rating = _worse(rating, MITTEL)
        badges.append("🌦️ Schauer moeglich")
    penalty += h["rain_prob"] + h["rain_mm"] * 30

    # --- Hitze (Asphalt-Naeherung; Temperatur steht in der Metrik-Kachel) ---
    if h["temp"] >= heat["bad"]:
        rating = _worse(rating, SCHLECHT)
        badges.append("🌡️ Asphalt zu heiss")
        penalty += (h["temp"] - heat["warn"]) * 4 + 30
    elif h["temp"] >= heat["warn"]:
        rating = _worse(rating, MITTEL)
        badges.append("🌡️ Warm — Pfoten pruefen")
        penalty += (h["temp"] - heat["warn"]) * 4

    # --- Pralle Sonne (Zusatz-Hinweis, verschlechtert nicht allein) ---
    if h["is_day"] and h["cloud"] <= sun["cloud_max"] and h["uv"] >= sun["uv_warn"]:
        badges.append(f"☀️ Pralle Sonne (UV {round(h['uv'])})")
        penalty += 5
        if h["temp"] >= heat["warn"] and rating == GUT:
            rating = MITTEL  # sonnig + schon warm -> nicht mehr "gut"

    h["rating"] = rating
    h["penalty"] = penalty
    h["badges"] = badges
    return h


def _worse(a: str, b: str) -> str:
    return a if RANK[a] >= RANK[b] else b


# ---------------------------------------------------------------------------
# FENSTER BAUEN (aufeinanderfolgende Stunden gleicher Guete zusammenfassen)
# ---------------------------------------------------------------------------
def build_windows(hours: list[dict]) -> list[dict]:
    windows: list[dict] = []
    for h in hours:
        if windows and windows[-1]["rating"] == h["rating"] \
                and h["hour"] == windows[-1]["end_hour"] + 1:
            _extend_window(windows[-1], h)
        else:
            windows.append(_new_window(h))
    for w in windows:
        _finalize_window(w)
    return windows


def _new_window(h: dict) -> dict:
    return {
        "rating": h["rating"],
        "start_hour": h["hour"],
        "end_hour": h["hour"],
        "temps": [h["temp"]],
        "rain_probs": [h["rain_prob"]],
        "clouds": [h["cloud"]],
        "penalties": [h["penalty"]],
        "badges": list(h["badges"]),
    }


def _extend_window(w: dict, h: dict) -> None:
    w["end_hour"] = h["hour"]
    w["temps"].append(h["temp"])
    w["rain_probs"].append(h["rain_prob"])
    w["clouds"].append(h["cloud"])
    w["penalties"].append(h["penalty"])
    for b in h["badges"]:
        # Nur eine Auspraegung je Hinweis-Typ (erstes Emoji als Schluessel)
        key = b.split(" ")[0]
        if not any(x.startswith(key) for x in w["badges"]):
            w["badges"].append(b)


def _finalize_window(w: dict) -> None:
    n = len(w["temps"])
    w["temp_min"] = round(min(w["temps"]))
    w["temp_max"] = round(max(w["temps"]))
    w["rain_max"] = int(max(w["rain_probs"]))
    w["cloud_avg"] = round(sum(w["clouds"]) / n)
    w["avg_penalty"] = sum(w["penalties"]) / n
    w["length"] = w["end_hour"] - w["start_hour"] + 1


def pick_best(windows: list[dict]) -> dict | None:
    """Bestes Fenster eines Tages: bevorzugt 'gut', dann geringste Penalty,
    dann laengstes, dann frueheste Uhrzeit."""
    if not windows:
        return None
    return sorted(
        windows,
        key=lambda w: (RANK[w["rating"]], w["avg_penalty"],
                       -w["length"], w["start_hour"]),
    )[0]


# ---------------------------------------------------------------------------
# TAGE GRUPPIEREN
# ---------------------------------------------------------------------------
def group_days(hours: list[dict], now: datetime) -> list[dict]:
    today = now.date()
    by_date: dict = {}
    for h in hours:
        by_date.setdefault(h["dt"].date(), []).append(h)

    days = []
    for d in sorted(by_date):
        offset = (d - today).days
        if offset == 0:
            label = "Heute"
        elif offset == 1:
            label = "Morgen"
        else:
            label = WOCHENTAGE[d.weekday()]
        windows = build_windows(by_date[d])
        days.append({
            "date": d,
            "label": label,
            "weekday": WOCHENTAGE[d.weekday()],
            "date_str": f"{d.day}. {MONATE[d.month]}",
            "windows": windows,
            "best": pick_best(windows),
        })
    return days


# ---------------------------------------------------------------------------
# HTML BAUEN
# ---------------------------------------------------------------------------
CSS = """
*{box-sizing:border-box;margin:0;padding:0;}
:root{
  --paper:#15120F; --card:#1E1A16; --ink:#F2EDE5; --muted:#A79E90;
  --faint:#6E665B; --line:#2C2823; --chip:#26221D;
  /* Status-Palette (Icon+Label sichern Bedeutung, nie Farbe allein) */
  --good:#0ca30c; --good-ink:#7ed99a;
  --warn:#fab219; --warn-ink:#f7c96b;
  --bad:#d03b3b;  --bad-ink:#eb8f8f;
  --serif:"Iowan Old Style","Palatino Linotype","Book Antiqua",Georgia,"Times New Roman",serif;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
}
html{-webkit-text-size-adjust:100%;}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);
  line-height:1.5;padding:20px 16px 48px;max-width:720px;margin:0 auto;
  -webkit-font-smoothing:antialiased;}

/* Kopf */
.masthead{border-bottom:2px solid var(--ink);padding-bottom:18px;margin-bottom:22px;}
.kicker{font-size:12px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--warn-ink);font-weight:600;margin-bottom:8px;}
.title{font-family:var(--serif);font-weight:600;font-size:clamp(34px,9vw,52px);
  line-height:1.02;letter-spacing:-.01em;}
.sub{color:var(--muted);font-size:14px;margin-top:8px;}

/* Legende */
.legend{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 26px;}
.lg{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;
  color:var(--muted);background:var(--chip);border:1px solid var(--line);
  border-radius:999px;padding:5px 11px 5px 9px;}
.dot{width:10px;height:10px;border-radius:50%;flex:none;}

/* Tag */
.day{margin-bottom:34px;}
.day-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
  border-bottom:1px solid var(--line);padding-bottom:8px;margin-bottom:14px;}
.day-label{font-family:var(--serif);font-size:clamp(24px,6vw,30px);font-weight:600;}
.day-date{color:var(--muted);font-size:14px;}

/* Beste-Zeit-Banner */
.best{display:flex;align-items:center;gap:12px;background:var(--chip);
  border:1px solid var(--line);border-left:4px solid var(--good);
  border-radius:12px;padding:12px 14px;margin-bottom:16px;}
.best--none{border-left-color:var(--bad);}
.best__ic{font-size:22px;flex:none;}
.best__t{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);}
.best__v{font-family:var(--serif);font-size:20px;font-weight:600;margin-top:1px;}

/* Fenster-Karten */
.cards{display:flex;flex-direction:column;gap:10px;}
.card{position:relative;background:var(--card);border:1px solid var(--line);
  border-left:5px solid var(--c);border-radius:14px;padding:13px 15px;
  background:color-mix(in srgb, var(--c) 9%, var(--card));}
.card.gut{--c:var(--good);} .card.mittel{--c:var(--warn);} .card.schlecht{--c:var(--bad);}
.card__top{display:flex;align-items:center;justify-content:space-between;gap:12px;}
.card__time{font-family:var(--serif);font-weight:600;font-size:22px;
  font-variant-numeric:tabular-nums;letter-spacing:-.01em;}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;
  padding:3px 10px;border-radius:999px;white-space:nowrap;}
.card.gut .pill{background:color-mix(in srgb,var(--good) 22%,transparent);color:var(--good-ink);}
.card.mittel .pill{background:color-mix(in srgb,var(--warn) 22%,transparent);color:var(--warn-ink);}
.card.schlecht .pill{background:color-mix(in srgb,var(--bad) 22%,transparent);color:var(--bad-ink);}
.pill .dot{width:8px;height:8px;}

.metrics{display:flex;gap:18px;flex-wrap:wrap;margin-top:11px;}
.metric{display:flex;flex-direction:column;gap:1px;}
.metric__v{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums;}
.metric__k{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--faint);}

.badges{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px;}
.badge{font-size:12px;color:var(--ink);background:var(--chip);
  border:1px solid var(--line);border-radius:8px;padding:4px 9px;}

/* Fuss */
.foot{border-top:1px solid var(--line);margin-top:8px;padding-top:16px;
  color:var(--faint);font-size:12.5px;line-height:1.7;}
.foot a{color:var(--muted);}
"""


def _time_range(w: dict) -> str:
    if w["start_hour"] == w["end_hour"]:
        return f"{w['start_hour']:02d} Uhr"
    # Ende zeigt das Ende der letzten Stunde -> +1
    return f"{w['start_hour']:02d} – {w['end_hour'] + 1:02d} Uhr"


def _card_html(w: dict) -> str:
    esc = html.escape
    badges = "".join(
        f'<span class="badge">{esc(b)}</span>' for b in w["badges"]
    )
    badges_block = f'<div class="badges">{badges}</div>' if badges else ""
    temp = (f"{w['temp_min']}°" if w["temp_min"] == w["temp_max"]
            else f"{w['temp_min']}–{w['temp_max']}°")
    return f"""
      <article class="card {w['rating']}">
        <div class="card__top">
          <div class="card__time">{_time_range(w)}</div>
          <span class="pill"><span class="dot" style="background:var(--c)"></span>{LABEL[w['rating']]}</span>
        </div>
        <div class="metrics">
          <div class="metric"><span class="metric__v">{temp} C</span><span class="metric__k">Temperatur</span></div>
          <div class="metric"><span class="metric__v">{w['rain_max']} %</span><span class="metric__k">Regen</span></div>
          <div class="metric"><span class="metric__v">{w['cloud_avg']} %</span><span class="metric__k">Wolken</span></div>
        </div>
        {badges_block}
      </article>"""


def _best_banner(day: dict) -> str:
    best = day["best"]
    if best and best["rating"] in (GUT, MITTEL):
        cls = "" if best["rating"] == GUT else ' style="border-left-color:var(--warn)"'
        ic = "🐾" if best["rating"] == GUT else "👍"
        return f"""
      <div class="best"{cls}>
        <span class="best__ic">{ic}</span>
        <div>
          <div class="best__t">Beste Zeit</div>
          <div class="best__v">{_time_range(best)}</div>
        </div>
      </div>"""
    return """
      <div class="best best--none">
        <span class="best__ic">🚫</span>
        <div>
          <div class="best__t">Beste Zeit</div>
          <div class="best__v">Heute kein gutes Fenster — kurz halten</div>
        </div>
      </div>"""


def _day_html(day: dict) -> str:
    if not day["windows"]:
        cards = '<p class="sub">Keine Vorhersagedaten im Gassi-Zeitfenster.</p>'
        banner = ""
    else:
        cards = "".join(_card_html(w) for w in day["windows"])
        banner = _best_banner(day)
    return f"""
    <section class="day">
      <div class="day-head">
        <h2 class="day-label">{day['label']}</h2>
        <span class="day-date">{day['weekday']}, {day['date_str']}</span>
      </div>
      {banner}
      <div class="cards">{cards}</div>
    </section>"""


def build_html(days: list[dict], cfg: dict, now: datetime) -> str:
    loc = cfg["location"]["name"]
    stand = now.strftime("%d.%m.%Y, %H:%M Uhr")
    days_html = "".join(_day_html(d) for d in days)
    wh = cfg["walk_hours"]
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<meta name="color-scheme" content="dark">
<title>Gassi-Wetter {loc}</title>
<style>{CSS}</style>
</head>
<body>
  <header class="masthead">
    <div class="kicker">🐕 Gassi-Planer</div>
    <h1 class="title">{loc}</h1>
    <p class="sub">Beste Zeitfenster fuer die Hunderunde &middot; Stand {stand}</p>
  </header>

  <div class="legend">
    <span class="lg"><span class="dot" style="background:var(--good)"></span>Gut — raus damit</span>
    <span class="lg"><span class="dot" style="background:var(--warn)"></span>Mittel — geht, aufpassen</span>
    <span class="lg"><span class="dot" style="background:var(--bad)"></span>Schlecht — lieber meiden</span>
  </div>

  {days_html}

  <footer class="foot">
    Gassi-Zeiten {wh['start']}–{wh['end']} Uhr &middot;
    Hitze-Schwelle {cfg['heat']['warn']} °C (warm) / {cfg['heat']['bad']} °C (heiss) &middot;
    Regen-Schwelle {cfg['rain']['prob_bad']} %<br>
    Wetterdaten: <a href="https://open-meteo.com/">Open-Meteo</a> (kostenlos, ohne Gewaehr) &middot;
    Automatische Aktualisierung taeglich am Morgen.
  </footer>
</body>
</html>"""


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Gassi-Wetter Dashboard bauen")
    ap.add_argument("--out", default="public",
                    help="Zielverzeichnis fuer index.html (Standard: public)")
    args = ap.parse_args()

    tz = ZoneInfo(CONFIG["timezone"])
    now = datetime.now(tz)

    print(f"Ziehe Wetter fuer {CONFIG['location']['name']} ...")
    data = fetch_weather(CONFIG)

    hours = [rate_hour(h, CONFIG) for h in parse_hours(data, CONFIG, now)]
    days = group_days(hours, now)
    print(f"  {len(hours)} Gassi-Stunden, {len(days)} Tage aufbereitet.")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "index.html"
    out_file.write_text(build_html(days, CONFIG, now), encoding="utf-8")
    print(f"OK: {out_file} geschrieben.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
