#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gassi-Wetter Magdeburg
======================
Zieht die stuendliche Wettervorhersage von Open-Meteo (kostenlos, kein API-Key),
bewertet für heute und morgen die besten Zeitfenster für Gassirunden und baut
ein mobiles HTML-Dashboard (index.html) im Magazin-Stil.

Zu vermeiden:
  - Regen (Wahrscheinlichkeit + Menge)
  - Gewitter / Schnee / Glatteis (Wettercode -> harter Ausschluss)
  - Hitze (Lufttemperatur als Naeherung für heissen Asphalt; Pfotenschutz)
  - Kaelte (gefühlte Temperatur/Windchill) + Glaettegefahr bei Frost & Naesse
  - Wind / Sturmboeen
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
import json
import math
import os
import re
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

    # Hitze: Lufttemperatur in Grad C (Naeherung für heissen Asphalt/Pfoten).
    "heat": {
        "warn": 25,   # ab hier: mittel (Pfoten im Blick behalten)
        "bad":  30,   # ab hier: schlecht (Asphalt zu heiss)
    },

    # Kaelte: bewertet die GEFUEHLTE Temperatur (inkl. Windchill) in Grad C.
    "cold": {
        "warn":  0,   # ab hier abwaerts: mittel (Pfoten schuetzen)
        "bad":  -10,  # ab hier abwaerts: schlecht (eisig, nur kurz raus)
    },

    # Glaette: ab/unter dieser Lufttemperatur (Grad C) + Naesse/Schnee -> Hinweis.
    "glaette_temp": 1,

    # Wind: Boeen in km/h.
    "wind": {
        "gust_warn": 45,  # ab hier: mittel (boeig)
        "gust_bad":  60,  # ab hier: schlecht (Sturmboeen)
    },

    # Schauer-/Gewitterrisiko ueber CAPE (labile Schichtung, J/kg).
    # Hohe Werte heissen: Schauer koennen lokal aus dem Nichts entstehen, die
    # Stundenvorhersage ist dann unsicherer als sonst.
    # Grobe Einordnung: <300 stabil, 300-1000 maessig labil, >1000 deutlich.
    "convective": {
        "cape_warn": 300,   # ab hier: Hinweis "Schauerrisiko"
        "cape_high": 800,   # ab hier + etwas Regenwahrscheinlichkeit: nicht mehr "gut"
    },

    # Pralle Sonne: wenig Wolken bei Tag -> Zusatz-Hinweis (Pfoten/Sonnenschutz).
    "sun": {
        "cloud_max": 30,  # Bewoelkung <= diesem Wert (%) gilt als "sonnig"
        "uv_warn":   6,   # UV-Index ab hier deutlich -> Hinweis
    },

    # Anzeige
    "days": 2,       # detaillierte Tage mit Karten (heute + morgen)
    "week_days": 7,  # kompakter Wochen-Ausblick unten
    "rain_horizon_h": 6,  # Vorausschau fuer den Naechster-Regen-Hinweis (Std)

    # Oeffentliche Adresse der Web-App (fuer den Klick in der Push-Nachricht
    # UND als Ablage des "heute schon benachrichtigt"-Markers).
    "site_url": "https://hannibal2404.github.io/Wetter/",

    # Ab diesem Alter (Stunden) warnt die Seite selbst, dass die Daten alt sind.
    # Faengt JEDE Ursache ab: abgelaufenes Token, toter Cron-Dienst, API-Ausfall.
    "stale_warn_h": 6,

    # Morgen-Push nur in diesem lokalen Zeitfenster (Stunden, Ende exklusiv).
    # Bewusst WEIT gefasst: GitHub-Cron laeuft nachweislich bis zu ~5 Stunden
    # zu spaet (beobachtet: 14:23 geplant -> 19:11 gelaufen). Ein enges Fenster
    # wuerde einen verspaeteten Morgenlauf stumm schalten - lieber eine spaete
    # Nachricht als gar keine. Der Dedupe-Marker haelt es trotzdem bei EINER
    # Nachricht pro Tag. Ende 14 Uhr, damit die Nachmittagslaeufe still bleiben.
    "notify_window": {"start": 4, "end": 14},
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
WOCHENTAGE_ABBR = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONATE = ["", "Januar", "Februar", "Maerz", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]
COMPASS = ["N", "NO", "O", "SO", "S", "SW", "W", "NW"]

# WMO-Wettercodes (Open-Meteo), gruppiert fuer harte Ausschluesse.
WX_THUNDER = {95, 96, 99}                  # Gewitter (ggf. mit Hagel)
WX_SNOW    = {71, 73, 75, 77, 85, 86}      # Schneefall / Schneeschauer
WX_FREEZE  = {56, 57, 66, 67}              # gefrierender Niesel/Regen -> Glatteis
WX_FOG     = {45, 48}                       # Nebel / Reifnebel
WX_WET     = {51, 53, 55, 61, 63, 65,       # Niesel + Regen (fuer Glaette-Kombi)
              80, 81, 82}


