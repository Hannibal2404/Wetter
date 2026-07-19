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
            "wcode": int(_num(h["weather_code"][i])),
            "cloud": _num(h["cloud_cover"][i]),
            "uv": _num(h["uv_index"][i]),
            "gust": _num(h["wind_gusts_10m"][i]),
            "is_day": bool(h["is_day"][i]),
        })
    return out


def _num(v, default=0.0):
    return default if v is None else v


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
        "clouds": [h["cloud"]],
        "gusts": [h["gust"]],
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
    w["clouds"].append(h["cloud"])
    w["gusts"].append(h["gust"])
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
    w["cloud_avg"] = round(sum(w["clouds"]) / n)
    w["gust_max"] = round(max(w["gusts"]))
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
.card__time{display:flex;align-items:center;gap:9px;
  font-family:var(--serif);font-weight:600;font-size:22px;
  font-variant-numeric:tabular-nums;letter-spacing:-.01em;}
.wx{width:26px;height:26px;flex:none;display:inline-flex;}
.wx svg{width:100%;height:100%;overflow:visible;}
.wx .spin{transform-origin:center;}
@media (prefers-reduced-motion:no-preference){
  .wx .spin{animation:wxspin 16s linear infinite;}
  .wx .drift{animation:wxdrift 3.6s ease-in-out infinite;}
  .wx .drop{animation:wxdrop 1.3s linear infinite;}
  .wx .flake{animation:wxflake 2.6s linear infinite;}
  .wx .fog{animation:wxfog 3.4s ease-in-out infinite;}
  .wx .bolt{animation:wxbolt 2.4s steps(1,end) infinite;}
}
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
.metric__v .feels{font-weight:400;color:var(--muted);font-size:13px;}
.metric__k{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--faint);}

.badges{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px;}
.badge{font-size:12px;color:var(--ink);background:var(--chip);
  border:1px solid var(--line);border-radius:8px;padding:4px 9px;}

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
    return f"""
      <article class="card {w['rating']}">
        <div class="card__top">
          <div class="card__time">{wx_svg(w['kind'])}{_time_range(w)}</div>
          <span class="pill"><span class="dot" style="background:var(--c)"></span>{LABEL[w['rating']]}</span>
        </div>
        <div class="metrics">
          <div class="metric"><span class="metric__v">{temp}C{feels}</span><span class="metric__k">Temperatur</span></div>
          <div class="metric"><span class="metric__v">{w['rain_max']} %</span><span class="metric__k">Regen</span></div>
          <div class="metric"><span class="metric__v">{w['gust_max']} km/h</span><span class="metric__k">Böen</span></div>
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
<title>{APP_NAME}</title>
{ICON_HEAD}
<style>{CSS}</style>
</head>
<body>
  <header class="masthead">
    <div class="kicker">🐾 Gassi-Planer</div>
    <h1 class="title">{loc}</h1>
    <p class="sub">Beste Zeitfenster für die Hunderunde &middot; Stand {stand}</p>
  </header>

  <div class="legend">
    <span class="lg"><span class="dot" style="background:var(--good)"></span>Gut</span>
    <span class="lg"><span class="dot" style="background:var(--warn)"></span>Mittel</span>
    <span class="lg"><span class="dot" style="background:var(--bad)"></span>Schlecht</span>
  </div>

  {days_html}

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
</body>
</html>"""


def build_fallback_html(cfg: dict, now: datetime, err: str) -> str:
    """Notseite, falls Open-Meteo nach mehreren Versuchen nicht erreichbar ist.
    Wird deployt, damit die Live-URL nie kaputt/leer wirkt."""
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

    print(f"Ziehe Wetter für {CONFIG['location']['name']} ...")
    try:
        data = fetch_weather(CONFIG)
        hours = [rate_hour(h, CONFIG) for h in parse_hours(data, CONFIG, now)]
        days = group_days(hours, now)
        print(f"  {len(hours)} Gassi-Stunden, {len(days)} Tage aufbereitet.")
        out_file.write_text(build_html(days, CONFIG, now), encoding="utf-8")
        print(f"OK: {out_file} geschrieben.")
    except Exception as e:  # noqa: BLE001  (API/Parsing-Ausfall -> Notseite)
        print(f"FEHLER: {e}\n  -> schreibe Fallback-Seite.", file=sys.stderr)
        out_file.write_text(build_fallback_html(CONFIG, now, str(e)),
                            encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
