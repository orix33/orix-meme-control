# -*- coding: utf-8 -*-
"""meme_control.py — die Kontrollgruppe zum Meme-Agenten (PAPER-ONLY).

WOZU DAS DA IST
---------------
`meme_agent.py` waehlt Coins nach einem Score aus und meldet eine Win-Rate.
Diese Zahl allein ist **wertlos**, egal wie sie aussieht. Eine Win-Rate von
40 % ist grossartig, wenn ein Zufallsgriff 10 % holt, und katastrophal, wenn
er 60 % holt. Ohne Vergleichsgruppe misst man nichts.

Genau daran sind am 11.08.2026 sieben Indikator-Strategien gestorben: Sie
sahen plausibel aus, bis `strategy_lab.py` eine Zufalls-Kontrolle daneben
gestellt hat. Keine war unterscheidbar. Dasselbe Verfahren, hier auf
Meme-Coins angewandt.

DER VERSUCHSAUFBAU
------------------
Bei jedem Scan werden DREI Arme gleichzeitig eroeffnet, aus demselben
Kandidaten-Topf, zum selben Zeitpunkt, mit identischen Ausstiegsregeln:

  ARM "alt"        — die k bestbewerteten nach dem Chart-Scorer aus
                     `meme_agent._score()` (Preis, Volumen, Alter, Buy-Ratio)
  ARM "neu"        — die k bestbewerteten nach `meme_signals.score_v2()`
                     (Social-Praesenz, Creator-Self-Buy, Halterverteilung),
                     nach harten Ausschluessen
  ARM "kontrolle"  — k zufaellig gezogene aus demselben Topf

Der EINZIGE Unterschied ist die Auswahlregel. Alles andere ist gleich:
gleicher Zeitpunkt, gleicher Markt, gleiches Take-Profit, gleicher Stop-Loss,
gleicher Horizont, gleiche Messmethode. Was an Differenz uebrig bleibt, kann
nur von der Auswahl kommen.

Die Arme sind **disjunkt** — kein Pool liegt in zweien. Sonst waeren die
Stichproben nicht unabhaengig und der Welch-Test nicht anwendbar.

DIE FRAGE, DIE DAS BEANTWORTET
------------------------------
Nicht "verdient man mit Meme-Coins Geld" — sondern die schaerfere:
**Traegt eine dieser Auswahlregeln Information, oder waere Wuerfeln genauso
gut?**

Faellt die Antwort "genauso gut" aus, ist das kein Grund aufzuhoeren. Es ist
der Beweis, dass die Arbeit an den Merkmalen liegen muss und nicht an mehr
Calls.

WAS DAS NICHT KANN
------------------
* Keine Aussage ueber echte Ausfuehrung. Paper-Preise kennen weder Slippage
  noch Prioritaetsgebuehren noch die Frage, ob zum Einstiegskurs ueberhaupt
  jemand verkauft haette. Ein Vorteil hier ist eine NOTWENDIGE, keine
  hinreichende Bedingung.
* Kein Ersatz fuer Stichprobengroesse. Bei n < MIN_N_FUER_URTEIL gibt
  `report()` bewusst KEIN Urteil aus, sondern sagt, wie viele Beobachtungen
  noch fehlen.
* **Der Topf ist nicht neutral.** Alle drei Arme ziehen aus dem Kandidatenfeld,
  das die Filter von `meme_agent` uebrig lassen. Was diese Filter aussortieren,
  sieht auch der "neu"-Arm nie. Der Vergleich misst also "welche Auswahl
  innerhalb dieses Feldes ist besser", nicht "welche Auswahl im Meme-Markt
  ist besser". Am 31.08.2026 gemessen: 37 von 40 Pools scheitern allein an
  `MIN_VOLUME_1H_USD`.
* **Der "neu"-Arm laeuft eingeschraenkt.** Sein staerkstes Merkmal ist die
  Telegram-Praesenz (8,94x-Lift). In der bisherigen Stichprobe (n=8) hatte
  **kein einziger** Kandidat einen Telegram-Kanal. Der Scorer entscheidet
  damit faktisch nur ueber die schwaecheren Merkmale.

KEIN LIVE-TRADING. Dieses Modul kennt keinen Order-Pfad, keine Credentials
und keine Signatur. Es beobachtet und rechnet.
"""
from __future__ import annotations