def wx_info(code: int) -> tuple[str, int]:
    """WMO-Code -> (Icon-Art, Schweregrad). Die Art waehlt das animierte SVG;
    der Schweregrad bestimmt, welches Icon ein Fenster praegt (schlimmste Stunde)."""
    c = int(code)
    if c in WX_THUNDER:               return ("thunder", 6)
    if c in WX_SNOW:                  return ("snow", 5)
    if c in WX_FREEZE:                return ("snow", 5)   # Glatteis
    if c in {61, 63, 65, 80, 81, 82}: return ("rain", 4)
    if c in {51, 53, 55}:             return ("rain", 3)   # Niesel
    if c in WX_FOG:                   return ("fog", 2)
    if c == 3:                        return ("cloudy", 1)
    if c == 2:                        return ("partly", 1)
    return ("clear", 0)               # 0/1 = klar bis heiter


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
            "weather_code",
            "cloud_cover",
            "uv_index",
            "wind_gusts_10m",
            "wind_direction_10m",
            "cape",
            "is_day",
        ]),
        "daily": ",".join([
            "weather_code",
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_probability_max",
            "sunrise",
            "sunset",
        ]),
        "minutely_15": "precipitation",
        "timezone": cfg["timezone"],
        "forecast_days": cfg.get("week_days", cfg["days"]),
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
    Vergangene Stunden bleiben erhalten (fuer den Stunden-Streifen), werden aber
    als 'past' markiert; die aktuelle Stunde als 'is_now'."""
    h = data["hourly"]
    times = h["time"]
    out = []
    tz = ZoneInfo(cfg["timezone"])
    now_floor = now.replace(minute=0, second=0, microsecond=0)
    n = len(times)
    for i, t in enumerate(times):
        dt = datetime.fromisoformat(t).replace(tzinfo=tz)
        hour = dt.hour
        if not (cfg["walk_hours"]["start"] <= hour <= cfg["walk_hours"]["end"]):
            continue  # ausserhalb der Gassi-Zeiten
        # WICHTIG: Open-Meteo gibt Niederschlag (und den daraus abgeleiteten
        # weather_code) als Summe der VORANGEGANGENEN Stunde aus. Der Wert mit
        # Zeitstempel 17:00 beschreibt also 16:00-17:00. Fuer die Stunde ab `dt`
        # ist deshalb der Eintrag i+1 der richtige - sonst waere die Regen-
        # bewertung um eine Stunde verschoben (empirisch mit 15-Minuten-Daten
        # bestaetigt). Temperatur/Wolken/Wind sind dagegen Momentanwerte.
        nxt = i + 1 if i + 1 < n else i
        out.append({
            "dt": dt,
            "hour": hour,
            "past": dt < now_floor,
            "is_now": dt == now_floor,
            "temp": _num(h["temperature_2m"][i]),
            "feels": _num(h["apparent_temperature"][i]),
            "rain_prob": _num(h["precipitation_probability"][nxt]),
            "rain_mm": _num(h["precipitation"][nxt]),
            "wcode": int(_num(h["weather_code"][nxt])),
            "cloud": _num(h["cloud_cover"][i]),
            "uv": _num(h["uv_index"][i]),
            "gust": _num(h["wind_gusts_10m"][i]),
            "wind_dir": _num(h["wind_direction_10m"][i]),
            "cape": _num(h["cape"][i]),   # Momentanwert -> Index i
            "is_day": bool(h["is_day"][i]),
        })
    return out


def _num(v, default=0.0):
    return default if v is None else v


def _compass(deg: float) -> str:
    """Grad -> 8-Punkt-Himmelsrichtung (aus welcher der Wind kommt)."""
    return COMPASS[int((deg % 360) / 45 + 0.5) % 8]


def _circular_mean(degs: list[float]) -> float:
    """Mittelwert von Winkeln (Windrichtungen) korrekt ueber den 0/360-Sprung."""
    if not degs:
        return 0.0
    s = sum(math.sin(math.radians(d)) for d in degs)
    c = sum(math.cos(math.radians(d)) for d in degs)
    return math.degrees(math.atan2(s, c)) % 360


def parse_daily(data: dict, cfg: dict, now: datetime) -> list[dict]:
    """Tages-Zusammenfassung fuer den 7-Tage-Ausblick + Sonnenauf-/untergang."""
    d = data.get("daily")
    if not d:
        return []
    tz = ZoneInfo(cfg["timezone"])
    today = now.date()
    out = []
    for i, ds in enumerate(d["time"]):
        date = datetime.fromisoformat(ds).date()
        sr = datetime.fromisoformat(d["sunrise"][i]).replace(tzinfo=tz)
        ss = datetime.fromisoformat(d["sunset"][i]).replace(tzinfo=tz)
        off = (date - today).days
        abbr = WOCHENTAGE_ABBR[date.weekday()]
        out.append({
            "date": date,
            "abbr": "Heute" if off == 0 else ("Morgen" if off == 1 else abbr),
            "kind": wx_info(int(_num(d["weather_code"][i])))[0],
            "tmax": round(_num(d["temperature_2m_max"][i])),
            "tmin": round(_num(d["temperature_2m_min"][i])),
            "prob": int(_num(d["precipitation_probability_max"][i])),
            "sunrise": sr, "sunset": ss,
            "sunrise_str": sr.strftime("%H:%M"),
            "sunset_str": ss.strftime("%H:%M"),
        })
    return out


def parse_rain_outlook(data: dict, cfg: dict, now: datetime):
    """15-Minuten-Regen -> naechster Regenbeginn bzw. naechste Regenpause.
    Rueckgabe: (art, zeit) oder None. art: rain_soon/dry_soon/rain_hold/dry."""
    m = data.get("minutely_15")
    if not m or "precipitation" not in m:
        return None
    tz = ZoneInfo(cfg["timezone"])
    now_min = now.replace(second=0, microsecond=0)
    horizon = now_min + timedelta(hours=cfg.get("rain_horizon_h", 6))
    seq = []
    for t, p in zip(m["time"], m["precipitation"]):
        dt = datetime.fromisoformat(t).replace(tzinfo=tz)
        if dt < now_min:
            continue
        if dt > horizon:
            break
        seq.append((dt, _num(p)))
    if not seq:
        return None
    if seq[0][1] > 0:  # es regnet gerade
        for dt, p in seq:
            if p == 0:
                return ("dry_soon", dt)
        return ("rain_hold", None)
    for dt, p in seq:  # es ist trocken
        if p > 0:
            return ("rain_soon", dt)
    return ("dry", None)


def rate_hour(h: dict, cfg: dict) -> dict:
    """Bewertet eine Stunde -> Rang (gut/mittel/schlecht), Penalty & Hinweise.
    Zahlen (Temp, Regen%, Boeen) stehen in den Metrik-Kacheln; Badges bleiben
    qualitativ, damit sie nie einem Kachelwert widersprechen."""
    rain, heat, cold = cfg["rain"], cfg["heat"], cfg["cold"]
    sun, wind = cfg["sun"], cfg["wind"]
    code = h["wcode"]
    rating = GUT
    penalty = 0.0
    badges: list[str] = []

    # Wetter-Icon-Art + Schweregrad (fuer die Fenster-Darstellung).
    h["wx_kind"], h["wx_sev"] = wx_info(code)

    # --- Harte Ausschluesse per Wettercode ---
    if code in WX_THUNDER:
        rating = SCHLECHT; badges.append("⛈️ Gewitter"); penalty += 60
    elif code in WX_SNOW:
        rating = SCHLECHT; badges.append("🌨️ Schnee"); penalty += 45
    elif code in WX_FREEZE:
        rating = SCHLECHT; badges.append("🧊 Glatteis"); penalty += 50
    elif code in WX_FOG:
        rating = _worse(rating, MITTEL); badges.append("🌫️ Nebel"); penalty += 10

    # --- Regen ---
    if h["rain_mm"] >= rain["mm_bad"] or h["rain_prob"] >= rain["prob_bad"]:
        rating = _worse(rating, SCHLECHT)
        if not any(b[0] in "⛈🌨🧊" for b in badges):
            badges.append("🌧️ Regen")
    elif h["rain_prob"] >= rain["prob_ok"]:
        rating = _worse(rating, MITTEL)
        if not any(b[0] in "⛈🌨🧊🌧" for b in badges):
            badges.append("🌦️ Schauer möglich")
    penalty += h["rain_prob"] + h["rain_mm"] * 30

    # --- Schauerrisiko (labile Luft) ---
    # Bewusst zurueckhaltend: hohe CAPE allein macht aus "gut" kein "schlecht"
    # (labil heisst nur "koennte", nicht "wird"). Der Hinweis warnt aber davor,
    # der Stundenvorhersage an solchen Tagen zu sehr zu vertrauen.
    cv = cfg["convective"]
    if h["cape"] >= cv["cape_warn"] and code not in WX_THUNDER:
        badges.append("⚡ Schauerrisiko — Vorhersage unsicher")
        penalty += 6
        if h["cape"] >= cv["cape_high"] and h["rain_prob"] >= 10:
            rating = _worse(rating, MITTEL)

    # --- Hitze (Lufttemperatur als Asphalt-Naeherung) ---
    if h["temp"] >= heat["bad"]:
        rating = _worse(rating, SCHLECHT)
        badges.append("🌡️ Asphalt zu heiß")
        penalty += (h["temp"] - heat["warn"]) * 4 + 30
    elif h["temp"] >= heat["warn"]:
        rating = _worse(rating, MITTEL)
        badges.append("🌡️ Warm — Pfoten prüfen")
        penalty += (h["temp"] - heat["warn"]) * 4

    # --- Kaelte (gefuehlte Temperatur / Windchill) ---
    if h["feels"] <= cold["bad"]:
        rating = _worse(rating, SCHLECHT)
        badges.append("🥶 Eisig — nur kurz raus")
        penalty += (cold["warn"] - h["feels"]) * 3 + 20
    elif h["feels"] <= cold["warn"]:
        rating = _worse(rating, MITTEL)
        badges.append("🥶 Kalt — Pfoten schützen")
        penalty += (cold["warn"] - h["feels"]) * 3

    # --- Glaette (Frost + Naesse/Schnee), falls nicht schon Glatteis-Code ---
    if h["temp"] <= cfg["glaette_temp"] and code not in WX_FREEZE \
            and (h["rain_mm"] > 0 or code in WX_SNOW or code in WX_WET):
        rating = _worse(rating, MITTEL)
        if not any(b[0] == "🧊" for b in badges):
            badges.append("🧊 Glättegefahr")
        penalty += 15

    # --- Wind / Boeen ---
    if h["gust"] >= wind["gust_bad"]:
        rating = _worse(rating, SCHLECHT)
        badges.append("💨 Sturmböen")
        penalty += (h["gust"] - wind["gust_warn"]) * 1.5 + 15
    elif h["gust"] >= wind["gust_warn"]:
        rating = _worse(rating, MITTEL)
        badges.append("💨 Böig")
        penalty += (h["gust"] - wind["gust_warn"]) * 1.5

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
        "feels": [h["feels"]],
        "rain_probs": [h["rain_prob"]],
        "rain_mms": [h["rain_mm"]],
        "capes": [h["cape"]],
        "clouds": [h["cloud"]],
        "gusts": [h["gust"]],
        "dirs": [h["wind_dir"]],
        "penalties": [h["penalty"]],
        "badges": list(h["badges"]),
        "sev": h["wx_sev"],
        "kind": h["wx_kind"],
    }


def _extend_window(w: dict, h: dict) -> None:
    w["end_hour"] = h["hour"]
    w["temps"].append(h["temp"])
    w["feels"].append(h["feels"])
    w["rain_probs"].append(h["rain_prob"])
    w["rain_mms"].append(h["rain_mm"])
    w["capes"].append(h["cape"])
    w["clouds"].append(h["cloud"])
    w["gusts"].append(h["gust"])
    w["dirs"].append(h["wind_dir"])
    w["penalties"].append(h["penalty"])
    if h["wx_sev"] > w["sev"]:          # schlimmstes Wetter praegt das Icon
        w["sev"], w["kind"] = h["wx_sev"], h["wx_kind"]
    for b in h["badges"]:
        # Nur eine Auspraegung je Hinweis-Typ (erstes Emoji als Schluessel)
        key = b.split(" ")[0]
        if not any(x.startswith(key) for x in w["badges"]):
            w["badges"].append(b)


def _finalize_window(w: dict) -> None:
    n = len(w["temps"])
    w["temp_min"] = round(min(w["temps"]))
    w["temp_max"] = round(max(w["temps"]))
    w["feels_avg"] = round(sum(w["feels"]) / n)
    w["rain_max"] = int(max(w["rain_probs"]))
    # Spitzenintensitaet (mm/h), nicht die Summe: sagt "wie nass werde ich",
    # ohne bei langen Fenstern zu dramatisieren.
    w["mm_max"] = max(w["rain_mms"])
    w["cape_max"] = round(max(w["capes"]))
    w["cloud_avg"] = round(sum(w["clouds"]) / n)
    w["gust_max"] = round(max(w["gusts"]))
    w["wind_dir"] = round(_circular_mean(w["dirs"]))
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
def group_days(hours: list[dict], cfg: dict, now: datetime,
               daily_by_date: dict) -> list[dict]:
    today = now.date()
    by_date: dict = {}
    for h in hours:
        by_date.setdefault(h["dt"].date(), []).append(h)

    days = []
    for d in sorted(by_date)[:cfg["days"]]:   # nur die Detail-Tage
        offset = (d - today).days
        if offset == 0:
            label = "Heute"
        elif offset == 1:
            label = "Morgen"
        else:
            label = WOCHENTAGE[d.weekday()]
        day_hours = by_date[d]
        # Karten/Fenster nur aus zukuenftigen Stunden; Streifen zeigt alles.
        windows = build_windows([h for h in day_hours if not h["past"]])
        dsum = daily_by_date.get(d, {})
        days.append({
            "date": d,
            "label": label,
            "weekday": WOCHENTAGE[d.weekday()],
            "date_str": f"{d.day}. {MONATE[d.month]}",
            "is_today": offset == 0,
            "hours_all": day_hours,
            "windows": windows,
            "best": pick_best(windows),
            "sunrise": dsum.get("sunrise"),
            "sunset": dsum.get("sunset"),
            "sunrise_str": dsum.get("sunrise_str"),
            "sunset_str": dsum.get("sunset_str"),
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
.card__time{display:flex;align-items:center;gap:9px;
  font-family:var(--serif);font-weight:600;font-size:22px;
  font-variant-numeric:tabular-nums;letter-spacing:-.01em;}
.wx{width:26px;height:26px;flex:none;display:inline-flex;}
.wx svg{width:100%;height:100%;overflow:visible;}
/* Bewegung laeuft immer -- bewusst NICHT hinter prefers-reduced-motion, weil
   die animierten Wetter-Icons hier ausdruecklich gewuenscht sind. */
.wx .spin{transform-origin:center;animation:wxspin 16s linear infinite;}
.wx .drift{animation:wxdrift 3.6s ease-in-out infinite;}
.wx .drop{animation:wxdrop 1.3s linear infinite;}
.wx .flake{animation:wxflake 2.6s linear infinite;}
.wx .fog{animation:wxfog 3.4s ease-in-out infinite;}
.wx .bolt{animation:wxbolt 2.4s steps(1,end) infinite;}
@keyframes wxspin{to{transform:rotate(360deg);}}
@keyframes wxdrift{0%,100%{transform:translateX(-1px);}50%{transform:translateX(1.2px);}}
@keyframes wxdrop{0%{transform:translateY(-2px);opacity:0;}25%{opacity:1;}100%{transform:translateY(5px);opacity:0;}}
@keyframes wxflake{0%{transform:translateY(-2px);opacity:0;}25%{opacity:1;}100%{transform:translateY(6px);opacity:0;}}
@keyframes wxfog{0%,100%{transform:translateX(-1.5px);}50%{transform:translateX(1.5px);}}
@keyframes wxbolt{0%,88%,100%{opacity:.2;}90%,96%{opacity:1;}}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;
  padding:3px 10px;border-radius:999px;white-space:nowrap;}
.card.gut .pill{background:color-mix(in srgb,var(--good) 22%,transparent);color:var(--good-ink);}
.card.mittel .pill{background:color-mix(in srgb,var(--warn) 22%,transparent);color:var(--warn-ink);}
.card.schlecht .pill{background:color-mix(in srgb,var(--bad) 22%,transparent);color:var(--bad-ink);}
.pill .dot{width:8px;height:8px;}

.metrics{display:flex;gap:18px;flex-wrap:wrap;margin-top:11px;}
.metric{display:flex;flex-direction:column;gap:1px;}
.metric__v{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums;}
.metric__v .feels,.metric__v .mm{font-weight:400;color:var(--muted);font-size:13px;}
.metric__k{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--faint);}

.badges{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px;}
.badge{font-size:12px;color:var(--ink);background:var(--chip);
  border:1px solid var(--line);border-radius:8px;padding:4px 9px;}

/* Warnung, wenn die Seite veraltet ist (per JS eingeblendet) */
.stale{display:flex;align-items:center;gap:8px;
  background:color-mix(in srgb,var(--bad) 16%,var(--chip));
  border:1px solid var(--bad);border-left:4px solid var(--bad);
  border-radius:10px;padding:10px 13px;font-size:13.5px;margin:0 0 18px;
  color:var(--bad-ink);}
.stale[hidden]{display:none;}

/* Naechster-Regen-Hinweis */
.outlook{display:flex;align-items:center;gap:8px;background:var(--chip);
  border:1px solid var(--line);border-radius:10px;padding:9px 13px;
  font-size:13.5px;margin:-8px 0 24px;}

/* Stunden-Streifen (ganzer Tag auf einen Blick) */
.ribbon{display:flex;gap:2px;margin:2px 0 5px;}
.ribbon .rc{flex:1;height:26px;border-radius:3px;background:var(--line);}
.ribbon .rc.gut{background:var(--good);}
.ribbon .rc.mittel{background:var(--warn);}
.ribbon .rc.schlecht{background:var(--bad);}
.ribbon .rc.past{opacity:.30;}
.ribbon .rc.night{filter:brightness(.5) saturate(.6);}
.ribbon .rc.now{outline:2px solid var(--ink);outline-offset:1px;
  border-radius:4px;position:relative;z-index:1;}
.ribbon-ax{display:flex;justify-content:space-between;color:var(--faint);
  font-size:10.5px;font-variant-numeric:tabular-nums;margin-bottom:10px;}
.suninfo{display:flex;gap:14px;color:var(--muted);font-size:12.5px;
  margin:0 0 14px;font-variant-numeric:tabular-nums;}

/* Windrichtungs-Pfeil in der Boeen-Kachel */
.wind{display:inline-flex;align-items:center;gap:3px;font-weight:400;
  color:var(--muted);font-size:12px;margin-left:3px;}
.warr{width:13px;height:13px;flex:none;}

/* 7-Tage-Ausblick */
.week{display:flex;gap:6px;overflow-x:auto;padding-bottom:4px;
  -webkit-overflow-scrolling:touch;}
.wk{flex:1 0 62px;min-width:62px;background:var(--card);border:1px solid var(--line);
  border-radius:12px;padding:10px 6px 9px;text-align:center;}
.wk__d{font-size:12px;font-weight:600;color:var(--muted);}
.wk__ic{display:flex;justify-content:center;margin:6px 0 4px;}
.wk__ic .wx{width:30px;height:30px;}
.wk__t{font-size:14px;font-weight:600;font-variant-numeric:tabular-nums;}
.wk__t span{color:var(--muted);font-weight:400;margin-left:3px;}
.wk__p{font-size:11px;color:#9ec5f4;margin-top:3px;font-variant-numeric:tabular-nums;}

/* Fuss */
.foot{border-top:1px solid var(--line);margin-top:8px;padding-top:16px;
  color:var(--faint);font-size:12.5px;line-height:1.7;}
.foot a{color:var(--muted);}
"""


