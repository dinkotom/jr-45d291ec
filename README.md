# Spoje – web pro rodinu

Živá stránka s nejbližšími spoji z našich zastávek do **Ostravy** a **Frýdku-Místku**.
Scrapuje veřejný IDOS (`idos.idnes.cz`). GitHub Actions každých ~15 min vygeneruje
stránku a publikuje ji na GitHub Pages. Stránka má `noindex` (nedohledatelná přes Google)
a obnovuje se v prohlížeči sama každých 5 min + živý odpočet „za X min".

## Změna zastávek / priorit

Uprav `ORIGINS` / `DESTINATIONS` nahoře v [idos_spoje.py](idos_spoje.py) a `git push` —
Action sama přegeneruje a nasadí. Přesné názvy zastávek ověříš:

```sh
python3 idos_spoje.py --najdi "Nošovice"
```

## Lokální spuštění (volitelné)

```sh
pip install -r requirements.txt
python3 idos_spoje.py                       # textový výpis
python3 idos_spoje.py --html out.html       # HTML stránka
```

## Cron / keepalive

GitHub vypíná naplánované (cron) workflow po ~60 dnech bez aktivity v repu.
Workflow proto obsahuje **keepalive** krok: když je poslední commit starší než
50 dní, udělá prázdný commit a tím cron udrží naživu (bez zahlcení historie).
Případné ruční spuštění: Actions → *build-and-deploy* → *Run workflow*.
