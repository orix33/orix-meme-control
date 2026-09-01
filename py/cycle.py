# -*- coding: utf-8 -*-
"""cycle.py — ein einzelner Kontrollzyklus, gedacht fuer geplante Laeufe.

Unterschied zu `_meme_control_runner.py`: Der Runner ist eine Endlosschleife
fuer einen dauerhaft laufenden Rechner. Diese Datei macht **genau einen**
Durchgang und beendet sich — das ist die Form, die ein Cron-Dienst oder
GitHub Actions braucht.

Reihenfolge ist wichtig: erst messen, dann eroeffnen. Andersherum wuerden
frisch eroeffnete Positionen sofort mitgeprueft und wuerden das Preisbudget
der aelteren auffressen.

Exit-Code 0 auch bei einem Rate Limit — ein ausgefallener Abruf ist kein
Fehlschlag des Laufs, und ein roter Haken in der Lauf-Historie soll etwas
bedeuten.
"""
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

import meme_control as MC   # noqa: E402


def main() -> int:
    try:
        up = MC.update()
        print("UPDATE :", json.dumps(up, ensure_ascii=False))
    except Exception:
        print("UPDATE fehlgeschlagen:")
        traceback.print_exc(limit=4)
        up = {}

    try:
        sc = MC.scan()
        print("SCAN   :", json.dumps(sc, ensure_ascii=False))
    except Exception:
        print("SCAN fehlgeschlagen:")
        traceback.print_exc(limit=4)
        sc = {}

    print()
    print(MC.report())

    # Ein Rate Limit ist kein Fehlschlag. Nur wenn BEIDE Schritte geworfen
    # haben, stimmt etwas Grundsaetzliches nicht.
    return 0 if (up or sc) else 1


if __name__ == "__main__":
    sys.exit(main())