# ---------------------------------------------------------------------------
# ANIMIERTE WETTER-ICONS (inline SVG + CSS-Keyframes, keine Library)
# Bewegung nur bei prefers-reduced-motion: no-preference (Barrierefreiheit).
# ---------------------------------------------------------------------------
_SUN_RAYS = ('<g class="spin"{o} stroke="#fab219" stroke-width="{sw}" '
             'stroke-linecap="round">'
             '<line x1="{cx}" y1="{t}" x2="{cx}" y2="{t2}"/>'
             '<line x1="{cx}" y1="{b2}" x2="{cx}" y2="{b}"/>'
             '<line x1="{t}" y1="{cy}" x2="{t2}" y2="{cy}"/>'
             '<line x1="{b2}" y1="{cy}" x2="{b}" y2="{cy}"/>'
             '<line x1="{d1}" y1="{d1}" x2="{d2}" y2="{d2}"/>'
             '<line x1="{d3}" y1="{d3}" x2="{d4}" y2="{d4}"/>'
             '<line x1="{d3}" y1="{d1}" x2="{d4}" y2="{d2}"/>'
             '<line x1="{d1}" y1="{d3}" x2="{d2}" y2="{d4}"/></g>')

_CLOUD = ('<g class="drift" fill="{c}"><circle cx="9" cy="13" r="4"/>'
          '<circle cx="14" cy="11" r="5"/><circle cx="17.5" cy="14" r="3.5"/>'
          '<rect x="8" y="12.5" width="10" height="5.5" rx="2.7"/></g>')

