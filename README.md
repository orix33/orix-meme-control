# Meme-Control

Ein Kontrollversuch für Solana-Meme-Coins. **Paper-only** — es gibt in diesem
Repo keinen Order-Pfad, keine Credentials und keine Signatur.

## Die Frage

Nicht „verdient man mit Meme-Coins Geld", sondern die schärfere:

> **Trägt eine Auswahlregel Information — oder wäre Würfeln im selben
> Kandidatenfeld genauso gut?**

Die meisten Bots melden eine Trefferquote und nennen das Ergebnis. Eine
Trefferquote ohne Vergleichsgruppe misst aber nichts: 40 % sind großartig, wenn
Würfeln 10 % holt, und katastrophal, wenn Würfeln 60 % holt.

## Der Aufbau

Bei jedem Durchgang werden **drei Arme** gleichzeitig eröffnet, aus demselben
Kandidatentopf, zur selben Sekunde:

| Arm | Auswahlregel |
|---|---|
| `alt` | Chart-Scorer: Preis, Volumen, Pool-Alter, Buy-Ratio |
| `neu` | nach Studienlage: Social-Präsenz, Creator-Self-Buy, Halterverteilung |
| `kontrolle` | Zufallszug |

**Alles andere ist identisch:** gleicher Zeitpunkt, gleicher Markt, gleiches
Take-Profit (+25 %), gleicher Stop-Loss (−15 %), gleicher Horizont (6 h),
gleiche Messmethode. Was an Differenz übrig bleibt, kann nur von der Auswahl
kommen.

Die Arme ziehen **disjunkt** — kein Pool liegt in zweien, sonst wären die
Stichproben nicht unabhängig.

### Die Zugriffsreihenfolge rotiert

```
Durchgang 0 -> alt > neu > kontrolle
Durchgang 1 -> neu > kontrolle > alt
Durchgang 2 -> kontrolle > alt > neu
```

Ohne das bekäme der Zufallsarm systematisch die Reste, nachdem die
Auswahlregeln sich bedient haben — und der Vorteil, den der Versuch messen
soll, wäre eingebaut. Über 30 Durchgänge greift jeder Arm exakt 10-mal zuerst
zu.

## Kein Urteil unter n = 30

`report()` gibt bewusst **kein** Ergebnis aus, solange der kleinste Arm unter 30
gewerteten Calls liegt, und sagt stattdessen, wie viele fehlen. Ein Urteil bei
n = 3 wäre geraten.

## Vier Messfehler, die das Ergebnis umgedreht hätten

Alle vier stehen als Kommentar an der Stelle im Code, an der sie passiert sind.
Sie sind der eigentliche Inhalt dieses Repos:

**1. Ein Messfehler ist kein Messwert.** Die erste Fassung gab bei *jedem*
Problem `None` zurück — HTTP 404, Rate Limit, Timeout, alles dasselbe. Der
Aufrufer buchte das als Rug mit −15 % und schloss endgültig. Nachprüfung von 11
so geschlossenen Calls: **0× wirklich weg, 5× nachweislich am Leben, 6× HTTP
429.** Die Bilanz war um 102 Prozentpunkte falsch. Heute unterscheidet
`fetch_pool_state()` drei Zustände, und `unknown` schließt nichts.

**2. Ein Kurs ohne Käufer ist keine Bewertung.** Ein Rug nimmt die Liquidität
mit und lässt den letzten Preis stehen. Gemessen: Pool von 25.316 $ auf
0,00000074 $, Preis dabei **17 % höher** als beim Einstieg. Die Position stand
auf +17,31 % Gewinn. Von 21 nachgemessenen Calls waren 5 echte Rugs — und
**3 davon hätten als Gewinn in der Statistik gestanden.**

**3. Ein Filter, der mit etwas anderem korreliert, misst womöglich das andere.**
Die Schwelle „Top-10-Halter über 90 %" trennte nicht Gefahr von Sicherheit,
sondern **jung von alt**: Spearman-ρ(Alter, Konzentration) = **−0,76**. Frische
Pools liegen im Median bei 100 %, etablierte bei 29 % — solange ein Token auf
der Bonding-Kurve steht, hält die Kurve das Angebot. Der Filter ist jetzt
altersabhängig.

**4. Ein Zufallsarm, der nur die Reste bekommt, ist kein Zufallsarm.** Siehe
Rotation oben.

Fehler 2 und 4 hätten beide **in dieselbe Richtung** verzerrt: zugunsten der
Auswahlregeln.

## Datenquellen — alle frei, ohne Schlüssel

| Quelle | wofür |
|---|---|
| GeckoTerminal | Pools, Preise, Liquidität |
| RugCheck | Halterverteilung, Creator, Insider-Netzwerke, Authorities |
| IPFS-Metadaten | Social-Präsenz (Telegram, Twitter, Website) |

**Falle bei den Socials:** Die Felder existieren oft und sind **leer**. Wer auf
`"telegram" in meta` prüft statt auf den Inhalt, misst Unsinn.

## Betrieb

Der Workflow läuft alle 20 Minuten, misst offene Positionen, eröffnet neue und
**schreibt den Zustand nach `py/data/` ins Repo zurück**. Dieses Verzeichnis ist
das Gedächtnis des Versuchs, kein Nebenprodukt.

Von Hand:

```bash
cd py && python cycle.py
```

Reine Standardbibliothek — kein `pip install`, kein `requirements.txt`.

## Grenzen, die bleiben

* **Keine Aussage über echte Ausführung.** Paper-Preise kennen weder Slippage
  noch Prioritätsgebühren noch die Frage, ob zum Einstiegskurs überhaupt jemand
  verkauft hätte. Ein Vorteil hier ist eine **notwendige, keine hinreichende**
  Bedingung.
* **Der Topf ist nicht neutral.** Alle drei Arme ziehen aus dem Kandidatenfeld,
  das die Filter des Scanners übrig lassen. Gemessen: 37 von 40 Pools scheitern
  allein an der 1h-Volumenschwelle. Der Versuch beantwortet also „welche Auswahl
  *innerhalb dieses Feldes* ist besser", nicht „welche Auswahl im Meme-Markt ist
  besser".
* **Der `neu`-Arm läuft eingeschränkt.** Sein stärkstes Merkmal ist die
  Telegram-Präsenz (in der Literatur 8,94×-Lift auf Graduierung). In der
  bisherigen Stichprobe hatte **kein einziger** Kandidat einen Telegram-Kanal.

## Zum Hintergrund

Die Basisraten, gegen die hier gemessen wird: Etwa **0,2 %** der auf pump.fun
gestarteten Token graduieren (Kaplan-Meier, 24h-Fenster, 2026), und rund
**6 %** der Trader liegen nach 90 Tagen im Plus. Das ist die Latte — nicht ein
Argument dagegen, aber die Zahl, die jede Auswahlregel schlagen muss.

## Lizenz

MIT.
