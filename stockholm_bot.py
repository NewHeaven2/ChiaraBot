#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stockholm Flat Alert v2 — Qasa + BostadsPortal + HousingAnywhere.

Filtri: appartamento intero, max 9000 SEK/mese, entro 40 min di mezzi
dall'Università di Stoccolma. Manda un alert Telegram in italiano.

Uso:
    export TG_TOKEN="123456:ABC..."
    export TG_CHAT="123456789"
    python3 stockholm_bot.py            # un giro singolo (cron / GitHub Actions)
    python3 stockholm_bot.py --loop     # ogni 15 minuti
    python3 stockholm_bot.py --test     # manda gli alert anche al primo giro

Dipendenze: pip install requests
"""

import argparse
import html as htmllib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# FILTRI
# ----------------------------------------------------------------------------
MAX_AFFITTO = 9000
MIN_MQ = 0                        # nessun limite di superficie
MAX_MINUTI_UNI = 50
SOLO_INTERI = True
AVVISA_ANCHE_SE_DUBBIO = True     # avvisa anche se un dato manca (zona, data, foto)
INGRESSO_DA = "2026-10-01"        # finestra di disponibilità richiesta
INGRESSO_A = "2026-10-20"
MIN_FOTO = 3                      # sotto questa soglia di solito c'è solo il palazzo
DATA_INDEFINITA_OK = True         # la data mancante non è un difetto: si concorda col proprietario

STATE_FILE = Path(__file__).with_name("seen.json")
STATO_FILE = Path(__file__).with_name("stato.json")
RIEPILOGO_OGNI_MINUTI = 60        # ogni quanto dire "nessun nuovo annuncio"; 0 = mai
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ----------------------------------------------------------------------------
# Minuti indicativi porta-a-porta fino a Universitetet (linea rossa T14).
# Stime, da verificare su sl.se per l'indirizzo preciso.
# ----------------------------------------------------------------------------
TEMPI_ZONA = {
    "lappkärrsberget": 10, "frescati": 10, "kräftriket": 15, "roslagstull": 15,
    "östermalm": 20, "vasastan": 20, "norrmalm": 20, "gärdet": 25, "stockholm": 25,
    "hjorthagen": 25, "danderyd": 25, "mörby": 25, "stocksund": 28, "bergshamra": 18,
    "solna": 25, "sundbyberg": 30, "kungsholmen": 28, "södermalm": 28,
    "huvudsta": 28, "hagalund": 27, "råsunda": 27, "ulriksdal": 28, "järva": 33,
    "johanneshov": 32, "hammarbyhöjden": 33, "liljeholmen": 32, "årsta": 33,
    "hägersten": 35, "aspudden": 35, "midsommarkransen": 35, "gröndal": 35,
    "enskede": 36, "sickla": 35, "bromma": 35, "alvik": 30, "traneberg": 32,
    "abrahamsberg": 35, "sundbybergs": 30, "solna strand": 30,
    "älvsjö": 38, "farsta": 40, "bandhagen": 38, "högdalen": 38, "stureby": 38,
    "nacka": 40, "sollentuna": 38, "kista": 35, "husby": 38, "akalla": 40,
    "lidingö": 35, "helenelund": 38, "ulvsunda": 33, "spånga": 40,
    "vällingby": 40, "hässelby": 45, "blackeberg": 38, "råcksta": 40,
    "skärholmen": 45, "järfälla": 45, "barkarby": 45, "jakobsberg": 45,
    "täby": 40, "vallentuna": 50, "upplands väsby": 45, "sollentuna kommun": 38,
    "haninge": 55, "huddinge": 45, "skogås": 50, "norsborg": 50, "tumba": 60,
    "märsta": 55, "södertälje": 70, "botkyrka": 50, "tyresö": 45, "värmdö": 55,
    "sigtuna": 60, "nynäshamn": 80, "uppsala": 60, "nykvarn": 75,
    "åkersberga": 55, "vårby": 50, "segeltorp": 45, "bro": 60, "kungsängen": 55,
    "gustavsberg": 55, "österåker": 55, "ekerö": 50, "rönninge": 55, "salem": 55,
}

PAROLE_STANZA = ("private room", "shared room", "room in", "rum i", "delat rum",
                 "corridor room", "studentrum")

# ----------------------------------------------------------------------------
# SORGENTI
# ----------------------------------------------------------------------------
QASA_API = "https://api.qasa.se/graphql"
QASA_AREE = ["se/stockholms_län"]      # copre tutta la contea, poi filtro io
QASA_PAGINE = 4                        # 4 x 50 = 200 annunci più recenti

URL_BOSTADSPORTAL = [
    "https://bostadsportal.se/en/rental-apartments/stockholm/",
    "https://bostadsportal.se/en/rental-apartments/solna/",
    "https://bostadsportal.se/en/rental-apartments/sundbyberg/",
    "https://bostadsportal.se/en/rental-apartments/nacka/",
    "https://bostadsportal.se/en/rental-apartments/sollentuna/",
    "https://bostadsportal.se/en/rental-apartments/lidingö/",
]
URL_HOMII = [
    "https://homii.se/hyra-bostad/stockholm",
    "https://homii.se/hyra-bostad/solna",
    "https://homii.se/hyra-bostad/sundbyberg",
]
URL_HOUSINGANYWHERE = [
    "https://housinganywhere.com/s/Stockholm--Sweden/apartment-for-rent",
    "https://housinganywhere.com/s/Stockholm--Sweden/studio-for-rent",
]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})


def scarica(url, lingua=None):
    r = SESSION.get(url, timeout=45,
                    headers={"Accept-Language": lingua} if lingua else None)
    r.raise_for_status()
    return r.text


# ----------------------------------------------------------------------------
# QASA — API GraphQL pubblica (nessun anti-bot, molto più affidabile dell'HTML)
# ----------------------------------------------------------------------------
QASA_QUERY = """
query($aree: [ID!], $max: Int, $after: String, $da: DateTime, $a: DateTime) {
  homeSearch(searchParams: {areaIdentifier: $aree, maxRent: $max,
                            homeType: [apartment],
                            moveInEarliest: $da, moveInLatest: $a}) {
    filterHomes(first: 50, after: $after) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        id rent squareMeters roomCount shared firsthand
        rentalType publishedAt
        duration { startOptimal }
        uploads { id }
        location { locality route }
      }
    }
  }
}
"""


def fetch_qasa():
    out, cursor = [], None
    for _ in range(QASA_PAGINE):
        r = SESSION.post(QASA_API, json={
            "query": QASA_QUERY,
            "variables": {"aree": QASA_AREE, "max": MAX_AFFITTO, "after": cursor,
                          "da": INGRESSO_DA, "a": INGRESSO_A},
        }, timeout=45)
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            raise RuntimeError(data["errors"][0].get("message"))
        conn = data["data"]["homeSearch"]["filterHomes"]
        for n in conn["nodes"]:
            if n.get("shared"):          # scarta stanze in condivisione
                continue
            loc = n.get("location") or {}
            zona = ", ".join(x for x in (loc.get("locality"), loc.get("route")) if x)
            out.append({
                "id": f"qasa-{n['id']}",
                "fonte": "Qasa",
                "url": f"https://qasa.com/se/sv/home/{n['id']}",
                "titolo": f"{n.get('roomCount') or '?'} locali, {n.get('squareMeters') or '?'} m²",
                "zona": zona or "Stoccolma",
                "prezzo": n["rent"],
                "mq": n.get("squareMeters"),
                "stanze": int(n["roomCount"]) if n.get("roomCount") else None,
                "extra": ("contratto di primo grado" if n.get("firsthand")
                          else "subaffitto") +
                         (", affitto breve" if n.get("rentalType") == "vacation" else ""),
                "data": (n.get("publishedAt") or "")[:10],
                "ingresso": ((n.get("duration") or {}).get("startOptimal") or "")[:10] or None,
                "foto": len(n.get("uploads") or []),
            })
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
        time.sleep(1)
    return out


# ----------------------------------------------------------------------------
# BOSTADSPORTAL
# ----------------------------------------------------------------------------
RE_BP = re.compile(
    r'href="(/en/rental-[^"]+?-id-(\d+))".*?'
    r'<h3[^>]*>(.*?)</h3>\s*<p[^>]*>(.*?)</p>.*?'
    r'<span class="font-bold">([\d\s\u00a0]+)\s*kr\.</span>', re.S)


def parse_bostadsportal(page, base="https://bostadsportal.se"):
    out = []
    for m in RE_BP.finditer(page):
        href, ann_id, titolo, zona, prezzo = m.groups()
        titolo = htmllib.unescape(re.sub(r"<[^>]+>", "", titolo)).strip()
        zona = htmllib.unescape(re.sub(r"<!--.*?-->|<[^>]+>", "", zona)).strip()
        mq = re.search(r"(\d+)\s*m²", titolo)
        st = re.search(r"(\d+)\s*rm", titolo)
        out.append({
            "id": f"bp-{ann_id}", "fonte": "BostadsPortal", "url": base + href,
            "titolo": titolo, "zona": zona,
            "prezzo": int(re.sub(r"\D", "", prezzo)),
            "mq": int(mq.group(1)) if mq else None,
            "stanze": int(st.group(1)) if st else None,
            "extra": "", "data": "", "ingresso": None, "foto": None,
        })
    return out


# ----------------------------------------------------------------------------
# HOUSINGANYWHERE
# ----------------------------------------------------------------------------
RE_HA = re.compile(
    r'href="(https://housinganywhere\.com/room/(ut\d+)[^"]*)"'
    r'.*?title="([^"]*?for SEK\s*([\d,]+)\s*per month in ([^"]+))"', re.S)


def parse_housinganywhere(page):
    out, visti = [], set()
    for m in RE_HA.finditer(page):
        url, ann_id, titolo, prezzo, zona = m.groups()
        if ann_id in visti:
            continue
        visti.add(ann_id)
        out.append({
            "id": f"ha-{ann_id}", "fonte": "HousingAnywhere", "url": url,
            "titolo": htmllib.unescape(titolo).strip(),
            "zona": htmllib.unescape(zona).strip(),
            "prezzo": int(prezzo.replace(",", "")),
            "mq": None, "stanze": None, "extra": "arredato, no deposito", "data": "",
            "ingresso": None, "foto": None,
        })
    return out


def arricchisci_bostadsportal(a):
    """Apre la scheda dell'annuncio per leggere data di ingresso e numero di foto."""
    try:
        page = scarica(a["url"])
    except Exception:
        return a
    m = re.search(r'available_from\\?": ?\\?"(\d{4}-\d{2}-\d{2})', page)
    if m:
        a["ingresso"] = m.group(1)
    m = re.search(r'\\?"images\\?": ?\[(.*?)\]', page, re.S)
    if m:
        blocco = m.group(1)
        totali = blocco.count('"url"')
        piante = blocco.count('is_floor_plan\\?": ?true')
        a["foto"] = max(totali - piante, 0)
    return a