WX_SVG = {
    "clear": '<svg viewBox="0 0 24 24">' + _SUN_RAYS.format(
        o='', sw=2, cx=12, cy=12, t=2, t2=4.6, b2=19.4, b=22,
        d1=5, d2=6.8, d3=19, d4=17.2) +
        '<circle cx="12" cy="12" r="5" fill="#fab219"/></svg>',

    "partly": ('<svg viewBox="0 0 24 24">' + _SUN_RAYS.format(
        o=' style="transform-origin:8px 8px"', sw=1.5, cx=8, cy=8,
        t=1.5, t2=3.4, b2=12.6, b=14.5, d1=3.4, d2=4.7, d3=12.6, d4=11.3) +
        '<circle cx="8" cy="8" r="3.2" fill="#fab219"/>'
        '<g class="drift" fill="#cfc8be"><circle cx="12.5" cy="16" r="3.6"/>'
        '<circle cx="16.5" cy="14.5" r="4.4"/>'
        '<rect x="11.5" y="16" width="8.5" height="4.6" rx="2.3"/></g></svg>'),

    "cloudy": '<svg viewBox="0 0 24 24">' + _CLOUD.format(c="#cfc8be") + '</svg>',

    "fog": ('<svg viewBox="0 0 24 24">'
            '<g class="drift" fill="#cfc8be" opacity=".65">'
            '<circle cx="9" cy="10" r="3.6"/><circle cx="14" cy="8.5" r="4.4"/>'
            '<rect x="8" y="9.5" width="9" height="4.2" rx="2"/></g>'
            '<g stroke="#b9b2a7" stroke-width="1.8" stroke-linecap="round">'
            '<line class="fog" x1="5" y1="17" x2="19" y2="17"/>'
            '<line class="fog" style="animation-delay:.5s" x1="6.5" y1="20" '
            'x2="17.5" y2="20"/></g></svg>'),

    "rain": ('<svg viewBox="0 0 24 24">' + _CLOUD.format(c="#c3bcb2") +
             '<g stroke="#6db3f2" stroke-width="2" stroke-linecap="round">'
             '<line class="drop" x1="9.5" y1="18.5" x2="9.5" y2="21"/>'
             '<line class="drop" style="animation-delay:.45s" x1="13.5" y1="18.5" x2="13.5" y2="21"/>'
             '<line class="drop" style="animation-delay:.9s" x1="16.5" y1="18.5" x2="16.5" y2="21"/>'
             '</g></svg>'),

    "snow": ('<svg viewBox="0 0 24 24">' + _CLOUD.format(c="#c3bcb2") +
             '<g fill="#e6eefb"><circle class="flake" cx="9.5" cy="19.5" r="1.2"/>'
             '<circle class="flake" style="animation-delay:.7s" cx="13.5" cy="19.5" r="1.2"/>'
             '<circle class="flake" style="animation-delay:1.3s" cx="16.5" cy="19.5" r="1.2"/>'
             '</g></svg>'),

    "thunder": ('<svg viewBox="0 0 24 24">' + _CLOUD.format(c="#a9a297") +
                '<polygon class="bolt" points="12.5,15 9,20 11.6,20 10.4,24 '
                '15.5,18 12.4,18" fill="#fac800"/></svg>'),
}