import json
import math
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import meme_agent as MA
import meme_signals as MS

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
DATA_FILE = DATA_DIR / "meme_control.json"
DATA_DIR.mkdir(exist_ok=True)

# Ausstiegsregeln — bewusst identisch mit meme_agent, sonst vergleicht man
# zwei verschiedene Experimente statt zwei Auswahlregeln.
TAKE_PROFIT_PCT = MA.TAKE_PROFIT_PCT
STOP_LOSS_PCT = MA.STOP_LOSS_PCT
HORIZON_MINUTES = MA.HORIZON_MINUTES

PICKS_PRO_ARM = 2          # k je Arm und Scan (3 Arme -> 6 Kandidaten noetig)
MAX_OFFEN_PRO_ARM = 12
PREIS_BUDGET_PRO_LAUF = 14  # Rate Limit: max. so viele Preisabfragen je Update
MIN_N_FUER_URTEIL = 30      # darunter wird kein Urteil ausgegeben
MAX_ANREICHERUNG = 12       # so viele Kandidaten je Scan durch meme_signals

#: DREI Arme, nicht zwei.
#:
#:   alt        — der Chart-Scorer aus meme_agent.py (Preis, Volumen, Alter,
#:                Buy-Ratio). Bleibt unveraendert im Rennen.
#:   neu        — der Scorer nach Beleglage aus meme_signals.py (Social-Praesenz,
#:                Creator-Self-Buy, Halterverteilung) mit harten Ausschluessen.
#:   kontrolle  — Zufallszug aus demselben Topf.
#:
#: WARUM DER ALTE SCORER BLEIBT: Wuerde man ihn einfach ersetzen, haette man eine
#: plausible Regel durch eine andere plausible Regel getauscht und nichts gelernt.
#: Erst wenn `neu` den `alt`-Arm UND den Zufall messbar schlaegt, ist der Tausch
#: begruendet. Vorher waere er genau der Fehler, den dieses Projekt schon zu oft
#: gemacht hat.
_ARME = ("alt", "neu", "kontrolle")

#: Frueher hiess der Chart-Arm "signal". Alte Zustaende werden beim Laden
#: umbenannt, damit die bereits gesammelten Beobachtungen nicht verloren gehen.
_ALTNAMEN = {"signal": "alt"}


# ═══════════════════════════════════════════════════════════════════
#  STATE
# ═══════════════════════════════════════════════════════════════════

def _leer() -> dict:
    return {
        "version": 1,
        "erstellt": datetime.now(timezone.utc).isoformat(),
        "scans": 0,
        "last_scan": None,
        "offen": {a: [] for a in _ARME},
        "geschlossen": {a: [] for a in _ARME},
    }


def _load() -> dict:
    if DATA_FILE.exists():
        try:
            d = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            # Altnamen migrieren, bevor Faecher angelegt werden — sonst landen
            # die alten Beobachtungen in einem Arm, den niemand mehr ausliest.
            for topf in ("offen", "geschlossen"):
                d.setdefault(topf, {})
                for alt_name, neu_name in _ALTNAMEN.items():
                    if alt_name in d[topf]:
                        d[topf].setdefault(neu_name, [])
                        for pos in d[topf].pop(alt_name):
                            pos["arm"] = neu_name
                            d[topf][neu_name].append(pos)
            for arm in _ARME:
                d["offen"].setdefault(arm, [])
                d["geschlossen"].setdefault(arm, [])
            return d
        except Exception:
            pass
    return _leer()


def _save(state: dict) -> None:
    tmp = DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str),
                   encoding="utf-8")
    tmp.replace(DATA_FILE)   # atomar, damit ein paralleler Leser nie halb liest


# ═══════════════════════════════════════════════════════════════════
#  KANDIDATEN
# ═══════════════════════════════════════════════════════════════════

