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

# Cílová města (klíč = přepínač --dest).
#   label     = popisek do UI
#   name      = název v IDOS
#   jen_prime = True → povolit pouze přímé spoje (bez přestupů), oba směry
DESTINATIONS = {
    "ostrava": {"label": "Ostrava", "name": "Ostrava", "jen_prime": False},
    "fm": {"label": "Frýdek-Místek", "name": "Frýdek-Místek", "jen_prime": True},
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


def _nice(name):
    """Hezčí název zastávky do UI: 'Pazderna,,Špok' -> 'Pazderna, Špok'."""
    return name.replace(",,", ", ")


def seber_smer(home_stops, city_name, smer, pocet, kdy, datum, zaklad,
               jen_prime, progress=True):
    """Posbírá spoje pro jeden směr ('tam' = domů→město, 'zpet' = město→domů)
    ze všech domácích zastávek, seřadí chronologicky a vrátí nejbližších `pocet`.
    Při `jen_prime` ponechá pouze přímé spoje (bez přestupů)."""
    vsechny = []
    fetch = pocet * 2 if jen_prime else pocet   # při filtru přímých nabrat víc
    for prio, home in home_stops:
        f, t = (home, city_name) if smer == "tam" else (city_name, home)
        if progress:
            print(f"  … {f} → {t}", file=sys.stderr)
        try:
            spoje = najdi_spoje(f, t, pocet=fetch, kdy=kdy, datum=datum, zaklad=zaklad)
        except Exception as e:
            print(f"     (chyba: {e})", file=sys.stderr)
            continue
        for s in spoje:
            if jen_prime and s["prestupy"] > 0:
                continue
            s["_prio"] = prio
            s["_origin"] = f
            s["_dest"] = t
            vsechny.append(s)
        _time.sleep(PAUZA_S)
    vsechny.sort(key=lambda s: s["dt"])
    return vsechny[:pocet]


def seber_vse(dests, home_stops, pocet, kdy, datum, zaklad, progress=True):
    """Vrátí strukturu {dkey: {label, jen_prime, smery: {tam:[...], zpet:[...]}}}."""
    data = {}
    for dkey, d in dests.items():
        smery = {}
        for smer in ("tam", "zpet"):
            if progress:
                print(f"== {d['label']} / {smer} ==", file=sys.stderr)
            smery[smer] = seber_smer(home_stops, d["name"], smer, pocet,
                                     kdy, datum, zaklad, d["jen_prime"], progress)
        data[dkey] = {"label": d["label"], "jen_prime": d["jen_prime"], "smery": smery}
    return data


# ---------------------------------------------------------------------------
# Textový výpis (CLI)
# ---------------------------------------------------------------------------

def vypis_spoj(i, s, zaklad):
    trv = f"{s['trvani_min']} min" if s["trvani_min"] else "?"
    prest = "přímý" if s["prestupy"] == 0 else f"{s['prestupy']}× přestup"
    den = ""
    if s["dt"].date() != zaklad.date():
        den = f" {s['dt'].day}.{s['dt'].month}."
    print(f"{i:>2}. {s['odjezd']} → {s['prijezd']}{den}   [P{s['_prio']}] "
          f"{_nice(s['_origin'])}  →  {_nice(s['_dest'])}   ({trv}, {prest})")
    for leg in s["legs"]:
        print(f"       {leg['label']:<14} {leg['dep']} {_nice(leg['from'])}  →  "
              f"{leg['arr']} {_nice(leg['to'])}")


def vypis_text(data, zaklad, kdy, datum):
    print(f"\nIDOS – nejbližší spoje  (od {kdy}, {datum})")
    for dkey, d in data.items():
        for smer in ("tam", "zpet"):
            nadpis = "TAM (domů → město)" if smer == "tam" else "ZPĚT (město → domů)"
            print("\n" + "=" * 72)
            print(f"### {d['label']} — {nadpis}"
                  + ("  [jen přímé]" if d["jen_prime"] else ""))
            spoje = d["smery"][smer]
            if not spoje:
                print("   (žádné spoje)")
            for i, s in enumerate(spoje, 1):
                vypis_spoj(i, s, zaklad)


# ---------------------------------------------------------------------------
# HTML stránka pro rodinu  (estetika „odjezdové tabule")
# ---------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta http-equiv="refresh" content="300">
<meta name="theme-color" content="#0b0c10">
<title>Odjezdy</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600..800&family=IBM+Plex+Mono:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0b0c10; --panel:#13151c; --ink:#efe9db; --muted:#969cab;
    --line:rgba(255,255,255,.085); --amber:#f6a623; --green:#33d39a; --hot:#ff6a3d;
    --acc:var(--amber);
  }
  *{box-sizing:border-box;}
  html{-webkit-text-size-adjust:100%;}
  body{
    margin:0; color:var(--ink); background:var(--bg);
    font-family:"IBM Plex Sans",system-ui,sans-serif; line-height:1.42;
    padding:0 14px 60px;
    background-image:
      radial-gradient(130% 60% at 50% -12%, rgba(246,166,35,.12), transparent 58%),
      radial-gradient(90% 50% at 105% 2%, rgba(51,211,154,.08), transparent 60%);
    background-attachment:fixed;
  }
  body::before{
    content:""; position:fixed; inset:0; z-index:0; pointer-events:none; opacity:.045;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  }
  .wrap{position:relative; z-index:1; max-width:700px; margin:0 auto;}

  header{padding:30px 2px 8px;}
  .kicker{font-family:"IBM Plex Mono",monospace; font-size:.7rem; letter-spacing:.34em;
    text-transform:uppercase; color:var(--muted);}
  h1{font-family:"Bricolage Grotesque",sans-serif; font-weight:800;
    font-size:clamp(2.3rem,11vw,3.4rem); line-height:.92; letter-spacing:-.025em;
    margin:.16em 0 .14em;}
  h1 .dot{color:var(--amber);}
  .sub{display:flex; justify-content:space-between; align-items:baseline; gap:12px;
    font-family:"IBM Plex Mono",monospace; font-size:.76rem; color:var(--muted);}
  #clock{color:var(--ink); font-weight:700; font-variant-numeric:tabular-nums;}

  .ctabs{display:flex; gap:9px; margin:22px 0 10px;}
  .ctab{flex:1; appearance:none; cursor:pointer; color:var(--muted);
    font-family:"Bricolage Grotesque",sans-serif; font-weight:700; font-size:1.05rem;
    background:var(--panel); border:1px solid var(--line); border-radius:15px;
    padding:14px 12px; transition:transform .15s, background .25s, color .25s, border-color .25s;}
  .ctab:active{transform:scale(.97);}
  .ctab[data-city="ostrava"].on{background:var(--amber); border-color:var(--amber); color:#120c02;}
  .ctab[data-city="fm"].on{background:var(--green); border-color:var(--green); color:#03130d;}

  .dtabs{display:flex; gap:8px; margin:0 0 18px;}
  .dtab{appearance:none; cursor:pointer; color:var(--muted);
    font-family:"IBM Plex Mono",monospace; font-size:.8rem; font-weight:600; letter-spacing:.05em;
    background:transparent; border:1px solid var(--line); border-radius:999px;
    padding:8px 17px; transition:.2s;}
  .dtab i{font-style:normal; opacity:.7;}
  .dtab.on{color:var(--ink); border-color:var(--ink); background:rgba(255,255,255,.05);}

  .panel{animation:fade .35s ease both;}
  .panel[hidden]{display:none;}
  .city-ostrava{--acc:var(--amber);}
  .city-fm{--acc:var(--green);}
  @keyframes fade{from{opacity:0; transform:translateY(5px);} to{opacity:1; transform:none;}}

  .card{position:relative; display:grid; grid-template-columns:auto 1fr auto; gap:14px;
    align-items:start; background:var(--panel); border:1px solid var(--line);
    border-radius:16px; padding:14px 15px 13px 19px; margin:11px 0; overflow:hidden;
    animation:rise .5s cubic-bezier(.2,.7,.2,1) both; animation-delay:calc(var(--i)*45ms);}
  @keyframes rise{from{opacity:0; transform:translateY(11px);} to{opacity:1; transform:none;}}
  .card .bar{position:absolute; left:0; top:0; bottom:0; width:4px; background:var(--acc);}
  .card.gone{opacity:.32; filter:saturate(.35);}

  .t{min-width:84px;}
  .time{display:block; font-family:"IBM Plex Mono",monospace; font-weight:700;
    font-size:1.66rem; line-height:1; letter-spacing:-.02em; font-variant-numeric:tabular-nums;}
  .cd{display:inline-block; margin-top:7px; padding:1px 8px; border-radius:999px;
    font-family:"IBM Plex Mono",monospace; font-size:.7rem; font-weight:600;
    color:var(--muted); border:1px solid var(--line); white-space:nowrap;}
  .cd.soon{color:#120c02; background:var(--hot); border-color:var(--hot);
    animation:pulse 1.7s ease-in-out infinite;}
  @keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(255,106,61,.45);} 50%{box-shadow:0 0 0 7px rgba(255,106,61,0);}}

  .route{font-family:"Bricolage Grotesque",sans-serif; font-weight:700; font-size:1.06rem;
    letter-spacing:-.01em; display:flex; flex-wrap:wrap; align-items:baseline;
    column-gap:.42em; row-gap:1px;}
  .route .ep{white-space:nowrap; overflow-wrap:anywhere;}
  .route .arr{font-style:normal; color:var(--acc); font-weight:800;}
  .meta{font-family:"IBM Plex Mono",monospace; font-size:.73rem; color:var(--muted); margin-top:3px;}
  .legs{margin-top:8px; display:flex; flex-direction:column; gap:2px;}
  .leg{font-family:"IBM Plex Mono",monospace; font-size:.71rem; color:var(--muted);}
  .leg b{color:var(--ink); font-weight:600;}

  .prio{align-self:flex-start; font-family:"IBM Plex Mono",monospace; font-size:.64rem;
    font-weight:700; letter-spacing:.06em; padding:2px 7px; border-radius:7px;
    border:1px solid var(--line); white-space:nowrap;}
  .prio.p1{color:#ffd27d; border-color:rgba(255,210,125,.32);}
  .prio.p2{color:#6b7180; border-color:rgba(255,255,255,.07); opacity:.85;}

  .empty{border:1px dashed var(--line); border-radius:16px; padding:26px 18px; text-align:center;
    color:var(--muted); font-family:"IBM Plex Mono",monospace; font-size:.82rem; margin:11px 0;}

  footer{margin-top:26px; text-align:center; color:var(--muted); opacity:.7;
    font-family:"IBM Plex Mono",monospace; font-size:.68rem; letter-spacing:.04em;}

  @media (prefers-reduced-motion:reduce){
    *{animation:none !important;}
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="kicker">Bus · Vlak · MHD — idos</div>
    <h1>Odjezdy<span class="dot">.</span></h1>
    <div class="sub">
      <span>Aktualizováno __CAS__</span>
      <span id="clock">--:--:--</span>
    </div>
  </header>

  <nav class="ctabs">__CTABS__</nav>
  <nav class="dtabs">
    <button class="dtab on" data-smer="tam" onclick="setSmer('tam')">Tam <i>→</i></button>
    <button class="dtab" data-smer="zpet" onclick="setSmer('zpet')"><i>←</i> Zpět</button>
  </nav>

  <main>__PANELS__</main>

  <footer>Data: IDOS (idos.idnes.cz) · stránka se sama obnoví každých 5 min</footer>
</div>

<script>
  var city = "__CITY0__", smer = "tam";
  function apply(){
    var panels = document.querySelectorAll(".panel");
    for (var i=0;i<panels.length;i++){
      var p = panels[i];
      p.hidden = !(p.dataset.city===city && p.dataset.smer===smer);
    }
    document.querySelectorAll(".ctab").forEach(function(b){ b.classList.toggle("on", b.dataset.city===city); });
    document.querySelectorAll(".dtab").forEach(function(b){ b.classList.toggle("on", b.dataset.smer===smer); });
  }
  function setCity(c){ city=c; apply(); }
  function setSmer(s){ smer=s; apply(); }

  function tik(){
    var now = new Date();
    document.querySelectorAll(".card").forEach(function(c){
      var dep = new Date(c.dataset.dep);
      var diff = Math.round((dep - now) / 60000);
      var el = c.querySelector(".cd");
      if (diff < 0){ c.classList.add("gone"); el.textContent="ujel"; el.classList.remove("soon"); return; }
      c.classList.remove("gone");
      el.textContent = diff < 60 ? ("za " + diff + " min")
                                 : ("za " + Math.floor(diff/60) + " h " + (diff%60) + " min");
      el.classList.toggle("soon", diff <= 10);
    });
  }
  function clock(){
    var d = new Date(), p = function(n){ return (n<10?"0":"")+n; };
    var el = document.getElementById("clock");
    if (el) el.textContent = p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds());
  }
  apply(); tik(); clock();
  setInterval(tik, 15000); setInterval(clock, 1000);
</script>
</body>
</html>
"""


def _karta(s, zaklad, i):
    trv = f"{s['trvani_min']} min" if s["trvani_min"] else "?"
    prest = "přímý" if s["prestupy"] == 0 else f"{s['prestupy']}× přestup"
    den = ""
    if s["dt"].date() != zaklad.date():
        den = f" · {s['dt'].day}.{s['dt'].month}."
    legs_html = "".join(
        f'<span class="leg"><b>{_html.escape(l["label"])}</b> {l["dep"]} '
        f'{_html.escape(_nice(l["from"]))} → {l["arr"]} {_html.escape(_nice(l["to"]))}</span>'
        for l in s["legs"])
    return f'''<article class="card" data-dep="{s['dt'].isoformat()}" style="--i:{i}">
  <span class="bar"></span>
  <div class="t"><time class="time">{s['odjezd']}</time><span class="cd"></span></div>
  <div class="m">
    <div class="route"><span class="ep">{_html.escape(_nice(s['_origin']))}</span><i class="arr">→</i><span class="ep">{_html.escape(_nice(s['_dest']))}</span></div>
    <div class="meta">příjezd {s['prijezd']}{den} · {trv} · {prest}</div>
    <div class="legs">{legs_html}</div>
  </div>
  <span class="prio p{s['_prio']}">P{s['_prio']}</span>
</article>'''


def _panel(dkey, d, smer, zaklad, hidden):
    spoje = d["smery"][smer]
    if spoje:
        vnitrek = "\n".join(_karta(s, zaklad, j) for j, s in enumerate(spoje))
    else:
        msg = "Žádné přímé spoje v dohledné době." if d["jen_prime"] else "Žádné spoje v dohledné době."
        vnitrek = f'<div class="empty">{msg}</div>'
    h = " hidden" if hidden else ""
    return (f'<section class="panel city-{dkey}" data-city="{dkey}" '
            f'data-smer="{smer}"{h}>\n{vnitrek}\n</section>')


def vytvor_html(data, zaklad):
    cas = datetime.now().strftime("%-d.%-m.%Y %H:%M")
    keys = list(data.keys())
    city0 = keys[0] if keys else "ostrava"
    ctabs = "\n".join(
        f'<button class="ctab{" on" if dkey == city0 else ""}" data-city="{dkey}" '
        f'onclick="setCity(\'{dkey}\')">{_html.escape(data[dkey]["label"])}</button>'
        for dkey in keys)
    panely = "\n".join(
        _panel(dkey, data[dkey], smer, zaklad, hidden=not (dkey == city0 and smer == "tam"))
        for dkey in keys for smer in ("tam", "zpet"))
    return (PAGE.replace("__CAS__", _html.escape(cas))
                .replace("__CTABS__", ctabs)
                .replace("__PANELS__", panely)
                .replace("__CITY0__", city0))


# ---------------------------------------------------------------------------
# Hlavní program
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Nejbližší spoje do Ostravy a Frýdku-Místku (tam i zpět).")
    ap.add_argument("--dest", choices=list(DESTINATIONS), help="jen jedno město (ostrava|fm)")
    ap.add_argument("--from", dest="odkud", help="jen jedna priorita počáteční zastávky (1-2)")
    ap.add_argument("--pocet", type=int, default=POCET_SPOJU, help="počet spojů na směr (výchozí 10)")
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

    home_stops = ORIGINS
    if args.odkud:
        home_stops = [o for o in ORIGINS if o[0] == args.odkud]
        if not home_stops:
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

    data = seber_vse(dests, home_stops, args.pocet, args.kdy, args.datum, zaklad)

    if args.html:
        out = vytvor_html(data, zaklad)
        if args.html == "-":
            sys.stdout.write(out)
        else:
            with open(args.html, "w", encoding="utf-8") as f:
                f.write(out)
            print(f"HTML zapsáno do {args.html}.", file=sys.stderr)
        return

    vypis_text(data, zaklad, kdy, datum)


if __name__ == "__main__":
    main()