def wx_svg(kind: str) -> str:
    return f'<span class="wx">{WX_SVG.get(kind, WX_SVG["cloudy"])}</span>'


def _wind_arrow(deg_from: float) -> str:
    """Kleiner Pfeil, gedreht in die Richtung, in die der Wind weht (+ Kompass,
    aus welcher Richtung er kommt)."""
    to = (deg_from + 180) % 360
    svg = (f'<svg class="warr" viewBox="0 0 16 16" '
           f'style="transform:rotate({to:.0f}deg)">'
           '<path d="M8 2.5 L8 13 M8 2.5 L4.8 6.3 M8 2.5 L11.2 6.3" '
           'stroke="#9ec5f4" stroke-width="1.7" fill="none" '
           'stroke-linecap="round" stroke-linejoin="round"/></svg>')
    return f'<span class="wind">{svg}{_compass(deg_from)}</span>'


def _dur(mins: int) -> str:
    if mins < 60:
        return f"{mins} Min"
    h, mm = divmod(mins, 60)
    return f"{h} Std" if mm == 0 else f"{h} Std {mm} Min"


def _banner(icon: str, title: str, value: str, tone: str = "good") -> str:
    col = {"good": "var(--good)", "warn": "var(--warn)", "bad": "var(--bad)"}[tone]
    return f"""
      <div class="best" style="border-left-color:{col}">
        <span class="best__ic">{icon}</span>
        <div>
          <div class="best__t">{title}</div>
          <div class="best__v">{value}</div>
        </div>
      </div>"""


def _now_next_banner(day: dict, now: datetime) -> str:
    """Heute: aktuelles Fenster hervorheben bzw. Countdown zum naechsten guten."""
    windows = day["windows"]
    cur = now.hour * 60 + now.minute

    def start(w): return w["start_hour"] * 60
    def end(w): return (w["end_hour"] + 1) * 60

    current = next((w for w in windows if start(w) <= cur < end(w)), None)
    if current and current["rating"] == GUT:
        return _banner("🐾", "Jetzt gute Zeit",
                       f"noch {_dur(end(current) - cur)} "
                       f"(bis {current['end_hour']+1:02d} Uhr)", "good")
    nxt = next((w for w in windows if w["rating"] == GUT and start(w) > cur), None)
    if nxt:
        return _banner("⏳", "Nächstes gutes Fenster",
                       f"{_time_range(nxt)} — in {_dur(start(nxt) - cur)}", "good")
    if current and current["rating"] == MITTEL:
        return _banner("👍", "Jetzt geht es",
                       f"Mittel bis {current['end_hour']+1:02d} Uhr", "warn")
    nxt_m = next((w for w in windows if w["rating"] == MITTEL and start(w) > cur), None)
    if nxt_m:
        return _banner("👍", "Nur mittlere Fenster",
                       f"nächstes {_time_range(nxt_m)} — in {_dur(start(nxt_m) - cur)}",
                       "warn")
    return _banner("🚫", "Kein gutes Fenster mehr heute",
                   "Morgen früh wieder schauen", "bad")


def _ribbon_html(day: dict, cfg: dict) -> str:
    """Farbstreifen ueber den ganzen Gassi-Tag; Vergangenheit blass, Nacht
    abgedunkelt, aktuelle Stunde markiert."""
    wh = cfg["walk_hours"]
    by_hour = {h["hour"]: h for h in day["hours_all"]}
    sr = day["sunrise"].hour if day.get("sunrise") else None
    ss = day["sunset"].hour if day.get("sunset") else None
    cells = ""
    for hr in range(wh["start"], wh["end"] + 1):
        h = by_hour.get(hr)
        cls = ["rc"]
        if h:
            cls.append(h["rating"])
            if h["past"]:
                cls.append("past")
            if h["is_now"]:
                cls.append("now")
            title = f"{hr:02d} Uhr — {LABEL[h['rating']]}"
        else:
            title = f"{hr:02d} Uhr"
        if sr is not None and (hr < sr or hr >= ss):
            cls.append("night")
        cells += f'<span class="{" ".join(cls)}" title="{title}"></span>'
    span = wh["end"] - wh["start"]
    ticks = [wh["start"], wh["start"] + round(span / 3),
             wh["start"] + round(2 * span / 3), wh["end"] + 1]
    ax = "".join(f"<span>{t:02d}</span>" for t in ticks)
    return f'<div class="ribbon">{cells}</div><div class="ribbon-ax">{ax}</div>'


def _suninfo_html(day: dict) -> str:
    if not day.get("sunrise_str"):
        return ""
    return (f'<div class="suninfo"><span>🌅 {day["sunrise_str"]} Uhr</span>'
            f'<span>🌇 {day["sunset_str"]} Uhr</span></div>')


def _rain_outlook_html(outlook) -> str:
    if not outlook:
        return ""
    kind, dt = outlook
    hhmm = dt.strftime("%H:%M") if dt else ""
    text = {
        "rain_soon": f"🌧️ Nächster Regen gegen {hhmm} Uhr",
        "dry_soon":  f"🌤️ Regenpause ab etwa {hhmm} Uhr",
        "rain_hold": "🌧️ Regen hält vorerst an",
        "dry":       "✅ Kein Regen in den nächsten Stunden",
    }[kind]
    return f'<div class="outlook">{text}</div>'


