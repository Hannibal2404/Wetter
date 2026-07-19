# 🐕 Gassi-Wetter Magdeburg

Wetterbasierter Gassi-Planer. Zieht die stündliche Vorhersage von
[Open-Meteo](https://open-meteo.com/) (kostenlos, **kein API-Key**), bewertet für
**heute und morgen** die besten Zeitfenster für Hunderunden und baut daraus ein
mobiles HTML-Dashboard, das automatisch auf **GitHub Pages** veröffentlicht wird.

Bewertet werden:
- **Regen** – Wahrscheinlichkeit (%) und Menge (mm/h)
- **Hitze** – Lufttemperatur als Näherung für heißen Asphalt (Pfotenschutz)
- **Pralle Sonne** – wenig Wolken bei Tag + hoher UV → Extra-Hinweis

Farblich klar getrennt: **Gut** (grün) · **Mittel** (gelb) · **Schlecht** (rot).

---

## Architektur

```
Wetter/
├── gassi_wetter.py          # Wetter ziehen → bewerten → public/index.html bauen
├── requirements.txt         # httpx (+ tzdata unter Windows)
├── .gitignore
└── .github/workflows/
    └── gassi.yml            # cron + manuell → build + deploy auf GitHub Pages
```

- **Motor:** GitHub Actions läuft täglich morgens (`schedule`) und ist manuell
  auslösbar (`workflow_dispatch`).
- **Hosting:** GitHub Pages, direkt aus dem Workflow deployt
  (`actions/deploy-pages` – kein Personal Access Token, kein zweites Repo nötig).

---

## Einmalige Einrichtung im Repo

1. **Öffentliches** Repo anlegen und diesen Ordner hineinpushen.
   > Öffentlich, weil Actions-Minuten und Pages-Traffic nur für öffentliche Repos
   > unbegrenzt kostenlos sind. Es sind nur Wetterdaten, nichts Privates.

2. **Settings → Pages → Build and deployment → Source:** auf **„GitHub Actions"**
   stellen (nicht „Deploy from a branch").

3. **Settings → Actions → General:** sicherstellen, dass Actions erlaubt sind
   (bei neuen Repos Standard). „Workflow permissions" muss nicht angefasst werden –
   die Rechte setzt der Workflow selbst (`permissions:` im YAML).

4. Fertig. Das Environment `github-pages` legt GitHub beim ersten Deploy selbst an.

**Ersten Lauf starten:** Actions-Tab → „Gassi-Wetter" → *Run workflow*.
Danach liegt die Seite unter `https://<dein-user>.github.io/<repo>/`.

---

## Nachjustieren (ohne Code-Umbau)

Alle Schwellwerte stehen zentral im `CONFIG`-Block oben in
[`gassi_wetter.py`](gassi_wetter.py):

| Wert | Bedeutung |
|------|-----------|
| `walk_hours` | Zeitfenster, das überhaupt als Gassi-Zeit gilt (z. B. 5–22 Uhr) |
| `rain.prob_ok` / `prob_bad` | Regenwahrscheinlichkeit für „mittel" bzw. „schlecht" (%) |
| `rain.mm_bad` | Regenmenge, ab der es „schlecht" ist (mm/h) |
| `heat.warn` / `bad` | Temperatur für „warm/aufpassen" bzw. „zu heiß" (°C) |
| `sun.cloud_max` / `uv_warn` | Ab wann es als „pralle Sonne" gilt (Wolken % / UV) |
| `days` | Wie viele Tage voraus (Standard 2 = heute + morgen) |

Cron-Zeit änderst du in [`.github/workflows/gassi.yml`](.github/workflows/gassi.yml)
unter `schedule`. Ein zweiter Lauf pro Tag = einfach eine zweite `cron`-Zeile.

---

## Lokal testen

```bash
pip install -r requirements.txt
python gassi_wetter.py --out ./public
# öffnet public/index.html im Browser
```

## Kosten

Komplett kostenlos: Open-Meteo (gratis, kein Key), GitHub Actions & Pages (für
öffentliche Repos ohne Limit). Kein Anthropic/KI-Aufruf im Betrieb.
