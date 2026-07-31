#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nettoie les .md issus de la conversion des 20-F / 10-K :
supprime le bloc de données XBRL "inline" collé en tête de fichier.

Repère la vraie ouverture du document (page de garde SEC) et coupe tout
ce qui précède. Fonctionne pour 10-K comme 20-F.

Usage :
    python clean_md.py fichier.md              # nettoie en place (avec .bak)
    python clean_md.py dossier/                # nettoie tous les .md du dossier
    python clean_md.py fichier.md --no-backup  # sans sauvegarde .bak
"""

import re
import sys
from pathlib import Path

# Marqueurs de début de la page de garde d'un dépôt SEC (ordre de préférence)
MARKERS = [
    re.compile(r"SECURITIES\s+AND\s+EXCHANGE\s+COMMISSION", re.I),
    re.compile(r"UNITED\s+STATES", re.I),
    re.compile(r"FORM\s+(?:20-F|10-K)", re.I),
]


def find_cut(lines):
    """Renvoie l'index de la 1re ligne du vrai document, ou 0 si rien trouvé."""
    for marker in MARKERS:
        for i, line in enumerate(lines[:3000]):   # le bruit est toujours en tête
            if marker.search(line):
                return i
    return 0


def clean_text(text):
    lines = text.splitlines()
    cut = find_cut(lines)
    cleaned = "\n".join(lines[cut:]).lstrip("\n")
    return cleaned, cut


def process(path: Path, backup=True):
    original = path.read_text(encoding="utf-8", errors="ignore")
    cleaned, cut = clean_text(original)
    removed = len(original) - len(cleaned)
    if cut == 0:
        print(f"  = {path.name} : aucun bloc détecté (déjà propre)")
        return
    if backup:
        path.with_suffix(path.suffix + ".bak").write_text(original, encoding="utf-8")
    path.write_text(cleaned, encoding="utf-8")
    print(f"  ✓ {path.name} : {cut} lignes / {removed:,} octets supprimés en tête")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    backup = "--no-backup" not in sys.argv
    if not args:
        print("Usage : python clean_md.py <fichier.md | dossier/> [--no-backup]")
        sys.exit(1)

    target = Path(args[0])
    if target.is_dir():
        files = sorted(target.glob("*.md"))
    else:
        files = [target]

    print(f"Nettoyage de {len(files)} fichier(s)…")
    for f in files:
        process(f, backup=backup)
    print("Terminé.")


if __name__ == "__main__":
    main()
