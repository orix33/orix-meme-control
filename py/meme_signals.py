# -*- coding: utf-8 -*-
"""meme_signals.py — die belegten Signale beschaffen (READ-ONLY).

WARUM DIESES MODUL EXISTIERT
----------------------------
Der Scorer in `meme_agent.py` bewertet Preis, Volumen, Alter und Buy-Ratio. Die
Studienlage sagt, dass genau diese Chart-Merkmale **schwach** sind, und benennt
zwei deutlich staerkere Signale, die vor dem Start feststehen:

  * **Social-Praesenz** — Telegram-Kanal vorhanden: 1,485 % Graduierung gegen
    0,166 % ohne. Das ist ein **8,94x-Lift**, der groesste bekannte Einzeleffekt.
    Alle drei Kanaele: 1,919 % gegen 0,110 % (17,4x).
  * **Creator-Self-Buy** — Start-Marktkapitalisierung ueber 30 SOL: Hazard-Ratio
    **4,51**. Das oberste Quartil graduiert mit 0,634 %.

  Quelle: ArXiv 2607.02823, 832.941 Launches, Mai–Juni 2026.
  Siehe `orix.GEHIRN/07 - Meme Trading/Analysis/Akademische-Studien-2024-2026.md`

Dazu die Rug-Merkmale aus der Praxis (Halterkonzentration, Insider-Netzwerke,
Mint-/Freeze-Authority), die der Scorer ebenfalls nicht kennt.

WOHER DIE DATEN KOMMEN — alles frei, kein API-Schluessel
--------------------------------------------------------
  1. `https://api.rugcheck.xyz/v1/tokens/<MINT>/report`
     → Halter, Creator, Insider, Authorities, Launchpad, `tokenMeta.uri`
  2. Das Off-Chain-Metadaten-JSON hinter `tokenMeta.uri` (meist IPFS)
     → `telegram`, `twitter`, `website`

  Am 31.08.2026 an vier echten Tokens geprueft: beide antworten HTTP 200.

WAS DABEI ZU BEACHTEN IST
-------------------------
* **Leere Felder sind der Normalfall.** Das Metadaten-JSON enthaelt oft die
  Schluessel `telegram`/`twitter`/`website` mit leerem Wert. Nur ein **nicht
  leerer** Wert zaehlt als Praesenz. Wer auf `"telegram" in meta` prueft statt auf
  den Inhalt, misst Unsinn.
* **`creatorBalance` ist ein Stellvertreter, nicht die Studien-Groesse.** Die
  Studie misst die Start-Marktkapitalisierung in SOL; hier steht der Token-Bestand
  des Creators in rohen Einheiten, ohne Dezimalstellen. Als **binaeres** Merkmal
  (haelt der Creator etwas: ja/nein) ist es brauchbar, als Betrag nicht
  vergleichbar. Genau so wird es unten verwendet.
* **Zwei HTTP-Anfragen je Token.** Deshalb der Cache und die harte Drosselung —
  sonst laeuft das in dieselben 429er, die schon einmal 18 Calls verdorben haben.

KEIN LIVE-TRADING. Dieses Modul liest oeffentliche Daten und rechnet.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
CACHE_FILE = DATA_DIR / "meme_signals_cache.json"
DATA_DIR.mkdir(exist_ok=True)

_UA = "ORIX/2.0 MemeSignals (research, read-only)"
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1/tokens"

#: Anreicherung altert langsam — Socials und Creator aendern sich nach dem Start
#: praktisch nicht, Halterzahlen schon. Eine Stunde ist der Kompromiss.
CACHE_TTL_S = 3600
#: Abstand zwischen HTTP-Anfragen. Lieber langsam als 429.
REQUEST_SPACING_S = 1.5
HTTP_TIMEOUT_S = 20.0

_letzte_anfrage = 0.0


# ═══════════════════════════════════════════════════════════════════
#  HTTP
# ═══════════════════════════════════════════════════════════════════

def _drossel() -> None:
    global _letzte_anfrage
    delta = time.time() - _letzte_anfrage
    if delta < REQUEST_SPACING_S:
        time.sleep(REQUEST_SPACING_S - delta)
    _letzte_anfrage = time.time()


def _get_json(url: str) -> tuple[str, dict | None]:
    """("ok", daten) | ("gone", None) | ("unknown", None).

    Dieselbe Drei-Zustands-Logik wie `meme_agent.fetch_pool_state` — aus dem
    gleichen Grund: ein Fehler beim Abrufen ist kein Messwert.
    """
    _drossel()
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return "ok", json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return ("gone", None) if e.code == 404 else ("unknown", None)
    except Exception:
        return "unknown", None


# ═══════════════════════════════════════════════════════════════════
#  CACHE
# ═══════════════════════════════════════════════════════════════════

def _cache_laden() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _cache_schreiben(cache: dict) -> None:
    try:
        tmp = CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False, default=str),
                       encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
#  ANREICHERUNG
# ═══════════════════════════════════════════════════════════════════

def _nicht_leer(wert) -> bool:
    """Ein Social-Feld zaehlt nur, wenn wirklich etwas drinsteht."""
    return bool(wert) and isinstance(wert, str) and wert.strip() not in ("", "-", "n/a")


def _socials_aus_metadaten(uri: str) -> dict:
    if not uri or not uri.startswith(("http://", "https://")):
        return {"erreichbar": False}
    zustand, meta = _get_json(uri)
    if zustand != "ok" or not isinstance(meta, dict):
        return {"erreichbar": False}
    return {
        "erreichbar": True,
        "telegram": _nicht_leer(meta.get("telegram")),
        "twitter": _nicht_leer(meta.get("twitter")),
        "website": _nicht_leer(meta.get("website")),
        "beschreibung": _nicht_leer(meta.get("description")),
    }


def enrich(mint: str, cache: dict | None = None) -> dict:
    """Alle belegten Signale zu einem Token. Nie eine Exception nach aussen.

    `vollstaendig` sagt, ob wirklich beide Quellen geantwortet haben. Ist es
    False, darf der Scorer daraus KEINE Sicherheit ableiten — fehlende Daten
    sind nicht dasselbe wie unauffaellige Daten.
    """
    eigener_cache = cache is None
    cache = _cache_laden() if eigener_cache else cache

    treffer = cache.get(mint)
    if treffer:
        try:
            alter = time.time() - float(treffer.get("_ts", 0))
            if alter < CACHE_TTL_S:
                return treffer
        except (TypeError, ValueError):
            pass

    ergebnis = {
        "mint": mint,
        "_ts": time.time(),
        "abgerufen": datetime.now(timezone.utc).isoformat(),
        "vollstaendig": False,
    }

    zustand, r = _get_json(f"{RUGCHECK_BASE}/{mint}/report")
    if zustand != "ok" or not isinstance(r, dict):
        ergebnis["rugcheck_zustand"] = zustand
        if eigener_cache:
            cache[mint] = ergebnis
            _cache_schreiben(cache)
        return ergebnis

    th = r.get("topHolders") or []

    def pct(h):
        try:
            return float(h.get("pct") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    creator_balance = r.get("creatorBalance")
    try:
        creator_haelt = float(creator_balance or 0) > 0
    except (TypeError, ValueError):
        creator_haelt = False

    ergebnis.update({
        "rugcheck_zustand": "ok",
        "risiko_score": r.get("score_normalised"),   # HOCH = gefaehrlich
        "rugged_flag": bool(r.get("rugged")),
        "top1_pct": round(pct(th[0]), 2) if th else None,
        "top10_pct": round(sum(pct(h) for h in th[:10]), 2) if th else None,
        "halter": r.get("totalHolders"),
        "creator": r.get("creator"),
        "creator_haelt": creator_haelt,
        "creator_balance_roh": creator_balance,
        "insider_netzwerke": len(r.get("insiderNetworks") or []),
        "graph_insider": r.get("graphInsidersDetected"),
        "mint_authority": bool(r.get("mintAuthority")),
        "freeze_authority": bool(r.get("freezeAuthority")),
        "lp_locked_pct": r.get("lpLockedPct"),
        "liquiditaet_rugcheck": r.get("totalMarketLiquidity"),
        "launchpad": ((r.get("launchpad") or {}).get("platform")
                      if isinstance(r.get("launchpad"), dict) else None),
    })

    uri = ((r.get("tokenMeta") or {}).get("uri")) or ""
    soc = _socials_aus_metadaten(uri)
    ergebnis["socials"] = soc
    ergebnis["vollstaendig"] = bool(soc.get("erreichbar")) and th != []

    if eigener_cache:
        cache[mint] = ergebnis
        _cache_schreiben(cache)
    return ergebnis


def enrich_viele(mints: list[str]) -> dict[str, dict]:
    """Mehrere Tokens, ein Cache-Schreibvorgang."""
    cache = _cache_laden()
    out = {}
    for m in mints:
        out[m] = enrich(m, cache=cache)
        cache[m] = out[m]
    _cache_schreiben(cache)
    return out


# ═══════════════════════════════════════════════════════════════════
#  BEWERTUNG NACH BELEGLAGE
# ═══════════════════════════════════════════════════════════════════

#: Ab hier gilt Halterkonzentration als aussagekraeftig. Darunter nicht.
#:
#: GEMESSEN am 31.08.2026, 28 Tokens aus zwei Altersgruppen:
#:
#:     Spearman-Rho(Alter, Top-10-Konzentration) = **-0,76**
#:
#:     new_pools      (Alter ~0)     Median 100,0 %   unter 90 %:  1 von 14
#:     trending_pools (6 h–5350 h)   Median  29,1 %   unter 90 %: 14 von 14
#:
#: Eine feste Schwelle von 90 % trennt damit nicht Gefahr von Sicherheit,
#: sondern **jung von alt**. Sie haette jeden frischen Token verworfen und
#: jeden etablierten durchgelassen — das ist kein Risikofilter, das ist eine
#: Segmentwahl. Die erste Fassung dieses Moduls hatte genau diesen Fehler.
#:
#: Grund dahinter: Solange ein Token auf der Bonding-Kurve steht, haelt die
#: Kurve das Angebot. Konzentration faellt erst mit der Graduierung — und die
#: erreichen 0,198 % der Token (siehe Vault: Akademische-Studien-2024-2026).
REIFE_MINUTEN = 360.0

#: Innerhalb der jungen Kohorte gibt es trotzdem Variation, und zwar in der
#: HALTERZAHL: in der Stichprobe 3 bis 201, Faktor 70, bei praktisch gleichem
#: Alter. Das ist ein Merkmal, das nicht bloss das Alter widerspiegelt.
JUNG_MIN_HALTER = 25


def ausschluss_gruende(s: dict, alter_minuten: float | None = None) -> list[str]:
    """Harte Ausschluesse. Alterssensitiv — siehe REIFE_MINUTEN.

    Nur Merkmale, die **unabhaengig vom Alter** ein rotes Tuch sind, fuehren
    hier zum Ausschluss. Alles Altersabhaengige gehoert in `score_v2()`, wo es
    gegen die eigene Kohorte gewichtet wird statt gegen eine erfundene Konstante.
    """
    g = []
    if not s.get("vollstaendig"):
        # Kein Freibrief: fehlende Daten heissen "nicht bewertbar", nicht "sauber".
        g.append("Signale unvollstaendig — nicht bewertbar")
        return g

    # ── altersunabhaengig: echte rote Tuecher ─────────────────────
    if s.get("rugged_flag"):
        g.append("RugCheck meldet 'rugged'")
    if s.get("mint_authority"):
        g.append("Mint-Authority aktiv (Angebot beliebig vermehrbar)")
    if s.get("freeze_authority"):
        g.append("Freeze-Authority aktiv (Verkauf blockierbar)")

    # ── altersabhaengig ───────────────────────────────────────────
    jung = alter_minuten is not None and alter_minuten < REIFE_MINUTEN
    t1, t10 = s.get("top1_pct"), s.get("top10_pct")
    h = s.get("halter")

    if jung:
        # Konzentration sagt hier nichts — jeder frische Pool liegt bei ~100 %.
        # Die Halterzahl schwankt dagegen um Faktor 70 und traegt Information.
        if isinstance(h, int) and h < JUNG_MIN_HALTER:
            g.append(f"nur {h} Halter fuer einen jungen Pool (< {JUNG_MIN_HALTER})")
    else:
        # Reifer Token: jetzt ist Konzentration aussagekraeftig.
        if t10 is not None and t10 > 90.0:
            g.append(f"Top-10 halten {t10:.0f} % trotz Alter (> 90 %)")
        if t1 is not None and t1 > 50.0:
            g.append(f"Ein Halter hat {t1:.0f} % (> 50 %)")
        if isinstance(h, int) and h < 100:
            g.append(f"nur {h} Halter (< 100)")
    return g


def score_v2(s: dict, alter_minuten: float | None = None) -> tuple[int, list[str]]:
    """Score nach Beleglage. Rueckgabe: (punkte, begruendungen).

    Die Gewichte folgen der Effektstaerke aus der Studie, nicht der Intuition:
    Social-Praesenz und Creator-Self-Buy tragen am meisten, Verteilung danach.
    Chart-Merkmale kommen hier bewusst NICHT vor — die stecken im alten Scorer,
    und genau der Unterschied soll gemessen werden.

    ALTERSSENSITIV: Verteilungsmerkmale werden nur bei reifen Tokens bepunktet.
    Bei jungen waere "wenig konzentriert" faktisch identisch mit "alt" und der
    Score damit ein verkappter Altersfilter — gemessen: Rho = -0,76.
    Die Social- und Creator-Merkmale sind davon unberuehrt: sie stehen schon
    vor dem Start fest und haengen nicht am Alter.
    """
    if not s.get("vollstaendig"):
        return 0, ["Signale unvollstaendig"]

    punkte = 0
    warum = []
    soc = s.get("socials") or {}

    # ── Social-Praesenz: der staerkste belegte Effekt ──────────────
    if soc.get("telegram"):
        punkte += 4
        warum.append("Telegram vorhanden (8,94x-Lift)")
    if soc.get("twitter"):
        punkte += 2
        warum.append("Twitter vorhanden")
    if soc.get("website"):
        punkte += 1
        warum.append("Website vorhanden")
    if soc.get("telegram") and soc.get("twitter") and soc.get("website"):
        punkte += 2
        warum.append("alle drei Kanaele (17,4x-Lift)")

    # ── Creator-Self-Buy: Hazard-Ratio 4,51 ───────────────────────
    if s.get("creator_haelt"):
        punkte += 3
        warum.append("Creator haelt eigene Token (Stellvertreter fuer Self-Buy)")

    # ── Verteilung, gegen die eigene Alterskohorte ────────────────
    jung = alter_minuten is not None and alter_minuten < REIFE_MINUTEN
    t10 = s.get("top10_pct")
    h = s.get("halter")

    if jung:
        # Konzentration liegt hier bei praktisch allen um 100 % und traegt
        # keine Information. Die Halterzahl schon — Massstaebe deshalb aus
        # der jungen Kohorte (Stichprobe 31.08.: 3 bis 201, Median 14).
        if isinstance(h, int):
            if h >= 150:
                punkte += 3
                warum.append(f"{h} Halter — viel fuer einen jungen Pool")
            elif h >= 50:
                punkte += 1
                warum.append(f"{h} Halter (jung)")
    else:
        if t10 is not None:
            if t10 < 35.0:
                punkte += 3
                warum.append(f"Top-10 nur {t10:.0f} % bei reifem Token")
            elif t10 < 55.0:
                punkte += 1
                warum.append(f"Top-10 {t10:.0f} %")
        if isinstance(h, int):
            if h >= 5000:
                punkte += 2
                warum.append(f"{h} Halter")
            elif h >= 1000:
                punkte += 1
                warum.append(f"{h} Halter")

    # ── Abzuege ───────────────────────────────────────────────────
    ins = s.get("insider_netzwerke") or 0
    if ins > 3:
        punkte -= 2
        warum.append(f"{ins} Insider-Netzwerke")
    rs = s.get("risiko_score")
    if isinstance(rs, (int, float)) and rs >= 20:
        punkte -= 2
        warum.append(f"RugCheck-Risiko {rs} (hoch)")

    return punkte, warum


# ═══════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════

def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) > 1:
        mints = argv[1:]
    else:
        try:
            st = json.loads((DATA_DIR / "meme_agent.json").read_text(encoding="utf-8"))
            mints = [c["token_address"] for c in st.get("active_calls", [])
                     if c.get("token_address")]
        except Exception:
            mints = []
    if not mints:
        print("Keine Mints. Aufruf: meme_signals.py <MINT> [<MINT> ...]")
        return 1

    daten = enrich_viele(mints)
    print(f"{'Mint':12}{'v2':>5}{'Risiko':>8}{'Top10':>8}{'Halter':>8}"
          f"{'TG':>4}{'TW':>4}{'WEB':>5}{'Creator':>9}   Ausschluss")
    print("-" * 96)
    for m, s in daten.items():
        p, _ = score_v2(s)
        aus = ausschluss_gruende(s)
        soc = s.get("socials") or {}
        j = lambda b: "ja" if b else "—"
        print(f"{m[:10]:12}{p:>5}{str(s.get('risiko_score')):>8}"
              f"{str(s.get('top10_pct')):>8}{str(s.get('halter')):>8}"
              f"{j(soc.get('telegram')):>4}{j(soc.get('twitter')):>4}"
              f"{j(soc.get('website')):>5}{j(s.get('creator_haelt')):>9}   "
              f"{aus[0] if aus else 'keiner'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