# ----------------------------------------------------------------------------
# HOMII
# ----------------------------------------------------------------------------
MESI_SV = {"jan": "01", "feb": "02", "mar": "03", "apr": "04", "maj": "05",
           "jun": "06", "jul": "07", "aug": "08", "sep": "09", "okt": "10",
           "nov": "11", "dec": "12"}


def _data_sv(testo):
    m = re.match(r"(\d{1,2}) (\w{3})\w*\.? (\d{4})", testo)
    if not m:
        return None
    g, mese, anno = m.groups()
    mm = MESI_SV.get(mese.lower()[:3])
    return f"{anno}-{mm}-{int(g):02d}" if mm else None


def parse_homii(page):
    out = []
    for card in re.split(r'(?=<a href="/soka-bostad/annonser/)', page)[1:]:
        mid = re.search(r"/soka-bostad/annonser/([0-9a-f-]{36})", card)
        testo_card = re.sub(r"<!--.*?-->|<[^>]+>", "", card)
        mpr = (re.search(r"(\d{1,3}[ \u00a0]\d{3})\s*kr/m", testo_card)
               or re.search(r"\b(\d{4,5})\s*kr/m", testo_card))
        if not (mid and mpr):
            continue
        testo = htmllib.unescape(re.sub(r"<!--.*?-->|<[^>]+>", "", card)).strip()
        mmq = re.search(r"(\d+)\s*m²", testo)
        mrum = re.search(r"(\d+)[\d,.]*\s*rum", testo)
        mdata = re.search(r"(\d{1,2} \w{3,}\.? \d{4})", testo)
        mzona = re.search(r"(?:Lägenhet|Hus|Rum)([A-ZÅÄÖ][^0-9]{2,40}?)\d+\s*rum", testo)
        out.append({
            "id": f"homii-{mid.group(1)[:12]}",
            "fonte": "Homii",
            "url": f"https://homii.se/soka-bostad/annonser/{mid.group(1)}",
            "titolo": ("room in " if re.search(r"\bRum\b(?!\s*\d)", testo) and
                       not re.search(r"\bLägenhet\b", testo) else "") + testo[:90],
            "zona": (mzona.group(1).strip() if mzona else "Stoccolma"),
            "prezzo": int(re.sub(r"\D", "", mpr.group(1))),
            "mq": int(mmq.group(1)) if mmq else None,
            "stanze": int(mrum.group(1)) if mrum else None,
            "extra": "subaffitto",
            "data": "",
            "ingresso": _data_sv(mdata.group(1)) if mdata else None,
            "foto": None,
        })
    return out