def _week_html(week: list[dict]) -> str:
    if not week:
        return ""
    cols = "".join(
        f'<div class="wk"><div class="wk__d">{wd["abbr"]}</div>'
        f'<div class="wk__ic">{wx_svg(wd["kind"])}</div>'
        f'<div class="wk__t">{wd["tmax"]}°<span>{wd["tmin"]}°</span></div>'
        f'<div class="wk__p">💧{wd["prob"]}%</div></div>'
        for wd in week
    )
    return f"""
    <section class="day">
      <div class="day-head">
        <h2 class="day-label">7 Tage</h2>
        <span class="day-date">Ausblick</span>
      </div>
      <div class="week">{cols}</div>
    </section>"""


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
    temp_mid = round((w["temp_min"] + w["temp_max"]) / 2)
    # Gefuehlte Temperatur nur zeigen, wenn sie spuerbar abweicht (Windchill/Schwuele).
    feels = (f'<span class="feels"> gef. {w["feels_avg"]}°</span>'
             if abs(w["feels_avg"] - temp_mid) >= 2 else "")
    # Spitzenintensitaet nur zeigen, wenn ueberhaupt nennenswert Regen kommt --
    # "0,0 mm" waere nur Rauschen. Deutsches Dezimalkomma.
    mm_txt = f'{w["mm_max"]:.1f}'.replace(".", ",")
    mm = (f'<span class="mm"> {mm_txt} mm/h</span>' if w["mm_max"] >= 0.05 else "")
    return f"""
      <article class="card {w['rating']}">
        <div class="card__top">
          <div class="card__time">{wx_svg(w['kind'])}{_time_range(w)}</div>
          <span class="pill"><span class="dot" style="background:var(--c)"></span>{LABEL[w['rating']]}</span>
        </div>
        <div class="metrics">
          <div class="metric"><span class="metric__v">{temp}C{feels}</span><span class="metric__k">Temperatur</span></div>
          <div class="metric"><span class="metric__v">{w['rain_max']} %{mm}</span><span class="metric__k">Regen</span></div>
          <div class="metric"><span class="metric__v">{w['gust_max']} km/h{_wind_arrow(w['wind_dir'])}</span><span class="metric__k">Böen</span></div>
          <div class="metric"><span class="metric__v">{w['cloud_avg']} %</span><span class="metric__k">Wolken</span></div>
        </div>
        {badges_block}
      </article>"""


def _best_banner(day: dict) -> str:
    best = day["best"]
    if best and best["rating"] == GUT:
        return _banner("🐾", "Beste Zeit", _time_range(best), "good")
    if best and best["rating"] == MITTEL:
        return _banner("👍", "Beste Zeit", _time_range(best), "warn")
    return _banner("🚫", "Beste Zeit", "Kein gutes Fenster — kurz halten", "bad")


def _day_html(day: dict, cfg: dict, now: datetime) -> str:
    banner = _now_next_banner(day, now) if day["is_today"] else _best_banner(day)
    if day["windows"]:
        cards = "".join(_card_html(w) for w in day["windows"])
    else:
        cards = ('<p class="sub">Keine weiteren Fenster heute '
                 'im Gassi-Zeitfenster.</p>')
    return f"""
    <section class="day">
      <div class="day-head">
        <h2 class="day-label">{day['label']}</h2>
        <span class="day-date">{day['weekday']}, {day['date_str']}</span>
      </div>
      {_ribbon_html(day, cfg)}
      {_suninfo_html(day)}
      {banner}
      <div class="cards">{cards}</div>
    </section>"""


# ---------------------------------------------------------------------------
# APP-ICON  --  "Pfote & Sonne" (Auswahl B): SVG fuers Tab, PNGs fuer Homescreen
# ---------------------------------------------------------------------------
APP_NAME = "Gassi Wetter"

# Vektor-Icon (Browser-Tab / Android-PWA). Amber Sonne mit Hundepfote.
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120">
  <defs><radialGradient id="bg" cx="0.5" cy="0.42" r="0.75">
    <stop offset="0" stop-color="#2e2620"/><stop offset="1" stop-color="#14110d"/></radialGradient></defs>
  <rect width="120" height="120" rx="26" fill="url(#bg)"/>
  <g stroke="#fab219" stroke-width="5" stroke-linecap="round">
    <line x1="60" y1="16" x2="60" y2="27"/><line x1="60" y1="93" x2="60" y2="104"/>
    <line x1="16" y1="60" x2="27" y2="60"/><line x1="93" y1="60" x2="104" y2="60"/>
    <line x1="29" y1="29" x2="37" y2="37"/><line x1="83" y1="83" x2="91" y2="91"/>
    <line x1="91" y1="29" x2="83" y2="37"/><line x1="37" y1="83" x2="29" y2="91"/></g>
  <circle cx="60" cy="60" r="27" fill="#fab219"/>
  <g fill="#14110d">
    <ellipse cx="60" cy="66" rx="11" ry="9"/>
    <ellipse cx="47" cy="55" rx="4.4" ry="6"/><ellipse cx="55" cy="49" rx="4.4" ry="6"/>
    <ellipse cx="65" cy="49" rx="4.4" ry="6"/><ellipse cx="73" cy="55" rx="4.4" ry="6"/></g>
