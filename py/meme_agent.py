# -*- coding: utf-8 -*-
"""meme_agent.py — Master-Meme-Agent: kurzfristige Meme-Coin-Calls (PAPER-ONLY).

WAS DAS IST
-----------
Der vierte Info-Kanal des ORIX-Ökosystems — und der einzige mit
KURZFRIST-Fokus (Minuten bis wenige Stunden). Er erzeugt KEINE echten Orders.
Er produziert **Paper-Calls**: dokumentierte Hypothesen „KAUFEN bei X, Exit bei
Y, Horizont Z", deren Ausgang der nächste Scan misst. Erst wenn die Papier-Quote
statistisch belastbar ist, entscheidet der Eigentümer — nie der Agent.

QUELLEN (alle frei, kein API-Key):
  1. GeckoTerminal /networks/solana/new_pools      — frischeste Pools
  2. GeckoTerminal /networks/solana/trending_pools — Aufmerksamkeits-Hotspots
  3. DexScreener  /token-profiles/latest/v1        — neu erstellte Token

HYPOTHESE (kurzfristig):
Der größte kurzfristige Move entsteht in den ersten Stunden eines Meme-Coins —
früher Aufmerksamkeits-/Volumen-Schub, bevor der Preis ihn eingepreist hat.
Signale: Pool-Alter, Volumen-Beschleunigung (5m vs 1h), Momentum, Buy-Ratio,
Liquiditäts-Sweet-Spot (genug, um nicht sofort zu ruggen, wenig genug fürs
Potenzial).

API (via app.py):
  GET  /api/meme/status     — Status + Stats
  GET  /api/meme/calls      — offene + geschlossene Calls
  POST /api/meme/scan       — Scan jetzt ausführen
  GET  /api/meme/journal    — letzte Journal-Einträge (Vault)

DAEMON: task_meme_scan alle 120s (Kurzfrist-Fokus braucht Frequenz).

RISIKO-GRENZE (nicht verhandelbar):
  TRADING_ENABLED steuert hier NICHT — Meme-Calls sind immer Paper. Es gibt
  keinen Pfad, der aus einem Call eine echte Order macht.
"""
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
DATA_FILE = DATA_DIR / "meme_agent.json"
DATA_DIR.mkdir(exist_ok=True)

VAULT_ROOT = HERE.parent / "orix.GEHIRN"
MEME_VAULT = VAULT_ROOT / "07 - Meme Trading"
META_DIR = MEME_VAULT / "_Meta"
TRADING_DIR = MEME_VAULT / "Trading"
ANALYSIS_DIR = MEME_VAULT / "Analysis"
PATTERNS_DIR = MEME_VAULT / "Patterns"
PROMPTS_DIR = MEME_VAULT / "Prompts"

_UA = "ORIX/2.0 MemeAgent"

# ── Kurzfrist-Konstanten ─────────────────────────────────────────
# Frische Solana-Memes haben in der ersten Stunde typisch $2k–$9k Liquidität.
# $10k als Schwelle hätte den ganzen Kurzfrist-Raum leer gefegt (Messung:
# Top-Kandidaten lagen bei $2.7k–$8.2k). Unter $2k ist ein Pool meist tot/leer.
# WICHTIG: $2k–$10k ist HOCH-RUG-RISIKO. Das wird gemessen, nicht empfohlen —
# die Calls sind Paper, und das Risiko steht im Journal + Regeln.md.
MIN_LIQUIDITY_USD = 2_000.0      # darunter: toter/leerer Pool
MAX_LIQUIDITY_USD = 2_000_000.0  # darüber: Meme-Potenzial meist weg
MIN_VOLUME_5M_USD = 2_000.0      # sonst kein Handel
MIN_VOLUME_1H_USD = 10_000.0
MAX_AGE_MINUTES = 24 * 60        # frischere Pools bevorzugt
CALL_THRESHOLD = 4               # Mindest-Score für einen Call
TAKE_PROFIT_PCT = 25.0           # kurzfristiges Pump-Ziel
STOP_LOSS_PCT = -15.0            # Memes: zu eng wird rausgewickt
HORIZON_MINUTES = 6 * 60         # Max-Haltedauer eines Kurzfrist-Calls
MAX_OPEN_CALLS = 8               # nicht mehr gleichzeitig offen

# GeckoTerminal Free erlaubt ~30 Anfragen/Minute. Ein Scan holt new_pools +
# trending + einen Preis je offenem Call — bei 8 offenen Calls sind das ueber
# 10 Anfragen im Block. Gemessen am 31.08.2026: schon bei 2,2 s Abstand kamen
# 429er. Deshalb Abstand zwischen den Preisabfragen.
PRICE_FETCH_SPACING_S = 2.5
# Wie oft ein Pool unmessbar sein darf, bevor der Call aussortiert wird.
# Aussortiert heisst NICHT "Verlust" — es heisst "keine Aussage moeglich".
UNKNOWN_STRIKES_MAX = 5

# Unter dieser Liquiditaet ist ein Ausstieg zum angezeigten Preis Fiktion.
# Gemessen: ein ausgeraeumter Pool behaelt seinen letzten Kurs, waehrend die
# Reserven auf Bruchteile eines Cents fallen. Wer dann den Preis bucht, bucht
# einen Gewinn auf eine Position, die niemand kaufen wuerde.
MIN_EXIT_LIQUIDITY_USD = 500.0
# Zusaetzlich relativ: faellt die Liquiditaet unter diesen Anteil ihres Standes
# beim Einstieg, ist der Pool abgezogen worden — unabhaengig vom Absolutwert.
DRAIN_ANTEIL = 0.05