# ----------------------------------------------------------------------------
# VALUTAZIONE
# ----------------------------------------------------------------------------
def minuti_stimati(zona):
    z = zona.lower()
    migliore = None
    for chiave, minuti in TEMPI_ZONA.items():
        if chiave in z and (migliore is None or len(chiave) > migliore[0]):
            migliore = (len(chiave), minuti)
    return migliore[1] if migliore else None


def euro(n):
    return f"{n:,}".replace(",", ".")


def valuta(a):
    """Ritorna (esito, check). esito: True = tutto ok, None = dati mancanti,
    False = almeno un criterio non rispettato."""
    check, passa = [], True

    ok = a["prezzo"] <= MAX_AFFITTO
    check.append((ok, f"{euro(a['prezzo'])} SEK/mese (max {euro(MAX_AFFITTO)})"))
    passa &= ok

    if a["mq"] is not None:
        ok = a["mq"] >= MIN_MQ
        check.append((ok, f"{a['mq']} m²"))
        passa &= ok
    elif MIN_MQ <= 0:
        check.append((True, "Superficie non indicata (nessun limite)"))
    else:
        check.append((None, "Superficie non indicata"))

    testo = (a["titolo"] + " " + a["zona"]).lower()
    intero = not any(p in testo for p in PAROLE_STANZA)
    if SOLO_INTERI:
        check.append((intero, "Appartamento intero" if intero else "Sembra una stanza"))
        passa &= intero

    ing = a.get("ingresso")
    if ing:
        ok = INGRESSO_DA <= ing <= INGRESSO_A
        check.append((ok, f"Disponibile dal {ing}"))
        passa &= ok
    elif DATA_INDEFINITA_OK:
        check.append((True, "Data da concordare col proprietario"))
    else:
        check.append((None, "Data di ingresso non indicata"))

    foto = a.get("foto")
    if foto is None:  # HousingAnywhere e Homii non espongono il conteggio
        check.append((None, "Numero di foto sconosciuto"))
    else:
        ok = foto >= MIN_FOTO
        check.append((ok, f"{foto} foto" + ("" if ok else " — probabilmente solo esterni")))
        passa &= ok

    minuti = minuti_stimati(a["zona"])
    if minuti is None:
        check.append((None, f"Zona «{a['zona']}» non in tabella — verifica su sl.se"))
        if not AVVISA_ANCHE_SE_DUBBIO:
            passa = False
    else:
        ok = minuti <= MAX_MINUTI_UNI
        check.append((ok, f"~{minuti} min fino all'università (stima)"))
        passa &= ok

    if not passa:
        return False, check
    return (None if any(o is None for o, _ in check) else True), check