</svg>"""

MANIFEST = {
    "name": APP_NAME,
    "short_name": APP_NAME,
    "description": "Beste Gassi-Zeiten fuer Magdeburg",
    "start_url": ".",
    "scope": ".",
    "display": "standalone",
    "background_color": "#15120F",
    "theme_color": "#15120F",
    "icons": [
        {"src": "icon.svg", "type": "image/svg+xml",
         "sizes": "any", "purpose": "any"},
        {"src": "icon-192.png", "type": "image/png",
         "sizes": "192x192", "purpose": "any maskable"},
        {"src": "icon-512.png", "type": "image/png",
         "sizes": "512x512", "purpose": "any maskable"},
    ],
}

# Kopf-Tags fuer Favicon, Homescreen-Icon, Manifest & Standalone-Modus.
ICON_HEAD = (
    '<link rel="icon" type="image/svg+xml" href="icon.svg">\n'
    '<link rel="apple-touch-icon" href="icon-180.png">\n'
    '<link rel="manifest" href="manifest.webmanifest">\n'
    '<meta name="theme-color" content="#15120F">\n'
    '<meta name="apple-mobile-web-app-capable" content="yes">\n'
    '<meta name="mobile-web-app-capable" content="yes">\n'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">\n'
    f'<meta name="apple-mobile-web-app-title" content="{APP_NAME}">'
)


def _paw_sun_png(size: int):
    """Zeichnet das Icon (Pfote & Sonne) als PNG-Bild der Kantenlaenge `size`.
    Motiv liegt in der zentralen ~70%-Zone -> auch als 'maskable' sicher."""
    from PIL import Image, ImageDraw
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    amber, dark = (250, 178, 25, 255), (20, 17, 13, 255)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22),
                        fill=(34, 28, 23, 255))
    cx = cy = s / 2
    # Sonnenstrahlen
    r_in, r_out, w = s * 0.25, s * 0.35, max(2, int(s * 0.045))
    for k in range(8):
        a = math.radians(k * 45)
        d.line([cx + math.cos(a) * r_in, cy + math.sin(a) * r_in,
                cx + math.cos(a) * r_out, cy + math.sin(a) * r_out],
               fill=amber, width=w)
    # Sonne
    r = s * 0.225
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=amber)
    # Pfotenballen + vier Zehen (dunkel) auf der Sonne
    prx, pry, pcy = s * 0.092, s * 0.075, cy + s * 0.05
    d.ellipse([cx - prx, pcy - pry, cx + prx, pcy + pry], fill=dark)
    trx, tr_y = s * 0.037, s * 0.05
    for dx, dy in ((-0.108, -0.042), (-0.042, -0.092),
                   (0.042, -0.092), (0.108, -0.042)):
        tx, ty = cx + dx * s, cy + dy * s
        d.ellipse([tx - trx, ty - tr_y, tx + trx, ty + tr_y], fill=dark)
    return img


def write_icons(out_dir: Path) -> None:
    """Schreibt icon.svg, die PNG-Groessen und das Manifest ins Ausgabeverzeichnis.
    Unabhaengig vom Wetter -> wird immer erzeugt (auch fuer die Fallback-Seite)."""
    (out_dir / "icon.svg").write_text(ICON_SVG, encoding="utf-8")
    try:
        for size, name in ((180, "icon-180.png"), (192, "icon-192.png"),
                           (512, "icon-512.png")):
            _paw_sun_png(size).save(out_dir / name)
    except ImportError:
        print("Hinweis: Pillow fehlt – PNG-Icons uebersprungen (SVG greift).",
              file=sys.stderr)
    (out_dir / "manifest.webmanifest").write_text(
        json.dumps(MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8")


def build_html(days: list[dict], cfg: dict, now: datetime,
               outlook=None, week=None, notified: str = "") -> str:
    loc = cfg["location"]["name"]
    stand = now.strftime("%d.%m.%Y, %H:%M Uhr")
    built_iso = now.replace(microsecond=0).isoformat()
    stale_h = cfg.get("stale_warn_h", 6)
    days_html = "".join(_day_html(d, cfg, now) for d in days)
    wh = cfg["walk_hours"]
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<meta name="color-scheme" content="dark">
<title>{APP_NAME}</title>
{ICON_HEAD}
<meta name="gassi-notified" content="{notified}">
<style>{CSS}</style>
</head>
<body>
  <header class="masthead">
    <div class="kicker">🐾 Gassi-Planer</div>
    <h1 class="title">{loc}</h1>
    <p class="sub">Beste Zeitfenster für die Hunderunde &middot; Stand {stand}</p>
  </header>

  <div class="stale" id="stale" hidden></div>

  <div class="legend">
    <span class="lg"><span class="dot" style="background:var(--good)"></span>Gut</span>
    <span class="lg"><span class="dot" style="background:var(--warn)"></span>Mittel</span>
    <span class="lg"><span class="dot" style="background:var(--bad)"></span>Schlecht</span>
  </div>

  {_rain_outlook_html(outlook)}

  {days_html}

  {_week_html(week or [])}

  <footer class="foot">
    Gassi-Zeiten {wh['start']}–{wh['end']} Uhr &middot;
    Hitze ab {cfg['heat']['warn']}°/{cfg['heat']['bad']} °C &middot;
    Kälte ab {cfg['cold']['warn']}° (gefühlt) &middot;
    Böen ab {cfg['wind']['gust_warn']} km/h &middot;
    Regen ab {cfg['rain']['prob_bad']} % &middot;
    Gewitter/Schnee/Glatteis = Ausschluss<br>
    Wetterdaten: <a href="https://open-meteo.com/">Open-Meteo</a> (kostenlos, ohne Gewähr) &middot;
    Automatische Aktualisierung morgens &amp; am frühen Nachmittag.
  </footer>
<script>
// Blendet eine Warnung ein, wenn die Seite deutlich veraltet ist. Faengt jede
// Ursache ab (Ausloeser tot, Token abgelaufen, API-Ausfall) -- ohne sie waere
// veraltete Vorhersage kaum vom aktuellen Stand zu unterscheiden.
(function () {{
  var built = new Date("{built_iso}");
  if (isNaN(built)) return;
  var hours = (Date.now() - built.getTime()) / 3600000;
  if (hours < {stale_h}) return;
  var el = document.getElementById("stale");
  if (!el) return;
  var txt = hours < 48
    ? Math.floor(hours) + " Stunden"
    : Math.floor(hours / 24) + " Tage";
  el.textContent = "\\u26A0\\uFE0F Daten sind " + txt +
    " alt \\u2014 die Aktualisierung h\\u00e4ngt. Werte mit Vorsicht nutzen.";
  el.hidden = false;
}})();
</script>
</body>
</html>"""


def build_fallback_html(cfg: dict, now: datetime, err: str,
                        notified: str = "") -> str:
    """Notseite, falls Open-Meteo nach mehreren Versuchen nicht erreichbar ist.
    Wird deployt, damit die Live-URL nie kaputt/leer wirkt. Der Dedupe-Marker
    wird durchgereicht, damit ein Ausfall den Push-Zustand nicht loescht."""
    loc = cfg["location"]["name"]
    stand = now.strftime("%d.%m.%Y, %H:%M Uhr")
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<meta name="color-scheme" content="dark">
<title>{APP_NAME}</title>
{ICON_HEAD}
<meta name="gassi-notified" content="{notified}">
<style>{CSS}</style>
</head>
<body>
  <header class="masthead">
    <div class="kicker">🐾 Gassi-Planer</div>
    <h1 class="title">{loc}</h1>
    <p class="sub">Stand {stand}</p>
  </header>
  <article class="card schlecht">
    <div class="card__top">
      <div class="card__time"><span style="font-size:22px">📡</span> Keine Daten</div>
    </div>
    <p class="sub" style="margin-top:10px">
      Die Wetterdaten sind gerade nicht abrufbar. Der nächste automatische
      Lauf versucht es erneut &mdash; einfach später neu laden.
    </p>
  </article>
  <footer class="foot">
    Wetterdaten: <a href="https://open-meteo.com/">Open-Meteo</a> vorübergehend
    nicht erreichbar.<br>Details: {html.escape(err)[:200]}
  </footer>