_NETWORK = "solana"


# ══════════════════════════════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════════════════════════════

# Backoff für den EINEN Nachfass-Versuch in _get(). Gemessen am 31.08.2026:
# GeckoTerminal Free liegt bei ~30 Anfragen/Minute; ein einzelner 429er-Burst
# warf zuvor den GANZEN Scan weg, weil kein einziger Aufruf nachfasste. Ein
# 404 bleibt endgueltig — ein verschwundener Pool ist ein Ergebnis, kein Fehler.
_RETRY_BACKOFF_429 = 12.0    # s — bei Rate Limit: Wartezeit, bis das Fenster klärt
_RETRY_BACKOFF_OTHER = 4.0   # s — Timeout/5xx/Verbindungsabbruch: kurz nachfassen
_MAX_RETRIES = 1             # genau eine Wiederholung, nicht mehr


def _get(url: str, timeout: float = 15.0, retries: int = _MAX_RETRIES) -> dict:
    """GET mit JSON-Antwort. Nie Exception nach außen — immer dict.

    Transiente Fehler (429, 5xx, Timeout, Verbindungsabbruch) werden EINMAL
    nach kurzem Backoff nachgefasst. Damit ueberlebt ein Scan einen einzelnen
    Rate-Limit-Burst, statt den ganzen Durchgang zu verwerfen. 404 wird sofort
    beendet — den Status-Code MUSS der Aufrufer sehen: 404 (Pool weg) und 429
    (Rate Limit) sind voellig verschiedene Ereignisse. Sie zu einem anonymen
    Fehler zu verschmelzen hat 11 Calls faelschlich als Rug geschlossen.
    """
    for versuch in range(retries + 1):
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": _UA,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="replace")
                return {"ok": True, "code": r.status, "data": json.loads(raw)}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"ok": False, "code": 404, "error": "HTTP 404"}
            if versuch < retries and e.code in (429, 500, 502, 503, 504):
                time.sleep(_RETRY_BACKOFF_429 if e.code == 429 else _RETRY_BACKOFF_OTHER)
                continue
            return {"ok": False, "code": e.code, "error": "HTTP %d" % e.code}
        except Exception as e:
            if versuch < retries:
                time.sleep(_RETRY_BACKOFF_OTHER)
                continue
            return {"ok": False, "code": None, "error": str(e)}
    return {"ok": False, "code": None, "error": "retries exhausted"}