def kandidaten() -> tuple[list[dict], bool]:
    """Der gemeinsame Topf, aus dem ALLE Arme ziehen. -> (kandidaten, quelle_ok)

    Die harten Filter des Agenten gelten fuer jeden Arm. Das ist Absicht: Wenn
    die Kontrolle auch aus offensichtlich toten Pools ziehen duerfte, haetten
    die Auswahl-Arme einen Vorteil, der nichts mit ihrer Regel zu tun hat, und
    der Vergleich waere wertlos.

    WARUM ZWEI RUECKGABEWERTE: Eine leere Liste kann zwei voellig verschiedene
    Dinge heissen — "gerade nichts Passendes am Markt" oder "die API hat 429
    geantwortet". Genau diese Verwechslung hat in `meme_agent.py` 18 Calls
    verdorben. `quelle_ok` sagt, ob die Datenquelle ueberhaupt geantwortet hat.
    """
    # MEHRERE SEITEN, und das ist noetig, nicht bequem: Gemessen am 31.08.2026
    # scheitern **37 von 40** Pools einer einzelnen Seite an
    # `MIN_VOLUME_1H_USD` (10.000 $). Mit einer Seite bleiben typisch 2
    # Kandidaten uebrig — zu wenig, um drei Arme disjunkt zu bestuecken.
    #
    # Nebenbefund, der im Scorer selbst liegt: `MIN_VOLUME_1H_USD` und die
    # Belohnung fuer "Alter < 1h" arbeiten gegeneinander. Ein zehn Minuten
    # alter Pool KANN kein 10.000-$-Stundenvolumen haben, ausser er pumpt
    # bereits. Der Agent sieht neue Token also erst NACH dem Anstieg. Das ist
    # eine Eigenschaft seiner Filter, keine des Marktes — hier bewusst nicht
    # angetastet, weil sonst der "alt"-Arm nicht mehr misst, was er messen soll.
    roh = []
    quelle_ok = False
    urls = [f"https://api.geckoterminal.com/api/v2/networks/{MA._NETWORK}/new_pools?page={s}"
            for s in (1, 2, 3, 4)]
    urls.append(f"https://api.geckoterminal.com/api/v2/networks/{MA._NETWORK}/trending_pools")
    for i, u in enumerate(urls):
        if i:
            time.sleep(MA.PRICE_FETCH_SPACING_S)
        r = MA._get(u)
        if r.get("ok"):
            quelle_ok = True
            roh += r.get("data", {}).get("data", []) or []

    gesehen: set[str] = set()
    out: list[dict] = []
    for pool in roh:
        p = MA._norm_pool(pool)
        pa = p.get("pool_address")
        if not pa or pa in gesehen:
            continue
        gesehen.add(pa)
        score, signale, ausschluss = MA._score(p)
        if ausschluss:
            continue
        if p["vol_m5"] < MA.MIN_VOLUME_5M_USD:
            continue
        if not p.get("price_usd"):
            continue
        p["score"] = score
        p["signals"] = signale
        out.append(p)
    return out, quelle_ok


def _seed_fuer(state: dict) -> int:
    """Reproduzierbarer Zufall: gleicher Scan-Index -> gleiche Ziehung.

    Ohne festen Seed liesse sich ein Lauf nicht nachrechnen, und genau das
    braucht man, wenn das Ergebnis spaeter angezweifelt wird.
    """
    return 20260831 + int(state.get("scans", 0))


# ═══════════════════════════════════════════════════════════════════
#  SCAN — beide Arme gleichzeitig eroeffnen
# ═══════════════════════════════════════════════════════════════════

def _eroeffne(p: dict, arm: str, now: datetime, scan_idx: int) -> dict:
    return {
        "id": f"{arm[:3].upper()}-{now.strftime('%m%d-%H%M%S')}-{p['symbol'][:8]}",
        "arm": arm,
        "scan_idx": scan_idx,
        "opened_ts": now.isoformat(),
        "symbol": p["symbol"],
        "pool_address": p["pool_address"],
        "token_address": p.get("token_address", ""),
        "entry_price_usd": p["price_usd"],
        "score": p["score"],
        "signals": p.get("signals", []),
        "liquidity_usd": p.get("liquidity_usd"),
        "age_minutes": round(p.get("age_minutes", 0.0), 1),
        "status": "open",
        "current_price_usd": p["price_usd"],
        "pnl_pct": 0.0,
        "unknown_strikes": 0,
        "letzte_pruefung": None,
    }