</body>
</html>"""


# ---------------------------------------------------------------------------
# PUSH-BENACHRICHTIGUNG (ntfy)  --  optional, nur beim Morgen-Lauf
# Topic kommt aus dem Secret NTFY_TOPIC (NICHT im oeffentlichen Code!).
# Aktiv nur, wenn GASSI_NOTIFY=1 gesetzt ist (Workflow steuert das).
# ---------------------------------------------------------------------------
def _range_ascii(w: dict) -> str:
    """Uhrzeit-Range ohne Sonderzeichen (HTTP-Header muessen ASCII/latin-1 sein)."""
    if w["start_hour"] == w["end_hour"]:
        return f"{w['start_hour']:02d} Uhr"
    return f"{w['start_hour']:02d}-{w['end_hour'] + 1:02d} Uhr"


def _notify_content(day: dict) -> tuple[str, str, str]:
    """(Titel [ASCII], Text [UTF-8], ntfy-Tags) fuer die Morgen-Nachricht."""
    best = day["best"]
    if best and best["rating"] == GUT:
        return (
            f"Gassi heute: {_range_ascii(best)}",
            f"Beste Zeit {_time_range(best)} — {best['temp_min']}–{best['temp_max']} °C, "
            f"Regen max {best['rain_max']} %, Böen {best['gust_max']} km/h",
            "dog2,white_check_mark",
        )
    if best and best["rating"] == MITTEL:
        return (
            f"Gassi heute: {_range_ascii(best)} (mittel)",
            f"Bestes Fenster {_time_range(best)} — {best['temp_min']}–{best['temp_max']} °C, "
            f"Regen max {best['rain_max']} %, Böen {best['gust_max']} km/h",
            "dog2",
        )
    return ("Heute kein gutes Gassi-Fenster",
            "Nur kurze Runden — Wetter meiden. Details in der App.",
            "dog2,umbrella")


MARKER_RE = re.compile(r'name="gassi-notified"\s+content="([^"]*)"')


def read_live_marker(cfg: dict) -> str | None:
    """Liest den 'zuletzt benachrichtigt'-Marker aus der aktuell VEROEFFENTLICHTEN
    Seite. Das ist unser Dedupe-Speicher: Jeder Lauf startet auf einem frischen
    Runner, die live stehende Seite ist der einzige gemeinsame Zustand.
    Rueckgabe: Datum als 'JJJJ-MM-TT', '' wenn kein Marker, None bei Fehler."""
    url = cfg.get("site_url")
    if not url:
        return None
    try:
        r = httpx.get(f"{url}?cb={int(time.time())}",
                      headers={"User-Agent": USER_AGENT,
                               "Cache-Control": "no-cache", "Pragma": "no-cache"},
                      timeout=15, follow_redirects=True)
        r.raise_for_status()
        m = MARKER_RE.search(r.text)
        return m.group(1) if m else ""
    except Exception as e:  # noqa: BLE001
        # Fail-open: lieber einmal doppelt melden als den Morgen-Push verschlucken.
        print(f"Hinweis: Marker nicht lesbar ({e}) -> Push nicht unterdrueckt.",
              file=sys.stderr)
        return None


def should_notify(mode: str, now: datetime, prev_marker: str | None,
                  cfg: dict) -> tuple[bool, str]:
    """Entscheidet, ob dieser Lauf pushen darf. Bewusst hier (nicht im YAML),
    damit keine Cron-Strings doppelt gepflegt werden muessen."""
    if mode == "force":
        return True, "manueller Lauf (erzwungen)"
    w = cfg["notify_window"]
    if not (w["start"] <= now.hour < w["end"]):
        return False, (f"ausserhalb des Melde-Fensters "
                       f"{w['start']}-{w['end']} Uhr")
    if prev_marker == now.date().isoformat():
        return False, "heute wurde bereits benachrichtigt"
    return True, "erster erfolgreicher Morgenlauf heute"


def send_ntfy(day: dict, cfg: dict) -> bool:
    """Schickt die Morgen-Zusammenfassung als Push an ntfy. Best-effort:
    Fehler brechen den Build nie ab. Rueckgabe: True bei Erfolg."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("Hinweis: NTFY_TOPIC nicht gesetzt -> keine Push-Nachricht.",
              file=sys.stderr)
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    title, body, tags = _notify_content(day)
    try:
        r = httpx.post(
            f"{server}/{topic}",
            content=body.encode("utf-8"),
            headers={
                "Title": title,                 # ASCII
                "Tags": tags,                   # ASCII
                "Click": cfg.get("site_url", ""),
                "User-Agent": USER_AGENT,
            },
            timeout=15,
        )
        r.raise_for_status()
        print(f"OK: Push an {server}/<topic> gesendet.")
        return True
    except Exception as e:  # noqa: BLE001  (Push ist best-effort)
        print(f"Warnung: Push fehlgeschlagen: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main() -> int:
    # Umlaute in der Konsolenausgabe auch unter Windows (cp1252) erlauben.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description="Gassi-Wetter Dashboard bauen")
    ap.add_argument("--out", default="public",
                    help="Zielverzeichnis für index.html (Standard: public)")
    args = ap.parse_args()

    tz = ZoneInfo(CONFIG["timezone"])
    now = datetime.now(tz)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "index.html"
    write_icons(out_dir)  # Icons + Manifest (wetterunabhaengig)

    # Dedupe-Marker aus der live stehenden Seite lesen (einziger Zustand ueber
    # Laeufe hinweg). Immer lesen, damit ihn auch stille Deploys nicht loeschen.
    today_iso = now.date().isoformat()
    prev_marker = read_live_marker(CONFIG)
    mode = os.environ.get("GASSI_NOTIFY", "")

    print(f"Ziehe Wetter für {CONFIG['location']['name']} ...")
    try:
        data = fetch_weather(CONFIG)
        daily = parse_daily(data, CONFIG, now)
        daily_by_date = {x["date"]: x for x in daily}
        hours = [rate_hour(h, CONFIG) for h in parse_hours(data, CONFIG, now)]
        days = group_days(hours, CONFIG, now, daily_by_date)
        outlook = parse_rain_outlook(data, CONFIG, now)
        print(f"  {len(hours)} Gassi-Stunden, {len(days)} Detailtage, "
              f"{len(daily)} Tage im Ausblick.")

        # Push VOR dem Schreiben entscheiden -> Ergebnis landet als Marker
        # in der Seite und unterdrueckt die weiteren Cron-Versuche des Tages.
        sent = False
        if mode:
            ok, reason = should_notify(mode, now, prev_marker, CONFIG)
            print(f"  Push: {'ja' if ok else 'nein'} ({reason})")
            if ok:
                today_day = next((d for d in days if d["is_today"]), None)
                if today_day:
                    sent = send_ntfy(today_day, CONFIG)
        marker = today_iso if (sent or prev_marker == today_iso) \
            else (prev_marker or "")

        out_file.write_text(
            build_html(days, CONFIG, now, outlook, daily, marker),
            encoding="utf-8")
        print(f"OK: {out_file} geschrieben (Marker: {marker or '-'}).")
    except Exception as e:  # noqa: BLE001  (API/Parsing-Ausfall -> Notseite)
        print(f"FEHLER: {e}\n  -> schreibe Fallback-Seite.", file=sys.stderr)
        out_file.write_text(
            build_fallback_html(CONFIG, now, str(e), prev_marker or ""),
            encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
