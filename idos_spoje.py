#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IDOS spoje – nejbližší spojení z našich zastávek do Ostravy a Frýdku-Místku.

Scrapuje veřejný web idos.idnes.cz (vlak + autobus + MHD dohromady).
Žádný API klíč. Pro osobní použití.

Spuštění:
    ~/.venvs/idos-spoje/bin/python3 idos_spoje.py
    ~/.venvs/idos-spoje/bin/python3 idos_spoje.py --dest ostrava
    ~/.venvs/idos-spoje/bin/python3 idos_spoje.py --pocet 3 --kdy 07:30
    ~/.venvs/idos-spoje/bin/python3 idos_spoje.py --najdi "Nošovice"   # nápověda názvů zastávek
"""

import argparse
import html as _html
import re
import sys
import time as _time
import json
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# KONFIGURACE  – uprav podle potřeby
# ---------------------------------------------------------------------------

# Počáteční zastávky v pořadí priority (1 = nejvýhodnější).
# Názvy musí přesně odpovídat IDOS – ověř přes:  --najdi "<text>"
ORIGINS = [
    ("1", "Pazderna,,Špok"),
    ("1", "Horní Domaslavice,,statek"),
    ("2", "Lučina,,Kocurovice"),
    ("2", "Nošovice,,U lesa"),
]

# Cílové zastávky (klíč = přepínač --dest, hodnota = (popisek, název v IDOS))
DESTINATIONS = {
    "ostrava": ("Ostrava", "Ostrava"),
    "fm": ("Frýdek-Místek", "Frýdek-Místek"),
}

POCET_SPOJU = 10         # kolik nejbližších spojů zobrazit (sloučeně, chronologicky)
MAX_STRANEK = 6          # pojistka proti zacyklení při doptávání
PAUZA_S = 1.1            # pauza mezi dotazy (IDOS toleruje ~1/s)

BASE = "https://idos.idnes.cz/vlakyautobusymhdvse"
RESULTS_URL = BASE + "/spojeni/vysledky/"
HINTS_URL = BASE + "/Ajax/SearchTimetableObjects"
HEADERS = {"User-Agent": "Mozilla/5.0", "accept": "*/*"}


# ---------------------------------------------------------------------------
# Nápověda názvů zastávek (autocomplete)
# ---------------------------------------------------------------------------

def najdi_zastavku(prefix, count=8):
    qs = {"callback": "jQuery", "count": count, "prefixText": prefix,
          "searchByPosition": "false", "onlyStation": "false", "format": "json"}
    r = requests.get(HINTS_URL, params=qs, headers=HEADERS, timeout=20)
    m = re.match(r"^jQuery\((.*)\);", r.text)
    if not m:
        return []
    return [(h["text"], h["description"]) for h in json.loads(m.group(1))]


# ---------------------------------------------------------------------------
# Parsování výsledků
# ---------------------------------------------------------------------------

def _cisty_nazev(li_item):
    """Vrátí čistý název zastávky (bez nástupiště) z <strong class="name">."""
    p_station = li_item.find("p", class_="station")
    name = p_station.find("strong", class_="name") if p_station else None
    if name:
        return name.get_text(strip=True)
    return p_station.get_text(strip=True) if p_station else "?"


def _parse_minuty(head_text):
    """'Celkový čas 1 hod 23 min' -> 83 ;  '56 min' -> 56"""
    hod = re.search(r"(\d+)\s*hod", head_text)
    minu = re.search(r"(\d+)\s*min", head_text)
    total = 0
    if hod:
        total += int(hod.group(1)) * 60
    if minu:
        total += int(minu.group(1))
    return total or None


def parse_spoje(html):
    soup = BeautifulSoup(html, "html.parser")
    spoje = []
    for box in soup.find_all("div", class_="connection-details"):
        parent = box.find_parent()
        head_text = parent.get_text(" ", strip=True)[:160] if parent else ""

        legs = []
        for ln in box.find_all("div", class_="outside-of-popup"):
            tc = ln.find("div", class_="title-container")
            h3 = tc.find("h3") if tc else None
            label = h3.get_text(" ", strip=True) if h3 else "?"
            items = ln.find_all("li", class_="item")
            if not items:
                continue
            dep = items[0].find("p", class_="time").get_text(strip=True)
            arr = items[-1].find("p", class_="time").get_text(strip=True)
            s_from = _cisty_nazev(items[0])
            s_to = _cisty_nazev(items[-1])
            legs.append({"label": label, "dep": dep, "from": s_from,
                         "arr": arr, "to": s_to})

        if not legs:
            continue

        # ID spoje (pro deduplikaci)
        cid = None
        try:
            m = re.match(r"^connectionBox-(\d+)$", box.find_parent().get("id", ""))
            if m:
                cid = m.group(1)
        except Exception:
            pass

        spoje.append({
            "id": cid,
            "odjezd": legs[0]["dep"],
            "prijezd": legs[-1]["arr"],
            "trvani_min": _parse_minuty(head_text),
            "prestupy": len(legs) - 1,
            "legs": legs,
        })
    return spoje


# ---------------------------------------------------------------------------
# Vyhledání N nejbližších spojů (s doptáváním posunem času)
# ---------------------------------------------------------------------------

def _cas_plus_minutu(hhmm):
    try:
        t = datetime.strptime(hhmm, "%H:%M") + timedelta(minutes=1)
        return t.strftime("%H:%M")
    except ValueError:
        return None


def _minuty(hhmm):
    hh, mm = (int(x) for x in hhmm.split(":"))
    return hh * 60 + mm


def najdi_spoje(f, t, pocet=POCET_SPOJU, kdy=None, datum=None, zaklad=None):
    """Vrátí až `pocet` nejbližších spojů v chronologickém pořadí.

    Každému spoji doplní absolutní `dt` (datetime odjezdu). Datum se neodvozuje
    z (nespolehlivé) hlavičky IDOS, ale z pořadí: IDOS vrací spoje chronologicky,
    takže když čas odjezdu „klesne", jde o další den.
    """
    if zaklad is None:
        zaklad = datetime.now()
    nalezene = []
    videno = set()
    cas = kdy
    prev_min = _minuty(kdy) if kdy else (zaklad.hour * 60 + zaklad.minute)
    den_offset = 0
    for _ in range(MAX_STRANEK):
        params = {"f": f, "t": t}
        if cas:
            params["time"] = cas
        if datum:
            params["date"] = datum
        r = requests.get(RESULTS_URL, params=params, headers=HEADERS, timeout=25)
        davka = parse_spoje(r.text)
        if not davka:
            break
        for s in davka:
            klic = s["id"] or (s["odjezd"], s["prijezd"], tuple(l["label"] for l in s["legs"]))
            if klic in videno:
                continue
            videno.add(klic)
            m = _minuty(s["odjezd"])
            if m < prev_min:               # čas klesl -> další den
                den_offset += 1
            prev_min = m
            den = (zaklad + timedelta(days=den_offset)).replace(
                hour=m // 60, minute=m % 60, second=0, microsecond=0)
            s["dt"] = den
            nalezene.append(s)
        if len(nalezene) >= pocet:
            break
        # posuň čas za poslední odjezd a doptej se
        novy = _cas_plus_minutu(davka[-1]["odjezd"])
        if not novy or novy == cas:
            break
        cas = novy
        _time.sleep(PAUZA_S)
    return nalezene[:pocet]


# ---------------------------------------------------------------------------
# Výpis
# ---------------------------------------------------------------------------

def vypis_spoj(i, s, zaklad):
    trv = f"{s['trvani_min']} min" if s["trvani_min"] else "?"
    prest = "přímý" if s["prestupy"] == 0 else f"{s['prestupy']}× přestup"
    # datum ukaž jen když spoj jede jiný den než základ vyhledávání
    den = ""
    dt = s.get("dt")
    if dt and dt.date() != zaklad.date():
        den = f" {dt.day}.{dt.month}."
    # štítky: priorita počáteční zastávky a cíl
    print(f"{i:>2}. {s['odjezd']} → {s['prijezd']}{den}   [P{s['_prio']}] "
          f"{s['_origin']}  →  {s['_dest']}   ({trv}, {prest})")
    for leg in s["legs"]:
        print(f"       {leg['label']:<14} {leg['dep']} {leg['from']}  →  {leg['arr']} {leg['to']}")


def seber_spoje(origins, dests, pocet, kdy, datum, zaklad, progress=True):
    """Posbírá spoje ze všech kombinací (zastávka × cíl), seřadí chronologicky
    a vrátí nejbližších `pocet`. Z každé kombinace bere až `pocet`, ať je globálně
    nejbližších `pocet` pokryto i kdyby vše jelo z jediné zastávky."""
    vsechny = []
    for dkey, (dlabel, dname) in dests.items():
        for prio, oname in origins:
            if progress:
                print(f"  … načítám {oname} → {dlabel}", file=sys.stderr)
            try:
                spoje = najdi_spoje(oname, dname, pocet=pocet,
                                    kdy=kdy, datum=datum, zaklad=zaklad)
            except Exception as e:
                print(f"     (chyba: {e})", file=sys.stderr)
                continue
            for s in spoje:
                s["_prio"] = prio
                s["_origin"] = oname
                s["_dest"] = dlabel
                vsechny.append(s)
            _time.sleep(PAUZA_S)
    vsechny.sort(key=lambda s: s["dt"])
    return vsechny[:pocet]


# ---------------------------------------------------------------------------
# HTML stránka pro rodinu
# ---------------------------------------------------------------------------

HTML_SABLONA = """<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta http-equiv="refresh" content="300">
<title>Spoje – Ostrava &amp; Frýdek-Místek</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 0 12px 40px; background: #f4f5f7; color: #1c1d1f; }}
  @media (prefers-color-scheme: dark) {{ body {{ background:#15171a; color:#e8e8ea; }} }}
  header {{ position: sticky; top: 0; background: inherit; padding: 14px 2px 8px; }}
  h1 {{ font-size: 1.15rem; margin: 0 0 2px; }}
  .meta {{ font-size: .8rem; opacity: .65; }}
  .card {{ background: #fff; border-radius: 12px; padding: 11px 13px; margin: 9px 0;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); display: flex; gap: 12px; align-items: flex-start; }}
  @media (prefers-color-scheme: dark) {{ .card {{ background:#22252a; box-shadow:none; }} }}
  .card.gone {{ opacity: .4; }}
  .when {{ min-width: 78px; }}
  .time {{ font-size: 1.3rem; font-weight: 700; line-height: 1.1; }}
  .cd {{ font-size: .78rem; font-weight: 600; color:#1a7f37; }}
  .cd.soon {{ color:#c2410c; }}
  .body {{ flex: 1; min-width: 0; }}
  .route {{ font-weight: 600; font-size: .98rem; }}
  .tags {{ margin: 3px 0 4px; display: flex; gap: 6px; flex-wrap: wrap; align-items:center; }}
  .tag {{ font-size: .72rem; padding: 1px 7px; border-radius: 999px; font-weight: 600; }}
  .p1 {{ background:#dbeafe; color:#1e40af; }}
  .p2 {{ background:#ede9fe; color:#5b21b6; }}
  .ostrava {{ background:#fde68a; color:#92400e; }}
  .fm {{ background:#bbf7d0; color:#166534; }}
  .info {{ font-size: .78rem; opacity:.7; }}
  .legs {{ font-size: .76rem; opacity:.75; margin-top: 5px; line-height: 1.45; }}
  .arr {{ font-size:.9rem; opacity:.8; }}
  footer {{ font-size:.72rem; opacity:.5; text-align:center; margin-top:18px; }}
</style>
</head>
<body>
<header>
  <h1>🚌 Nejbližší spoje</h1>
  <div class="meta">Aktualizováno {cas_aktualizace} • obnovuje se automaticky</div>
</header>
<main id="seznam">
{karty}
</main>
<footer>Data: IDOS (idos.idnes.cz). Stránka se sama obnoví každých 5 min.</footer>
<script>
function tik() {{
  var now = new Date();
  document.querySelectorAll('.card').forEach(function(c) {{
    var dep = new Date(c.dataset.dep);
    var diff = Math.round((dep - now) / 60000);
    var el = c.querySelector('.cd');
    if (diff < 0) {{ c.classList.add('gone'); el.textContent = 'ujel'; el.classList.remove('soon'); return; }}
    c.classList.remove('gone');
    if (diff < 60) {{ el.textContent = 'za ' + diff + ' min'; }}
    else {{ el.textContent = 'za ' + Math.floor(diff/60) + ' h ' + (diff%60) + ' min'; }}
    el.classList.toggle('soon', diff <= 10);
  }});
}}
tik(); setInterval(tik, 20000);
</script>
</body>
</html>
"""


def _karta(s, zaklad):
    trv = f"{s['trvani_min']} min" if s["trvani_min"] else "?"
    prest = "přímý" if s["prestupy"] == 0 else f"{s['prestupy']}× přestup"
    den = ""
    if s["dt"].date() != zaklad.date():
        den = f" ({s['dt'].day}.{s['dt'].month}.)"
    dtag = "ostrava" if s["_dest"].startswith("Ostrava") else "fm"
    legs_html = "<br>".join(
        f"{_html.escape(l['label'])}: {l['dep']} {_html.escape(l['from'])} → "
        f"{l['arr']} {_html.escape(l['to'])}" for l in s["legs"])
    return f"""  <div class="card" data-dep="{s['dt'].isoformat()}">
    <div class="when">
      <div class="time">{s['odjezd']}</div>
      <div class="cd"></div>
    </div>
    <div class="body">
      <div class="route">{_html.escape(s['_origin'])} → {_html.escape(s['_dest'])}{den}</div>
      <div class="tags">
        <span class="tag p{s['_prio']}">priorita {s['_prio']}</span>
        <span class="tag {dtag}">{_html.escape(s['_dest'])}</span>
        <span class="info">příjezd {s['prijezd']} • {trv} • {prest}</span>
      </div>
      <div class="legs">{legs_html}</div>
    </div>
  </div>"""


def vytvor_html(vybrane, zaklad):
    cas = datetime.now().strftime("%-d.%-m.%Y %H:%M")
    if vybrane:
        karty = "\n".join(_karta(s, zaklad) for s in vybrane)
    else:
        karty = '  <p>Momentálně nenalezeny žádné spoje.</p>'
    return HTML_SABLONA.format(cas_aktualizace=cas, karty=karty)


# ---------------------------------------------------------------------------
# Hlavní program
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Nejbližší spoje do Ostravy a Frýdku-Místku.")
    ap.add_argument("--dest", choices=list(DESTINATIONS), help="jen jeden cíl (ostrava|fm)")
    ap.add_argument("--from", dest="odkud", help="jen jedna priorita počáteční zastávky (1-2)")
    ap.add_argument("--pocet", type=int, default=POCET_SPOJU, help="počet spojů (výchozí 10)")
    ap.add_argument("--kdy", help="čas odjezdu HH:MM (výchozí: teď)")
    ap.add_argument("--datum", help="datum DD.MM.RRRR (výchozí: dnes)")
    ap.add_argument("--najdi", help="jen vypíše nápovědu názvů zastávek pro daný text a skončí")
    ap.add_argument("--html", metavar="SOUBOR", help="vygeneruje HTML stránku (- = stdout)")
    args = ap.parse_args()

    if args.najdi:
        print(f"Nápověda zastávek pro: {args.najdi!r}")
        for txt, desc in najdi_zastavku(args.najdi):
            print(f"  {txt}   [{desc}]")
        return

    origins = ORIGINS
    if args.odkud:
        origins = [o for o in ORIGINS if o[0] == args.odkud]
        if not origins:
            sys.exit(f"Priorita {args.odkud!r} není v konfiguraci.")

    dests = DESTINATIONS
    if args.dest:
        dests = {args.dest: DESTINATIONS[args.dest]}

    ted = datetime.now()
    # základ vyhledávání (datetime), z něhož se počítají absolutní časy odjezdů
    zaklad = ted
    if args.datum:
        try:
            zaklad = datetime.strptime(args.datum, "%d.%m.%Y")
        except ValueError:
            sys.exit("Datum zadej ve formátu DD.MM.RRRR.")
    if args.kdy:
        try:
            t = datetime.strptime(args.kdy, "%H:%M")
            zaklad = zaklad.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        except ValueError:
            sys.exit("Čas zadej ve formátu HH:MM.")
    kdy = args.kdy or ted.strftime("%H:%M")
    datum = args.datum or ted.strftime("%-d.%-m.%Y")

    vybrane = seber_spoje(origins, dests, args.pocet, args.kdy, args.datum, zaklad)

    if args.html:
        out = vytvor_html(vybrane, zaklad)
        if args.html == "-":
            sys.stdout.write(out)
        else:
            with open(args.html, "w", encoding="utf-8") as f:
                f.write(out)
            print(f"HTML zapsáno do {args.html} ({len(vybrane)} spojů).", file=sys.stderr)
        return

    print(f"\nIDOS – {len(vybrane)} nejbližších spojů chronologicky  (od {kdy}, {datum})")
    print("=" * 72)
    if not vybrane:
        print("(žádné spoje nenalezeny)")
    for i, s in enumerate(vybrane, 1):
        vypis_spoj(i, s, zaklad)


if __name__ == "__main__":
    main()