def scan() -> dict:
    """Ein Versuchsdurchgang: Kandidaten holen, beide Arme bestuecken."""
    now = datetime.now(timezone.utc)
    state = _load()
    scan_idx = int(state.get("scans", 0))

    pool, quelle_ok = kandidaten()

    if not quelle_ok:
        # Die Datenquelle hat nicht geantwortet. Das ist KEIN Scan — der
        # Zaehler bleibt stehen, damit der Seed nicht weiterwandert und ein
        # ausgefallener Durchgang nicht wie ein leerer Markt aussieht.
        return {"ok": False, "eroeffnet": 0, "kandidaten": 0,
                "grund": "Datenquelle nicht erreichbar (vermutlich Rate Limit) "
                         "— kein Scan gezaehlt"}

    # Mindestens ein Pick je Arm muss moeglich sein, sonst ist der Durchgang
    # unbrauchbar — ein Arm ohne Zug verzerrt den Vergleich.
    noetig = len(_ARME)
    if len(pool) < noetig:
        # Zu wenige Kandidaten, um alle Arme disjunkt zu bestuecken. Lieber
        # gar nichts eroeffnen als einen unsauberen Durchgang protokollieren.
        state["scans"] = scan_idx + 1
        state["last_scan"] = now.isoformat()
        _save(state)
        return {"ok": True, "eroeffnet": 0, "kandidaten": len(pool),
                "grund": f"zu wenige Kandidaten (<{noetig} fuer {len(_ARME)} Arme)"}

    bereits = {c["pool_address"]
               for arm in _ARME
               for c in state["offen"][arm] + state["geschlossen"][arm]}
    frisch = [p for p in pool if p["pool_address"] not in bereits]
    if len(frisch) < noetig:
        state["scans"] = scan_idx + 1
        state["last_scan"] = now.isoformat()
        _save(state)
        return {"ok": True, "eroeffnet": 0, "kandidaten": len(pool),
                "neu_im_topf": len(frisch),
                "grund": f"zu wenige NEUE Kandidaten (<{noetig})"}

    # k adaptiv: nimm, was das Kandidatenfeld hergibt, aber fuer JEDEN Arm
    # gleich viel. Ungleiche Armgroessen wuerden den Vergleich verzerren.
    k = max(1, min(PICKS_PRO_ARM, len(frisch) // len(_ARME)))

    # ── Anreicherung fuer den "neu"-Arm ───────────────────────────
    # Kostet zwei HTTP-Anfragen je Token, deshalb gedeckelt. Bewertet wird der
    # ganze Topf — gezogen wird spaeter disjunkt, damit die Stichproben
    # unabhaengig bleiben und der Welch-Test anwendbar ist.
    zu_pruefen = [p for p in frisch if p.get("token_address")][:MAX_ANREICHERUNG]
    angereichert = {}
    if zu_pruefen:
        try:
            angereichert = MS.enrich_viele([p["token_address"] for p in zu_pruefen])
        except Exception as e:
            angereichert = {}
            log_hinweis = f"Anreicherung fehlgeschlagen: {type(e).__name__}"
        else:
            log_hinweis = ""
    else:
        log_hinweis = "keine Mint-Adressen im Kandidatenfeld"

    bewertet = []
    for p in zu_pruefen:
        s = angereichert.get(p["token_address"]) or {}
        # Alter mitgeben: Konzentration ist bei jungen Pools bedeutungslos
        # (Rho(Alter, Top-10) = -0,76, gemessen 31.08.2026).
        alter = p.get("age_minutes")
        gruende = MS.ausschluss_gruende(s, alter_minuten=alter)
        punkte, warum = MS.score_v2(s, alter_minuten=alter)
        p["v2_score"] = punkte
        p["v2_warum"] = warum
        p["v2_ausschluss"] = gruende
        p["v2_signale"] = {
            "top10_pct": s.get("top10_pct"), "halter": s.get("halter"),
            "telegram": (s.get("socials") or {}).get("telegram"),
            "creator_haelt": s.get("creator_haelt"),
            "risiko_score": s.get("risiko_score"),
        }
        if not gruende:
            bewertet.append(p)

    # ── ZUGRIFFSREIHENFOLGE: rotierend, nicht fest ────────────────
    #
    # Frueher griff immer "alt" zuerst zu, dann "neu", zuletzt "kontrolle".
    # Das war ein Konstruktionsfehler mit zwei Wirkungen, beide am 31.08.2026
    # gemessen:
    #
    #   1. "neu" bekam nur die Reste. Gemessen: Er haette DREAM gewaehlt,
    #      bekam ihn aber nicht, weil "alt" vorher zugriff. In 2 von 3
    #      Durchgaengen ging er komplett leer aus — Hochrechnung bis n=30:
    #      58 Stunden gegen 10 fuer die anderen beiden.
    #   2. Schlimmer: Die Kontrolle zog IMMER zuletzt. Nehmen die beiden
    #      Auswahlregeln vorher die aussichtsreichsten Kandidaten weg, zieht
    #      der Zufallsarm aus einem systematisch schlechteren Rest — und
    #      beide Regeln sehen besser aus, als sie sind. Genau der Vorteil,
    #      den der Versuch messen soll, waere eingebaut gewesen.
    #
    # Die Reihenfolge rotiert deshalb mit dem Scan-Index. Ueber viele
    # Durchgaenge greift jeder Arm gleich oft zuerst zu; kein Arm hat einen
    # systematischen Vorteil. Deterministisch, damit ein Lauf nachrechenbar
    # bleibt.
    reihenfolge = _ARME[scan_idx % len(_ARME):] + _ARME[:scan_idx % len(_ARME)]

    nach_score = sorted(frisch, key=lambda x: (-x["score"], -x["vol_m5"]))
    nach_v2 = sorted(bewertet, key=lambda x: -x["v2_score"])
    rng = random.Random(_seed_fuer(state))
    zufall = frisch[:]
    rng.shuffle(zufall)

    quellen = {"alt": nach_score, "neu": nach_v2, "kontrolle": zufall}

    vergeben: set[str] = set()
    picks_je_arm = {}
    for arm in reihenfolge:
        frei = [p for p in quellen[arm] if p["pool_address"] not in vergeben]
        gewaehlt = frei[:k]
        picks_je_arm[arm] = gewaehlt
        vergeben |= {p["pool_address"] for p in gewaehlt}

    eroeffnet = {a: [] for a in _ARME}
    for arm, picks in picks_je_arm.items():
        if len(state["offen"][arm]) >= MAX_OFFEN_PRO_ARM:
            continue
        for p in picks:
            pos = _eroeffne(p, arm, now, scan_idx)
            if arm == "neu":
                pos["v2_score"] = p.get("v2_score")
                pos["v2_warum"] = p.get("v2_warum")
                pos["v2_signale"] = p.get("v2_signale")
            state["offen"][arm].append(pos)
            eroeffnet[arm].append(pos["symbol"])

    state["scans"] = scan_idx + 1
    state["last_scan"] = now.isoformat()
    _save(state)

    return {
        "ok": True,
        "ts": now.isoformat(),
        "kandidaten": len(pool),
        "neu_im_topf": len(frisch),
        "angereichert": len(angereichert),
        "durch_ausschluss_gefallen": len(zu_pruefen) - len(bewertet),
        "eroeffnet": {a: len(v) for a, v in eroeffnet.items()},
        "symbole": eroeffnet,
        "reihenfolge": list(reihenfolge),
        "score_alt": [p["score"] for p in picks_je_arm.get("alt", [])],
        "score_neu": [p.get("v2_score") for p in picks_je_arm.get("neu", [])],
        "hinweis": log_hinweis or None,
    }


# ═══════════════════════════════════════════════════════════════════
#  AUSGANG MESSEN — identisch fuer beide Arme
# ═══════════════════════════════════════════════════════════════════

def update() -> dict:
    """Prueft offene Positionen beider Arme. Budgetiert wegen Rate Limit."""
    now = datetime.now(timezone.utc)
    state = _load()

    # Aelteste zuerst pruefen, damit bei knappem Budget niemand verhungert.
    aufgaben = []
    for arm in _ARME:
        for pos in state["offen"][arm]:
            aufgaben.append((pos.get("letzte_pruefung") or "", arm, pos))
    aufgaben.sort(key=lambda x: x[0])
    aufgaben = aufgaben[:PREIS_BUDGET_PRO_LAUF]

    geschlossen = {a: 0 for a in _ARME}
    unbekannt = 0
    for i, (_, arm, pos) in enumerate(aufgaben):
        if i:
            time.sleep(MA.PRICE_FETCH_SPACING_S)
        zustand, preis, liq = MA.fetch_pool_state(pos["pool_address"])
        pos["letzte_pruefung"] = now.isoformat()

        if zustand == "unknown":
            pos["unknown_strikes"] = int(pos.get("unknown_strikes", 0)) + 1
            unbekannt += 1
            if pos["unknown_strikes"] >= MA.UNKNOWN_STRIKES_MAX:
                _schliessen(state, arm, pos, now, "unmeasurable", None,
                            f"{pos['unknown_strikes']}x nicht abfragbar — keine Wertung")
            continue

        if zustand == "gone":
            _schliessen(state, arm, pos, now, "rug", -100.0,
                        "Pool weg oder Liquiditaet null — Totalverlust")
            geschlossen[arm] += 1
            continue

        pos["unknown_strikes"] = 0
        pos["current_price_usd"] = preis
        pos["liquidity_usd_jetzt"] = liq

        # Liquiditaet VOR der PnL-Rechnung pruefen. Ein ausgeraeumter Pool
        # behaelt seinen letzten Kurs — am 31.08.2026 stand die Position "AI"
        # auf +17,31 %, waehrend im Pool 0,00000074 $ lagen (Start: 25.316 $).
        #
        # Ohne diese Pruefung wird ein Rug als GEWINN gebucht. Und weil der
        # "alt"-Arm nachweislich die konzentrierteren, rug-anfaelligeren Token
        # zieht, haette ihn genau das BESSER aussehen lassen — der Versuch
        # haette das Gegenteil der Wahrheit gemessen.
        liq_start = float(pos.get("liquidity_usd") or 0.0)
        abgezogen = liq_start > 0 and liq < liq_start * MA.DRAIN_ANTEIL
        if liq < MA.MIN_EXIT_LIQUIDITY_USD or abgezogen:
            schein = ((preis / pos["entry_price_usd"] - 1) * 100
                      if pos.get("entry_price_usd") else 0.0)
            _schliessen(state, arm, pos, now, "rug", -100.0,
                        "Liquiditaet %.2f $ (Start %.0f $) — kein Ausstieg. "
                        "Schein-PnL waere %+.1f%% gewesen." % (liq, liq_start, schein))
            geschlossen[arm] += 1
            continue

        entry = pos["entry_price_usd"] or 0.0
        if entry <= 0:
            _schliessen(state, arm, pos, now, "unmeasurable", None,
                        "Einstiegspreis fehlt — keine Wertung")
            continue
        pnl = (preis / entry - 1) * 100
        pos["pnl_pct"] = round(pnl, 2)
        alter_min = (now - datetime.fromisoformat(pos["opened_ts"])).total_seconds() / 60.0

        if pnl >= TAKE_PROFIT_PCT:
            _schliessen(state, arm, pos, now, "tp", TAKE_PROFIT_PCT,
                        f"Take-Profit erreicht (roh {pnl:+.1f}%)")
            geschlossen[arm] += 1
        elif pnl <= STOP_LOSS_PCT:
            _schliessen(state, arm, pos, now, "sl", STOP_LOSS_PCT,
                        f"Stop-Loss erreicht (roh {pnl:+.1f}%)")
            geschlossen[arm] += 1
        elif alter_min >= HORIZON_MINUTES:
            _schliessen(state, arm, pos, now, "expired", round(pnl, 2),
                        f"Horizont erreicht ({alter_min:.0f} min)")
            geschlossen[arm] += 1

    _save(state)
    return {"ok": True, "geprueft": len(aufgaben), "geschlossen": geschlossen,
            "unbekannt": unbekannt,
            "offen": {a: len(state["offen"][a]) for a in _ARME}}


def _schliessen(state, arm, pos, now, status, pnl, grund) -> None:
    pos["status"] = status
    pos["pnl_pct"] = pnl
    pos["closed_ts"] = now.isoformat()
    pos["close_reason"] = grund
    state["offen"][arm] = [p for p in state["offen"][arm] if p["id"] != pos["id"]]
    state["geschlossen"][arm].append(pos)


# ═══════════════════════════════════════════════════════════════════
#  STATISTIK
# ═══════════════════════════════════════════════════════════════════

def _renditen(state: dict, arm: str) -> list[float]:
    """Nur gewertete Ausgaenge. `unmeasurable` ist keine Beobachtung."""
    return [c["pnl_pct"] for c in state["geschlossen"][arm]
            if c.get("status") not in ("unmeasurable",) and c.get("pnl_pct") is not None]


def _kennzahlen(werte: list[float]) -> dict | None:
    n = len(werte)
    if n < 2:
        return {"n": n, "mittel": werte[0] if n else None, "sd": None, "se": None, "t": None}
    mittel = sum(werte) / n
    var = sum((x - mittel) ** 2 for x in werte) / (n - 1)
    sd = math.sqrt(var)
    se = sd / math.sqrt(n)
    return {"n": n, "mittel": mittel, "sd": sd, "se": se,
            "t": (mittel / se) if se > 0 else None,
            "trefferquote": len([x for x in werte if x > 0]) / n}


def _welch(a: list[float], b: list[float]) -> dict | None:
    """Welch-t fuer zwei unabhaengige Stichproben ungleicher Varianz."""
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se <= 0:
        return None
    t = (ma - mb) / se
    # Welch-Satterthwaite-Freiheitsgrade
    zaehler = (va / len(a) + vb / len(b)) ** 2
    nenner = (va / len(a)) ** 2 / (len(a) - 1) + (vb / len(b)) ** 2 / (len(b) - 1)
    df = zaehler / nenner if nenner > 0 else float("nan")
    return {"differenz": ma - mb, "t": t, "df": df, "se": se}


def benoetigte_stichprobe(effekt: float, sd: float, alpha_t: float = 2.0,
                          power_z: float = 0.84) -> int | None:
    """Wie viele Beobachtungen JE ARM braucht es, um `effekt` zu zeigen?

    Naeherung fuer den Zweistichprobenvergleich:
        n je Arm ~ 2 * ((z_alpha + z_power) * sd / effekt)^2
    mit z_alpha = 2 (entspricht der Hausregel |t| > 2) und z_power = 0,84
    (80 % Testmacht). Das ist eine Groessenordnung, keine Praezisionsangabe —
    aber sie beantwortet die Frage, die sonst nie gestellt wird: reicht das,
    was wir haben, ueberhaupt fuer ein Urteil?
    """
    if not effekt or effekt == 0 or not sd or sd <= 0:
        return None
    return int(math.ceil(2 * (((alpha_t + power_z) * sd) / abs(effekt)) ** 2))


def report(als_text: bool = True):
    state = _load()
    renditen = {a: _renditen(state, a) for a in _ARME}
    kennz = {a: _kennzahlen(renditen[a]) for a in _ARME}

    # Beide Auswahlregeln gegen den Zufall — das ist der eigentliche Test.
    # "alt gegen neu" waere ein Vergleich zweier Vermutungen; erst der Zufall
    # sagt, ob ueberhaupt eine von beiden Information traegt.
    vergleiche = {
        "alt vs kontrolle": _welch(renditen["alt"], renditen["kontrolle"]),
        "neu vs kontrolle": _welch(renditen["neu"], renditen["kontrolle"]),
        "neu vs alt": _welch(renditen["neu"], renditen["alt"]),
    }

    ungewertet = {a: len([c for c in state["geschlossen"][a]
                          if c.get("status") == "unmeasurable"]) for a in _ARME}

    n_min = min((kennz[a]["n"] if kennz[a] else 0) for a in _ARME)
    if n_min < MIN_N_FUER_URTEIL:
        urteil = (f"KEIN URTEIL. Kleinster Arm hat n={n_min}, noetig sind "
                  f"mindestens {MIN_N_FUER_URTEIL} je Arm. Bis dahin ist jede "
                  f"Aussage ueber einen Vorteil geraten.")
    else:
        teile = []
        for name in ("alt vs kontrolle", "neu vs kontrolle"):
            w = vergleiche[name]
            if w is None:
                teile.append(f"{name}: zu wenig Varianz")
            elif abs(w["t"]) < 2:
                teile.append(f"{name}: kein Unterschied (t={w['t']:+.2f})")
            elif w["t"] > 0:
                teile.append(f"{name}: SCHLAEGT ZUFALL (t={w['t']:+.2f}, "
                             f"{w['differenz']:+.2f} Pp)")
            else:
                teile.append(f"{name}: SCHLECHTER als Zufall (t={w['t']:+.2f})")
        urteil = " | ".join(teile)

    noetig = None
    w_neu = vergleiche["neu vs kontrolle"]
    if w_neu and kennz["neu"] and kennz["neu"].get("sd"):
        noetig = benoetigte_stichprobe(w_neu["differenz"], kennz["neu"]["sd"])

    erg = {"ok": True, "arme": kennz, "vergleiche": vergleiche,
           "ungewertet": ungewertet, "urteil": urteil,
           "benoetigte_n_je_arm": noetig,
           "offen": {a: len(state["offen"][a]) for a in _ARME},
           "scans": state.get("scans", 0)}
    if not als_text:
        return erg

    beschriftung = {
        "alt": "alt (Chart)",
        "neu": "neu (Beleglage)",
        "kontrolle": "kontrolle (Zufall)",
    }
    z = ["=" * 78,
         "MEME-CONTROL — zwei Auswahlregeln gegen den Zufall, gleicher Topf",
         "=" * 78,
         f"Scans: {erg['scans']}   offen: " +
         ", ".join(f"{a} {erg['offen'][a]}" for a in _ARME),
         "",
         f"  {'Arm':20}{'n':>5}{'Ø Rendite':>12}{'Treffer':>10}{'sd':>10}{'t vs 0':>9}",
         "  " + "-" * 68]
    for a in _ARME:
        k = kennz[a]
        if not k or not k["n"]:
            z.append(f"  {beschriftung[a]:20}{0:>5}{'—':>12}{'—':>10}{'—':>10}{'—':>9}")
            continue
        m = f"{k['mittel']:+.2f}%" if k["mittel"] is not None else "—"
        tq = f"{k['trefferquote']*100:.0f}%" if k.get("trefferquote") is not None else "—"
        sd = f"{k['sd']:.2f}" if k.get("sd") else "—"
        t = f"{k['t']:+.2f}" if k.get("t") is not None else "—"
        z.append(f"  {beschriftung[a]:20}{k['n']:>5}{m:>12}{tq:>10}{sd:>10}{t:>9}")
    z.append("")
    for name, w in vergleiche.items():
        if w:
            z.append(f"  Welch-t {name:20} {w['t']:+6.2f}  "
                     f"(df {w['df']:.1f}, {w['differenz']:+.2f} Pp)")
        else:
            z.append(f"  Welch-t {name:20}     —  (zu wenig Daten)")
    z.append("")
    z.append("  Ungewertet (unmessbar): " +
             ", ".join(f"{a} {ungewertet[a]}" for a in _ARME))
    if noetig:
        z.append(f"  Fuer ein belastbares Urteil bei diesem Effekt: "
                 f"~{noetig} Calls JE ARM")
    z.append("")
    z.append("  " + urteil)
    z.append("=" * 78)
    return "\n".join(z)


# ═══════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════

def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    befehl = argv[1] if len(argv) > 1 else "report"
    if befehl == "scan":
        print(json.dumps(scan(), indent=2, ensure_ascii=False))
    elif befehl == "update":
        print(json.dumps(update(), indent=2, ensure_ascii=False))
    elif befehl == "zyklus":
        print(json.dumps(update(), indent=2, ensure_ascii=False))
        print(json.dumps(scan(), indent=2, ensure_ascii=False))
        print()
        print(report())
    else:
        print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
