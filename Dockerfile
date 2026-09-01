# Reine Standardbibliothek — kein pip install, kein requirements.txt.
# Der Kontrollversuch nutzt nur urllib, json, math, random, pathlib.
FROM python:3.12-slim

WORKDIR /app
COPY py/ /app/

# Der Zustand gehoert in ein Volume, sonst ist er beim naechsten
# `docker run` weg. Siehe README, Weg B.
VOLUME ["/app/data"]

ENV PYTHONUNBUFFERED=1

# Dauerschleife statt Cron: ein Zyklus, dann warten. 900 s = 15 Minuten,
# haeufiger als der GitHub-Weg, weil hier keine Minuten gezaehlt werden.
CMD ["sh", "-c", "while true; do python cycle.py; sleep 900; done"]