def _f(v, default=0.0):
    """float-Parser für API-Strings (GeckoTerminal liefert Strings)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════
#  DATENQUELLEN
# ══════════════════════════════════════════════════════════════════

def fetch_new_pools(network=_NETWORK, page=1):
    r = _get(f"https://api.geckoterminal.com/api/v2/networks/{network}/new_pools?page={page}")
    return r.get("data", {}).get("data", []) if r.get("ok") else []


def fetch_trending_pools(network=_NETWORK):
    r = _get(f"https://api.geckoterminal.com/api/v2/networks/{network}/trending_pools")
    return r.get("data", {}).get("data", []) if r.get("ok") else []


def fetch_latest_profiles():
    """DexScreener: neu registrierte Token-Profile (Discovery-Zusatz)."""
    r = _get("https://api.dexscreener.com/token-profiles/latest/v1")
    if not r.get("ok"):
        return []
    profiles = r.get("data", [])
    out = []
    for p in profiles[:50]:
        if p.get("chainId") == _NETWORK:
            out.append({
                "token_address": p.get("tokenAddress"),
                "url": p.get("url"),
                "description": (p.get("description") or "")[:120],
            })
    return out


def _first_pool(r: dict) -> dict | None:
    """Pool-Objekt aus einer GeckoTerminal-Antwort ziehen.

    Gotcha: Einzeln angefragte Pools kommen als OBJEKT zurück
    (`data.data` = {…}), Listen-Endpunkte als LISTE. Beide Formen abfangen,
    sonst wirft `[0]` einen KeyError.
    """
    d = r.get("data") if isinstance(r.get("data"), dict) else {}
    arr = d.get("data")
    if isinstance(arr, dict):
        return arr
    if isinstance(arr, list) and arr:
        return arr[0]
    return None


def fetch_pool_state(pool_address: str) -> tuple[str, float | None, float]:
    """Zustand: ("alive", preis, liq) | ("gone", None, liq) | ("unknown", None, liq).

    WARUM DIE LIQUIDITAET MIT ZURUECKKOMMT
    ---------------------------------------
    Gemessen am 31.08.2026 am Token "AI": Der Pool fiel binnen einer knappen
    Stunde von 25.316 $ Reserven auf **0,00000074 $** — und der zuletzt
    gehandelte Preis stand dabei **17 % hoeher** als beim Einstieg.

    Ein Rug nimmt die Liquiditaet mit und laesst den Preis stehen. Wer nur den
    Preis anschaut, bucht einen Totalverlust als Gewinn. Ohne diese Zahl war
    genau das der Fall.

    WARUM DREI ZUSTAENDE STATT None
    --------------------------------
    Die Vorgaengerfassung gab bei JEDEM Problem `None` zurueck, und der
    Aufrufer buchte das als Rug mit -15%. Gemessen am 31.08.2026: von 11 so
    geschlossenen Calls war **kein einziger** wirklich weg (0x HTTP 404).
    Fuenf liefen nachweislich weiter, einer davon bei +132%. Sechs lieferten
    HTTP 429 — Rate Limit, kein Rug.

    "unknown" ist deshalb kein Ergebnis, sondern das Eingestaendnis, dass
    gerade nichts gemessen werden konnte. Eine erfundene Zahl ist schlimmer
    als eine fehlende.
    """
    r = _get(f"https://api.geckoterminal.com/api/v2/networks/{_NETWORK}/pools/{pool_address}")
    if not r.get("ok"):
        # 404 = Pool existiert nicht mehr. Alles andere (429, Timeout, DNS,
        # 5xx) heisst nur: wir wissen es gerade nicht.
        return ("gone", None, 0.0) if r.get("code") == 404 else ("unknown", None, 0.0)
    pool = _first_pool(r)
    if not pool:
        return ("unknown", None, 0.0)
    attrs = pool.get("attributes", {}) or {}
    preis = _f(attrs.get("base_token_price_usd"), 0.0)
    liq = _f(attrs.get("reserve_in_usd"), 0.0)
    if preis > 0:
        return ("alive", preis, liq)
    # Preis 0: mit Liquiditaet ist das ein Datenaussetzer, ohne Liquiditaet
    # ein leergezogener Pool. Die alte Fassung warf beides in denselben Topf,
    # weil `_f(...) or None` die Null verschluckt hat.
    return ("gone", None, liq) if liq <= 0 else ("unknown", None, liq)


def fetch_pool_price(pool_address: str) -> float | None:
    """Nur der Preis. Duenner Wrapper — Zustand und Liquiditaet gehen verloren."""
    zustand, preis, _ = fetch_pool_state(pool_address)
    return preis if zustand == "alive" else None


def _resolve_token_address(pool_address: str) -> str:
    """Mint-Adresse (Contract) aus dem Pool nachziehen — Axiom handelt über die Mint.

    Ältere Calls (State-Version < aktuell) können ohne `token_address` gespeichert
    sein. Axiom identifiziert Tokens über die Mint-Adresse, nicht über den
    AMM-Pool — deshalb wird sie hier aus der Base-Token-Relation rekonstruiert.
    """
    r = _get(f"https://api.geckoterminal.com/api/v2/networks/{_NETWORK}/pools/{pool_address}")
    if not r.get("ok"):
        return ""
    pool = _first_pool(r)
    if not pool:
        return ""
    try:
        rel = pool.get("relationships", {}).get("base_token", {}).get("data", {})
        tid = rel.get("id", "")
        return tid.split("_", 1)[-1] if "_" in tid else tid
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════
#  POOL-NORMALISIERUNG
# ══════════════════════════════════════════════════════════════════

def _norm_pool(pool: dict) -> dict:
    """GeckoTerminal-Pool → flache, typisierte Struktur."""
    a = pool.get("attributes", {}) or {}
    vol = a.get("volume_usd", {}) or {}
    chg = a.get("price_change_percentage", {}) or {}
    tx = a.get("transactions", {}) or {}

    created = a.get("pool_created_at")
    age_min = 1e9
    if created:
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            age_min = max(0.0, (datetime.now(timezone.utc) - created_dt).total_seconds() / 60.0)
        except Exception:
            age_min = 1e9

    # Name "TOKEN / QUOTE" → Symbol
    name = a.get("name", "?")
    symbol = name.split(" / ")[0].strip() if " / " in name else (name.split("/")[0].strip() if "/" in name else "?")

    # Buy-Ratio aus den letzten 5 Minuten
    m5 = tx.get("m5", {}) or {}
    buys = int(m5.get("buys", 0) or 0)
    sells = int(m5.get("sells", 0) or 0)
    buy_ratio = (buys / (buys + sells)) if (buys + sells) > 0 else 0.5

    pool_id = pool.get("id", "")
    pool_address = pool_id.split("_", 1)[-1] if "_" in pool_id else pool_id

    # Token-Mint (Contract-Adresse) — das, was Axiom zum Handeln braucht.
    token_address = ""
    try:
        rel = pool.get("relationships", {}).get("base_token", {}).get("data", {})
        tid = rel.get("id", "")
        token_address = tid.split("_", 1)[-1] if "_" in tid else tid
    except Exception:
        token_address = ""

    return {
        "pool_address": pool_address,
        "token_address": token_address,
        "pool_id": pool_id,
        "symbol": symbol,
        "name": name,
        "price_usd": _f(a.get("base_token_price_usd")),
        "liquidity_usd": _f(a.get("reserve_in_usd")),
        "fdv_usd": _f(a.get("fdv_usd")),
        "vol_m5": _f(vol.get("m5")),
        "vol_h1": _f(vol.get("h1")),
        "vol_h24": _f(vol.get("h24")),
        "chg_m5": _f(chg.get("m5")),
        "chg_h1": _f(chg.get("h1")),
        "chg_h24": _f(chg.get("h24")),
        "buy_ratio": buy_ratio,
        "age_minutes": age_min,
        "source": "new" if "/new_pools" in pool_id else "trending",
    }


# ══════════════════════════════════════════════════════════════════
#  SCORING — kurzfristige Meme-Signale
# ══════════════════════════════════════════════════════════════════

def _score(p: dict) -> tuple[int, list[str], list[str]]:
    """Score + Signale + Ausschlussgründe."""
    score = 0
    signals = []
    reasons = []

    # Filter (hart)
    if p["liquidity_usd"] < MIN_LIQUIDITY_USD:
        reasons.append(f"Liquidität zu niedrig ({p['liquidity_usd']:.0f}$)")
    if p["liquidity_usd"] > MAX_LIQUIDITY_USD:
        reasons.append("Liquidität zu hoch (Meme-Potenzial weg)")
    if p["vol_h1"] < MIN_VOLUME_1H_USD:
        reasons.append(f"1h-Volumen zu niedrig ({p['vol_h1']:.0f}$)")
    if p["age_minutes"] > MAX_AGE_MINUTES:
        reasons.append(f"zu alt ({p['age_minutes']/60:.1f}h)")

    # Signale (weich, additiv)
    if p["age_minutes"] < 60:
        score += 2
        signals.append("Alter<1h")
    elif p["age_minutes"] < 360:
        score += 1
        signals.append("Alter<6h")

    # Volumen-Beschleunigung: 5m hochgerechnet deutlich über 1h
    if p["vol_h1"] > 0 and p["vol_m5"] > 0 and (p["vol_m5"] * 12) > (p["vol_h1"] * 1.5):
        score += 2
        signals.append("Volumen-Beschleunigung")

    if p["chg_m5"] > 2.0:
        score += 1
        signals.append(f"5m +{p['chg_m5']:.1f}%")
    if p["chg_h1"] > 5.0:
        score += 1
        signals.append(f"1h +{p['chg_h1']:.1f}%")

    if p["buy_ratio"] >= 0.60:
        score += 1
        signals.append(f"Buy-Ratio {p['buy_ratio']:.0%}")

    if MIN_LIQUIDITY_USD <= p["liquidity_usd"] <= MAX_LIQUIDITY_USD:
        score += 1
        signals.append("Liquiditäts-Sweet-Spot")

    return score, signals, reasons


# ══════════════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════════════

def _load() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "version": 2,
        "last_scan": None,
        "scans": 0,
        "active_calls": [],
        "closed_calls": [],
        "stats": {"calls": 0, "wins": 0, "losses": 0, "expired": 0,
                  "win_rate": 0.0, "avg_return_pct": 0.0, "total_return_pct": 0.0},
    }


def _save(state: dict):
    try:
        DATA_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass


def _recompute_stats(state: dict):
    """Statistik ueber die MESSBAREN Calls.

    `unmeasurable` und `quarantined` gehen NICHT ein. Ein Call, dessen Ausgang
    nie ermittelt werden konnte, ist kein Verlust und kein Gewinn — er ist
    keine Beobachtung. Ihn mitzuzaehlen war der Fehler, der die Bilanz am
    31.08.2026 um 102 Prozentpunkte verzogen hat.
    """
    alle = state["closed_calls"]
    ungewertet = {"unmeasurable", "quarantined"}
    closed = [c for c in alle if c.get("status") not in ungewertet]
    wins = sum(1 for c in closed if c["status"] == "tp")
    losses = sum(1 for c in closed if c["status"] in ("sl", "rug"))
    rugs = sum(1 for c in closed if c["status"] == "rug")
    expired = sum(1 for c in closed if c["status"] == "expired")
    rets = [c.get("pnl_pct") or 0.0 for c in closed]
    n = len(closed)
    state["stats"] = {
        "calls": n,
        "wins": wins,
        "losses": losses,
        "rugs": rugs,
        "expired": expired,
        "win_rate": round(wins / n * 100, 1) if n else 0.0,
        "avg_return_pct": round(sum(rets) / n, 2) if n else 0.0,
        "total_return_pct": round(sum(rets), 2),
        "ungewertet": len(alle) - n,
        "hinweis": ("Ohne Kontrollgruppe sagt die Win-Rate nichts. "
                    "Vergleich: meme_control.py"),
    }


# ══════════════════════════════════════════════════════════════════
#  SCAN
# ══════════════════════════════════════════════════════════════════

def scan() -> dict:
    """Ein Scan-Zyklus: Pools holen → scoren → Calls öffnen/schließen → persistieren."""
    now = datetime.now(timezone.utc)
    state = _load()

    # 1. Quellen
    new_pools = fetch_new_pools()
    trending = fetch_trending_pools()
    profiles = fetch_latest_profiles()
    seen = set()
    candidates = []
    for pool in (new_pools + trending):
        p = _norm_pool(pool)
        if p["pool_address"] and p["pool_address"] not in seen:
            seen.add(p["pool_address"])
            candidates.append(p)
    # Trending-Kennzeichnung nachziehen
    trending_addrs = {_norm_pool(x)["pool_address"] for x in trending}
    for p in candidates:
        if p["pool_address"] in trending_addrs:
            p["source"] = "trending"

    # 2. Scoren
    scored = []
    for p in candidates:
        s, sig, reas = _score(p)
        p["score"] = s
        p["signals"] = sig
        p["reject_reasons"] = reas
        scored.append(p)

    # 3. Offene Calls aktualisieren (Outcome-Check)
    newly_closed = _update_open_calls(state, now)

    # 4. Neue Calls öffnen (nur über Schwelle, ohne harte Ausschlüsse)
    opened = []
    open_count = len(state["active_calls"])
    for p in sorted(scored, key=lambda x: (-x["score"], -x["vol_m5"])):
        if open_count >= MAX_OPEN_CALLS:
            break
        if p["score"] < CALL_THRESHOLD or p["reject_reasons"]:
            continue
        if p["vol_m5"] < MIN_VOLUME_5M_USD:
            continue
        # Kein Call auf denselben Pool doppelt
        if any(c["pool_address"] == p["pool_address"] for c in state["active_calls"]):
            continue
        if any(c["pool_address"] == p["pool_address"] for c in state["closed_calls"]):
            continue
        call = _open_call(p, now)
        state["active_calls"].append(call)
        opened.append(call)
        open_count += 1

    # 5. Persistieren
    state["last_scan"] = now.isoformat()
    state["scans"] += 1
    _save(state)

    # 6. Vault-Journal (nur wenn etwas passiert ist)
    vault_written = 0
    for c in opened:
        if _write_journal(c):
            vault_written += 1
    for c in newly_closed:
        _write_journal(c)

    _recompute_stats(state)
    _save(state)

    return {
        "ok": True,
        "ts": now.isoformat(),
        "scanned": len(scored),
        "sources": {"new_pools": len(new_pools), "trending": len(trending), "profiles": len(profiles)},
        "above_threshold": len([p for p in scored if p["score"] >= CALL_THRESHOLD and not p["reject_reasons"]]),
        "opened": [c["id"] for c in opened],
        "closed": [c["id"] for c in newly_closed],
        "active": len(state["active_calls"]),
        "stats": state["stats"],
        "vault_written": vault_written,
        "top": [
            {"symbol": p["symbol"], "score": p["score"], "signals": p["signals"],
             "age_min": round(p["age_minutes"], 1), "chg_h1": p["chg_h1"],
             "vol_h1": p["vol_h1"], "liquidity": p["liquidity_usd"]}
            for p in sorted(scored, key=lambda x: -x["score"])[:5]
        ],
    }


def _open_call(p: dict, now: datetime) -> dict:
    entry = p["price_usd"] or 0.0
    return {
        "id": f"MEME-{now.strftime('%m%d-%H%M%S')}-{p['symbol'][:8]}-{p['pool_address'][:6]}",
        "opened_ts": now.isoformat(),
        "symbol": p["symbol"],
        "name": p["name"],
        "pool_address": p["pool_address"],
        "token_address": p.get("token_address", ""),
        "network": _NETWORK,
        "entry_price_usd": entry,
        "take_profit_pct": TAKE_PROFIT_PCT,
        "stop_loss_pct": STOP_LOSS_PCT,
        "horizon_minutes": HORIZON_MINUTES,
        "confidence": min(0.95, 0.40 + p["score"] * 0.10),
        "score": p["score"],
        "signals": p["signals"],
        "liquidity_usd_start": p.get("liquidity_usd"),
        "rationale": _rationale(p),
        "status": "open",
        "current_price_usd": entry,
        "pnl_pct": 0.0,
    }


def _recall(p: dict, k: int = 3) -> str:
    """Gedächtnis-Recall: die passendsten Vault-Notizen zum aktuellen Call.

    Fragt den Wissensgraphen (graph_engine) mit den Risiko-Merkmalen des Tokens
    ab und hängt die Titel der nächsten Notizen an die Begründung. Die Anfrage
    wird aus den Merkmalen gebaut — NICHT aus den rohen Signal-Strings, die in
    jedem Trade-Journal stehen und nur Selbst-Ähnlichkeit erzeugen. Schlägt der
    Recall fehl (kein Graph, leeres Vault), bleibt die Begründung unverändert —
    er ist additiv und kann einen Call nie verhindern.
    """
    try:
        import graph_engine
        age = float(p.get("age_minutes", 0) or 0)
        liq = float(p.get("liquidity_usd", 0) or 0)
        ctx = ["meme", "coin", "position", "sizing", "telegram", "narrative"]
        if age < 60:
            ctx += ["frisch", "erste", "stunde", "timing", "sniper", "bundler"]
        if liq < 10_000:
            ctx += ["rug", "liquidität", "risiko", "konzentration"]
        q = " ".join(ctx).strip()
        g = graph_engine.get_graph()
        hits = g.search(q, k + 3).get("hits", [])
        # Nur Wissens-Notizen: keine Trade-Journale (Trading/), kein Index-MOC.
        titles = [h["title"] for h in hits
                  if h.get("score", 0) > 0.02
                  and "/Trading/" not in h["id"]
                  and "Wissensbasis" not in h["title"]][:k]
        if not titles:
            return ""
        return "🧠 Gedächtnis: " + " · ".join(titles)
    except Exception:
        return ""


def _rationale(p: dict) -> str:
    parts = [f"Score {p['score']}/8", f"Alter {p['age_minutes']/60:.1f}h",
             f"Liq ${p['liquidity_usd']:,.0f}", f"1h-Vol ${p['vol_h1']:,.0f}"]
    if p["liquidity_usd"] < 10_000.0:
        parts.append("⚠️ <$10k Liquidität = hohes Rug-Risiko (gemessen, nicht empfohlen)")
    if p["signals"]:
        parts.append("Signale: " + ", ".join(p["signals"]))
    recall = _recall(p)
    if recall:
        parts.append(recall)
    return ". ".join(parts) + "."


def _update_open_calls(state: dict, now: datetime) -> list:
    """Prüft offene Calls gegen TP/SL/Horizont. Gibt geschlossene zurück."""
    closed = []
    remaining = []
    for i, c in enumerate(state["active_calls"]):
        if i:
            time.sleep(PRICE_FETCH_SPACING_S)   # Rate Limit respektieren
        zustand, cur, liq = fetch_pool_state(c["pool_address"])

        if zustand == "unknown":
            # Rate Limit oder Netzwerkfehler. Das ist KEIN Ergebnis. Der Call
            # bleibt offen und wird beim naechsten Scan erneut geprueft.
            c["unknown_strikes"] = int(c.get("unknown_strikes", 0)) + 1
            c["last_unknown_ts"] = now.isoformat()
            if c["unknown_strikes"] < UNKNOWN_STRIKES_MAX:
                remaining.append(c)
                continue
            # Dauerhaft unmessbar: aussortieren, aber NICHT als Verlust.
            # Dieser Call geht in keine Statistik ein.
            c["status"] = "unmeasurable"
            c["closed_ts"] = now.isoformat()
            c["close_reason"] = ("%dx nicht abfragbar (Rate Limit/Netzwerk) "
                                 "— kein Ergebnis, keine Wertung" % c["unknown_strikes"])
            c["pnl_pct"] = None
            state["closed_calls"].append(c)
            closed.append(c)
            continue

        if zustand == "gone":
            # Echter 404 oder Pool ohne Liquiditaet: Rug. Und ein Rug kostet
            # ALLES — bei gezogener Liquiditaet gibt es keinen Ausstieg zum
            # Stop-Loss. -15% zu buchen beschoenigt den schlimmsten Fall.
            c["status"] = "rug"
            c["current_price_usd"] = 0.0
            c["pnl_pct"] = -100.0
            c["closed_ts"] = now.isoformat()
            c["close_reason"] = "Pool weg oder Liquiditaet null — Totalverlust"
            state["closed_calls"].append(c)
            closed.append(c)
            continue

        c["unknown_strikes"] = 0
        c["current_price_usd"] = cur
        c["liquidity_usd_jetzt"] = liq

        # Liquiditaets-Pruefung VOR der PnL-Rechnung. Ein Pool, aus dem man
        # nicht mehr aussteigen kann, ist ein Totalverlust — egal, welchen
        # Kurs er noch anzeigt.
        liq_start = _f(c.get("liquidity_usd_start"), 0.0)
        abgezogen = liq_start > 0 and liq < liq_start * DRAIN_ANTEIL
        if liq < MIN_EXIT_LIQUIDITY_USD or abgezogen:
            c["status"] = "rug"
            c["pnl_pct"] = -100.0
            c["closed_ts"] = now.isoformat()
            c["close_reason"] = (
                "Liquiditaet %.2f $ (Start %.0f $) — kein Ausstieg moeglich. "
                "Angezeigter Kurs waere %+.1f%% gewesen."
                % (liq, liq_start,
                   ((cur / c["entry_price_usd"] - 1) * 100) if c.get("entry_price_usd") else 0.0))
            state["closed_calls"].append(c)
            closed.append(c)
            continue
        if c["entry_price_usd"] > 0:
            c["pnl_pct"] = round((cur / c["entry_price_usd"] - 1) * 100, 2)

        tp_price = c["entry_price_usd"] * (1 + c["take_profit_pct"] / 100)
        sl_price = c["entry_price_usd"] * (1 + c["stop_loss_pct"] / 100)
        age_min = (now - datetime.fromisoformat(c["opened_ts"])).total_seconds() / 60.0

        if cur >= tp_price:
            c["status"] = "tp"
            c["closed_ts"] = now.isoformat()
            c["close_reason"] = f"Take-Profit erreicht ({c['take_profit_pct']:+.0f}%)"
            state["closed_calls"].append(c)
            closed.append(c)
        elif cur <= sl_price:
            c["status"] = "sl"
            c["closed_ts"] = now.isoformat()
            c["close_reason"] = f"Stop-Loss erreicht ({c['stop_loss_pct']:+.0f}%)"
            state["closed_calls"].append(c)
            closed.append(c)
        elif age_min >= c["horizon_minutes"]:
            c["status"] = "expired"
            c["closed_ts"] = now.isoformat()
            c["close_reason"] = f"Horizont überschritten ({age_min:.0f}min), PnL {c['pnl_pct']:+.2f}%"
            state["closed_calls"].append(c)
            closed.append(c)
        else:
            remaining.append(c)

    state["active_calls"] = remaining
    return closed


# ══════════════════════════════════════════════════════════════════
#  VAULT — 07 - Meme Trading/
# ══════════════════════════════════════════════════════════════════

def ensure_vault_structure() -> dict:
    """Legt die Vault-Struktur an. Gibt {'ok', 'created': [...], 'root'} zurück."""
    created = []
    for d in (MEME_VAULT, META_DIR, TRADING_DIR, ANALYSIS_DIR, PATTERNS_DIR, PROMPTS_DIR):
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(d.name)
    seed_files = {
        META_DIR / "Ziele.md": (
            "# Ziele\n\nKurzfristige Meme-Coin-Calls finden, bevor der Preis die "
            "Aufmerksamkeit einpreist. Nur Paper-Calls — kein Live-Geld.\n"
            "Erfolg = statistisch belastbare Win-Rate über 100+ Calls, nicht einzelne Treffer.\n"
        ),
        META_DIR / "Regeln.md": (
            "# Regeln\n\n1. Kein Live-Trading — jeder Call ist Paper.\n"
            "2. Kurzfrist-Horizont: 0–6 Stunden.\n"
            "3. Jeder Call hat Entry, TP, SL, Horizont und Begründung.\n"
            "4. Verluste werden genauso dokumentiert wie Gewinne.\n"
            "5. Entscheidung über echtes Geld liegt beim Eigentümer, nie beim Agenten.\n"
        ),
        PATTERNS_DIR / "README.md": (
            "# Muster-Archiv\n\nHier werden bestätigte Muster dokumentiert — erst NACH "
            "statistischer Bestätigung, nicht nach einem Treffer.\n"
        ),
        PROMPTS_DIR / "README.md": (
            "# Prompt-Archiv\n\nGetestete Prompts für Analyse und Dokumentation.\n"
        ),
    }
    for fpath, content in seed_files.items():
        if not fpath.exists():
            fpath.write_text(content, encoding="utf-8")
            created.append(str(fpath.relative_to(MEME_VAULT)))
    return {"ok": True, "root": str(MEME_VAULT), "created": created}


def _write_journal(call: dict) -> bool:
    """Schreibt einen Journal-Eintrag in Trading/. True wenn geschrieben."""
    try:
        TRADING_DIR.mkdir(parents=True, exist_ok=True)
        date = call["opened_ts"][:10]
        fname = f"{date} - {call['symbol']} - {call['pool_address'][:6]}.md"
        fpath = TRADING_DIR / fname

        lines = [
            f"# {call['symbol']} — {call['id']}",
            "",
            "| Feld | Wert |",
            "|---|---|",
            f"| Status | {call['status']} |",
            f"| Geöffnet | {call['opened_ts']} |",
            f"| Entry | {call['entry_price_usd']} $ |",
            f"| Take-Profit | {call['take_profit_pct']:+.0f}% |",
            f"| Stop-Loss | {call['stop_loss_pct']:+.0f}% |",
            f"| Horizont | {call['horizon_minutes']/60:.0f}h |",
            f"| Confidence | {call['confidence']:.0%} |",
            f"| Score | {call['score']} |",
            f"| Signale | {', '.join(call['signals']) or '—'} |",
        ]
        if call.get("closed_ts"):
            lines += [
                f"| Geschlossen | {call['closed_ts']} |",
                f"| Grund | {call.get('close_reason', '')} |",
                f"| PnL | {call.get('pnl_pct', 0):+.2f}% |",
            ]
        if call.get("token_address"):
            lines.insert(4, f"| Token (Axiom) | `{call['token_address']}` |")
        lines += ["", "## Begründung", "", call["rationale"], "",
                  f"Pool: https://www.geckoterminal.com/{_NETWORK}/pools/{call['pool_address']}", "",
                  "## Ausführung (Axiom)", "",
                  "⚠️ PAPER-CALL — noch NICHT mit echtem SOL ausführen. Erst wenn die",
                  "Gesamt-Win-Rate über 100+ Calls belastbar ist, entscheidet der Eigentümer.",
                  f"- Token-Adresse: `{call.get('token_address', '—')}`",
                  f"- Entry ~{call['entry_price_usd']} $, TP {call['take_profit_pct']:+.0f}%, SL {call['stop_loss_pct']:+.0f}%",
                  f"- Horizont max {call['horizon_minutes']/60:.0f}h", ""]
        fpath.write_text("\n".join(lines), encoding="utf-8")
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════
#  STATUS / API-Helfer
# ══════════════════════════════════════════════════════════════════

def _t_detail(w):
    if not w or w.get("t") is None:
        return "noch nicht messbar"
    return f"t={w['t']:+.2f}"


def readiness() -> dict:
    """Go/No-Go-Schalter für echtes Geld — rein aus Messdaten, nie aus Gefühl.

    Liest die Drei-Arme-Messung (alt/neu/kontrolle) aus meme_control und setzt
    den Schalter nur dann auf Grün, wenn drei Bedingungen gleichzeitig stimmen:
      1. n ≥ 30 je Arm (unterhalb ist jede Aussage geraten)
      2. der `neu`-Arm schlägt den Zufall UND den alten Scorer (|t| ≥ 2)
      3. ein Live-Pfad existiert und ist freigeschaltet (heute: paper-only)
    Bis dahin bleibt er rot. Das ist der Schutz davor, dass echtes Geld auf
    einer nicht belegten Vermutung landet.
    """
    res = {
        "ok": True,
        "bereit": False,
        "stufe": "rot",  # rot | gelb | gruen
        "bedingungen": [],
        "arme": {},
        "offen": {},
        "urteil": "",
        "hinweis": (
            "Paper-Vorteil ist notwendig, aber nicht hinreichend: echte "
            "Ausführung kennt Slippage, Prioritätsgebühren und Exit-Liquidität, "
            "die Papierpreise nicht abbilden. Auch bei Grün gilt das "
            "Position-Sizing (kleine Positionen, hartes Tageslimit)."
        ),
    }
    try:
        import meme_control as MC
        rep = MC.report(als_text=False)
    except Exception as e:
        res["ok"] = False
        res["error"] = str(e)
        res["urteil"] = "Kontrollgruppe nicht lesbar — Schalter bleibt rot."
        return res

    arme = rep.get("arme", {})
    vergleiche = rep.get("vergleiche", {})
    res["arme"] = {a: arme.get(a) for a in ("alt", "neu", "kontrolle")}
    res["offen"] = rep.get("offen", {})
    res["urteil"] = rep.get("urteil", "")

    n_per_arm = {a: (arme.get(a) or {}).get("n", 0) for a in ("alt", "neu", "kontrolle")}
    n_min = min(n_per_arm.values())

    nv_k = vergleiche.get("neu vs kontrolle")
    nv_a = vergleiche.get("neu vs alt")
    neu_beat_k = bool(nv_k and nv_k.get("t") is not None and nv_k["t"] >= 2.0)
    neu_beat_a = bool(nv_a and nv_a.get("t") is not None and nv_a["t"] >= 2.0)

    # Bedingung 3: Live-Pfad. Der Meme-Agent hat bewusst KEINEN Order-Pfad;
    # diesen baut und schaltet erst der Eigentümer frei.
    live_pfad = False

    bed = [
        {"name": "n ≥ 30 je Arm",
         "erfuellt": n_min >= 30,
         "detail": f"kleinster Arm n={n_min} von 30"},
        {"name": "neu schlägt Zufall (t ≥ 2)",
         "erfuellt": neu_beat_k,
         "detail": _t_detail(nv_k)},
        {"name": "neu schlägt alten Scorer (t ≥ 2)",
         "erfuellt": neu_beat_a,
         "detail": _t_detail(nv_a)},
        {"name": "Live-Pfad freigeschaltet",
         "erfuellt": live_pfad,
         "detail": "paper-only — Entscheidung liegt beim Eigentümer"},
    ]
    res["bedingungen"] = bed
    res["bereit"] = all(b["erfuellt"] for b in bed)

    if res["bereit"]:
        res["stufe"] = "gruen"
    elif n_min >= 30 and neu_beat_k and neu_beat_a:
        res["stufe"] = "gelb"  # Evidenz da, nur der Live-Pfad fehlt noch
    else:
        res["stufe"] = "rot"

    return res


def get_status() -> dict:
    state = _load()
    _recompute_stats(state)
    return {
        "ok": True,
        "last_scan": state["last_scan"],
        "scans": state["scans"],
        "active_calls": len(state["active_calls"]),
        "closed_calls": len(state["closed_calls"]),
        "stats": state["stats"],
        "config": {
            "take_profit_pct": TAKE_PROFIT_PCT,
            "stop_loss_pct": STOP_LOSS_PCT,
            "horizon_minutes": HORIZON_MINUTES,
            "call_threshold": CALL_THRESHOLD,
            "max_open_calls": MAX_OPEN_CALLS,
            "network": _NETWORK,
        },
        "readiness": readiness(),
    }


def get_calls() -> dict:
    state = _load()
    _recompute_stats(state)
    return {
        "ok": True,
        "active": state["active_calls"],
        "closed": state["closed_calls"][-50:],  # letzte 50
        "stats": state["stats"],
    }


def get_journal(limit: int = 10) -> dict:
    """Letzte Journal-Einträge aus dem Vault."""
    try:
        if not TRADING_DIR.exists():
            return {"ok": True, "entries": []}
        files = sorted(TRADING_DIR.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
        return {"ok": True, "entries": [f.name for f in files]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_axiom_orders() -> dict:
    """Offene Paper-Calls als Axiom-fähige Ausführungsblöcke.

    Liefert Mint-Adresse + Entry/TP/SL/Horizont. Axiom identifiziert Tokens
    über die **Mint-Adresse** (Web-Suche + Telegram-Bot) — nicht über den
    AMM-Pool. Fehlt die Mint in einem älteren Call, wird sie hier aus dem
    Pool nachgezogen. Der Agent führt selbst KEINE Order aus (paper-only):
    es gibt keinen Pfad von diesem Block zu einer echten Axiom-Order.
    """
    state = _load()
    blocks = []
    for c in state["active_calls"]:
        mint = c.get("token_address", "") or _resolve_token_address(c["pool_address"])
        entry = c.get("entry_price_usd", 0.0) or 0.0
        blocks.append({
            "call_id": c["id"],
            "symbol": c["symbol"],
            "mint": mint,
            "network": c["network"],
            "entry_usd": entry,
            "take_profit_pct": c["take_profit_pct"],
            "stop_loss_pct": c["stop_loss_pct"],
            "horizon_minutes": c["horizon_minutes"],
            "confidence": c.get("confidence", 0.0),
            "signals": c.get("signals", []),
            "rationale": c.get("rationale", ""),
            "axiom_ready": bool(mint),
            "links": {
                "pool": f"https://www.geckoterminal.com/{c['network']}/pools/{c['pool_address']}",
                # Die Mint ist der Axiom-Identifier; die exakte Web-/Bot-Syntax
                # ist Wallet-/Version-spezifisch und wird hier NICHT erfunden.
                "mint": mint,
            },
            "note": "PAPER — nicht ausführen, bis Win-Rate belastbar ist. "
                    "In Axiom über die Mint-Adresse suchen.",
        })
    return {"ok": True, "wallet": "axiom", "count": len(blocks), "orders": blocks}