SIMBOLO = {True: "✅", False: "❌", None: "❓"}


def messaggio(a, check, esito):
    icona = {True: "🏠", None: "🔎", False: "⚠️"}[esito]
    r = [f"{icona} <b>Nuovo annuncio — {a['fonte']}</b>", ""]
    descr = []
    if a["stanze"]:
        descr.append(f"{a['stanze']} locali")
    if a["mq"]:
        descr.append(f"{a['mq']} m²")
    r.append(("Appartamento di " + ", ".join(descr) + f", zona {a['zona']}.")
             if descr else f"Appartamento in zona {a['zona']}.")
    if a.get("extra"):
        r.append(a["extra"].capitalize() + ".")
    r.append(f"<b>{euro(a['prezzo'])} SEK/mese</b>")
    if a.get("data"):
        r.append(f"<i>pubblicato il {a['data']}</i>")
    r += ["", "<b>Filtri:</b>"] + [f"{SIMBOLO[ok]} {t}" for ok, t in check]
    finale = {True: "Soddisfa tutti i criteri.",
              None: "Promettente, ma alcuni dati vanno verificati.",
              False: "Non rispetta tutti i criteri, ma ci va vicino."}[esito]
    r += ["", finale,
          f'<a href="{a["url"]}">Apri l\'annuncio</a>']
    return "\n".join(r)


