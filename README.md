# 24/7 ohne laufenden PC

Stand 31.08.2026. **Fertig zum Deployen — der letzte Schritt liegt bei dir.**

---

## Zuerst das, was du entscheiden musst

Die Aufgabe „das System läuft 24/7" zerfällt in **zwei Teile mit völlig
verschiedenem Risiko.** Sie zusammen zu deployen wäre ein Fehler.

| | Credentials | Risiko | Status |
|---|---|---|---|
| **Paper-Kontrollversuch** (`meme_control`) | **keine** | keins | ✅ fertig, in diesem Ordner |
| **Handelssystem** (Daemon, Server) | KuCoin **live** | echtes Geld | ⛔ nicht angefasst |

### Warum ich das Handelssystem nicht mit deploye

**1. Es steht auf `live`.** `/api/trading/status` meldet `mode: live`, zwei
Börsen mit `live: True`. Das ist kein Testbetrieb.

**2. `order_guard` kann nicht über Rechnergrenzen koordinieren.** Er ist eine
lokale Datei (`order_guard.json`) mit lokalem Dateilock. Läuft eine Instanz auf
deinem PC und eine in der Cloud, glauben **beide**, sie besitzen dieselbe
Position. Das ist exakt der Zwei-Verkäufer-Unfall, aus dem die 14.012
gescheiterten Verkaufsversuche entstanden — nur diesmal ohne Rettungsleine, weil
der Guard es prinzipiell nicht sehen kann.

**3. Dein `.env` enthält alles.** KuCoin-Handelsschlüssel, OpenAI, Cloudflare,
R2, Gumroad, PayPal — und ein Pinterest-Passwort im Klartext. Ein Deploy heißt:
Diese Schlüssel liegen auf einer Maschine, die dir nicht gehört. Das ist deine
Entscheidung, nicht meine, und ich trage keine Credentials irgendwo ein.

**Wenn du das Handelssystem wirklich 24/7 willst**, ist die Reihenfolge:
erst eine host-übergreifende Sperre bauen (der Guard braucht einen gemeinsamen
Speicher statt einer lokalen Datei), dann den lokalen Daemon **abschalten**,
dann deployen. Nicht andersherum.

---

## Der Paper-Versuch: fertig, kostenlos, risikofrei

Er braucht **null Schlüssel** — GeckoTerminal, RugCheck und IPFS sind alle frei
und ohne Key. Und **null externe Pakete**: reine Python-Standardbibliothek, kein
`pip install`.

Es gibt in diesen Dateien keinen Pfad zu einer Order. Keine Credentials, keine
Signatur, kein Handelsendpunkt.

### Weg A — GitHub Actions (empfohlen)

Kostenlos, kein Server, kein Passwort. Du hast schon einen GitHub-Account.

1. Neues Repo anlegen (privat reicht).
2. Den Inhalt dieses Ordners hineinkopieren — inklusive `.github/`.
3. Pushen. Fertig.

Der Workflow läuft **stündlich**, misst die offenen Positionen, eröffnet neue
und **schreibt den Zustand ins Repo zurück**. Jeder Lauf zeigt den aktuellen
Bericht in der Zusammenfassung.

```bash
git init && git add -A && git commit -m "Kontrollversuch 24/7"
```

**Zum Takt:** Stündlich passt in die 2.000 Freiminuten für private Repos
(~1.450/Monat). Alle 30 Minuten passt **nicht** (~2.900). In einem öffentlichen
Repo ist es unbegrenzt — dann kannst du in `meme-control.yml` auf
`*/20 * * * *` gehen.

Für den Versuch reicht stündlich: Die Positionen haben 6 Stunden Horizont, und
24 Durchgänge am Tag liefern **mehr** Stichprobe als der lokale 15-Minuten-Takt,
der nur lief, solange dein Rechner an war.

### Weg B — eigener kleiner Server

Nur nötig, wenn du später mehr als den Versuch laufen lassen willst.

```bash
docker build -t meme-control .
docker run -d --name meme-control --restart=always \
  -v "$PWD/py/data:/app/data" meme-control
```

Kosten realistisch: 4–6 €/Monat (Hetzner CX22, Netcup). Oracle Cloud hat ein
dauerhaft freies ARM-Kontingent, verlangt aber Kreditkarte zur Anmeldung.

**Gegen die Zahlen gehalten:** Das Projekt hat bisher 0,01 $ nachgewiesene
Einnahmen. 5 €/Monat sind 60 €/Jahr. Weg A kostet nichts und leistet für diesen
Versuch dasselbe.

---

## ⚠️ Nur EINE Instanz gleichzeitig

Sobald der Versuch in der Cloud läuft, **beende den lokalen Runner** — sonst
schreiben zwei Instanzen in zwei getrennte Zustände, und beide Stichproben sind
halbiert und unvergleichbar.

```
py_ecosystem/_meme_control_disabled     ← diese Datei anlegen
```

Solange sie existiert, startet der Watchdog den lokalen Runner nicht neu. Dann
den laufenden Prozess beenden (PID steht in `_meme_control_pid.txt`).

---

## Was im Ordner liegt

```
.github/workflows/meme-control.yml   Der stündliche Lauf
py/cycle.py                          Ein Durchgang (messen, dann eröffnen)
py/meme_control.py                   Die drei Arme und die Statistik
py/meme_signals.py                   Anreicherung über RugCheck + IPFS
py/meme_agent.py                     Scanner und Pool-Zustand
py/data/                             Zustand — wächst mit jedem Lauf
Dockerfile                           Für Weg B
```

Der Zustand ist mitkopiert: Der Versuch läuft dort weiter, wo er lokal steht,
statt bei null anzufangen.

## Wann es etwas zu sehen gibt

`report()` verweigert das Urteil unter **n = 30 je Arm**. Bei stündlich und
1–2 Picks je Arm und Durchgang ist das in **ein bis zwei Tagen** erreicht.

Bis dahin steht in jedem Lauf „KEIN URTEIL" — das ist kein Fehler, sondern die
einzige ehrliche Antwort bei zu kleiner Stichprobe.