def invia(testo):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not token or not chat:
        print(re.sub(r"<[^>]+>", "", testo), "\n" + "-" * 40)
        return
    try:
        SESSION.post(f"https://api.telegram.org/bot{token}/sendMessage",
                     data={"chat_id": chat, "text": testo, "parse_mode": "HTML"},
                     timeout=30)
    except Exception as e:
        print(f"[!] Telegram: {e}", file=sys.stderr)


def carica_stato():
    if STATO_FILE.exists():
        try:
            return json.loads(STATO_FILE.read_text())
        except Exception:
            pass
    return {}


def salva_stato(st):
    STATO_FILE.write_text(json.dumps(st))


def giro(silenzioso_al_primo_giro=True):
    visti = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
    primo = not visti and silenzioso_al_primo_giro
    annunci, errori = [], []

    try:
        annunci += fetch_qasa()
    except Exception as e:
        errori.append(f"Qasa: {e}")

    for url in URL_BOSTADSPORTAL + URL_HOMII + URL_HOUSINGANYWHERE:
        try:
            page = scarica(url, "sv-SE" if "homii" in url else None)
            annunci += (parse_bostadsportal(page) if "bostadsportal" in url
                        else parse_homii(page) if "homii" in url
                        else parse_housinganywhere(page))
        except Exception as e:
            errori.append(f"{url.split('/')[2]}: {e}")
        time.sleep(2)

    nuovi = 0
    for a in annunci:
        if a["id"] in visti:
            continue
        visti.add(a["id"])
        if a["fonte"] == "BostadsPortal" and a["prezzo"] <= MAX_AFFITTO:
            arricchisci_bostadsportal(a)
            time.sleep(1)
        esito, check = valuta(a)
        quasi = (a["prezzo"] <= MAX_AFFITTO and
                 sum(1 for ok, _ in check if ok is False) <= 1)
        manda = esito is True or (AVVISA_ANCHE_SE_DUBBIO and (esito is None or quasi))
        if manda and not primo:
            invia(messaggio(a, check, esito))
            nuovi += 1

    STATE_FILE.write_text(json.dumps(sorted(visti)))

    # riepilogo orario: parte solo se in quell'ora non è arrivato nessun alert
    stato = carica_stato()
    adesso = time.time()
    ultimo = stato.get("ultimo_messaggio", 0)
    if nuovi:
        stato["ultimo_messaggio"] = adesso      # il timer riparte da qui
    elif (not primo and RIEPILOGO_OGNI_MINUTI > 0
          and adesso - ultimo >= RIEPILOGO_OGNI_MINUTI * 60):
        invia("🔕 <b>Ricerca effettuata</b>\n"
              f"Nessun nuovo annuncio nell'ultima ora.\n"
              f"<i>{len(annunci)} annunci controllati su "
              f"{len(set(a['fonte'] for a in annunci))} portali.</i>")
        stato["ultimo_messaggio"] = adesso
    salva_stato(stato)
    print(f"{time.strftime('%H:%M')} — {len(annunci)} annunci letti, {nuovi} alert"
          + (" (primo giro: solo indicizzazione)" if primo else "")
          + ("  |  errori: " + "; ".join(errori) if errori else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    while True:
        giro(silenzioso_al_primo_giro=not args.test)
        if not args.loop:
            break
        time.sleep(15 * 60)
