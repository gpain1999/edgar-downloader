#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Téléchargement automatique de rapports annuels (10-K et 20-F) depuis SEC EDGAR,
avec conversion en Markdown (.md) pour l'extraction de données.

  - Prend une liste de sociétés (par TICKER ou par CIK).
  - Récupère tous les 10-K et 20-F sur la période choisie (y compris l'historique
    ancien, pas seulement les ~1000 derniers dépôts).
  - Enregistre chaque rapport en HTML  : filings/<SOCIETE>/<ANNEE>_<FORME>.html
  - Convertit chaque rapport en Markdown : filings/<SOCIETE>/<ANNEE>_<FORME>.md
    (via markitdown : les tableaux HTML deviennent des tableaux Markdown propres).
  - Produit un LISTING CSV récapitulatif (listing_filings.csv).

Dépendances : pip install requests markitdown
API 100% gratuite. Seule contrainte : un User-Agent avec ton email + max 10 req/s.
Doc officielle : https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
"""

import csv
import os
import re
import time
import requests

try:
    from markitdown import MarkItDown
    _md_converter = MarkItDown()
except ImportError:
    _md_converter = None

# ============================================================================
#  CONFIGURATION  — à adapter
# ============================================================================

# OBLIGATOIRE : mets ton vrai nom + email, sinon la SEC renvoie une erreur 403.
USER_AGENT = "Guillaume PAIN guillaume.pain9@gmail.com"

# Tes sociétés. Tu peux mélanger tickers (ex "AAPL") et CIK (ex "0000320193").
# Pour un 20-F étranger sans ticker, cherche le CIK sur https://www.sec.gov/cgi-bin/browse-edgar
COMPANIES = [
    "0001101026",  # Zivo Bioscience, Inc. (ZIVO)
    "0001849853",  # Stevanato Group S.p.A. (STVN)
    "0000834365",  # BIOLIFE SOLUTIONS INC (BLFS)
]

# Formes à télécharger. Laisse les deux pour couvrir US (10-K) + étrangers (20-F).
FORM_TYPES = {"10-K", "20-F"}

# Période (année du rapport, incluse). Mets None pour ne pas filtrer.
# EDAP dépose des 20-F depuis ~2012 ; large fenêtre pour tout récupérer.
YEAR_MIN = 2010
YEAR_MAX = 2025

# Convertir chaque rapport en Markdown (.md) via markitdown ? (True conseillé)
CONVERT_TO_MD = True
# Garder aussi le HTML source à côté du .md ? (False = on ne garde QUE les .md)
KEEP_HTML = False

# Dossier de sortie
OUTPUT_DIR = "filings"
LISTING_CSV = "listing_filings.csv"

# Délai entre requêtes (secondes). 0.12 = sûr, reste sous la limite des 10 req/s.
DELAY = 0.15

# ============================================================================
#  Fin de la configuration — normalement rien à toucher en dessous
# ============================================================================

HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
session = requests.Session()
session.headers.update(HEADERS)


def get(url):
    """GET avec respect du rate limit et petite tolérance aux erreurs réseau."""
    time.sleep(DELAY)
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 429:          # rate limited -> on ralentit
                time.sleep(2 + attempt * 2)
                continue
            print(f"    [!] HTTP {r.status_code} sur {url}")
            return None
        except requests.RequestException as e:
            print(f"    [!] Erreur réseau ({e}), tentative {attempt + 1}/3")
            time.sleep(2)
    return None


def load_ticker_map():
    """Table ticker -> CIK fournie gratuitement par la SEC."""
    url = "https://www.sec.gov/files/company_tickers.json"
    r = get(url)
    if not r:
        return {}
    mapping = {}
    for row in r.json().values():
        mapping[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
    return mapping


def resolve_cik(identifier, ticker_map):
    """Accepte un ticker ('AAPL') ou un CIK ('320193' / '0000320193')."""
    ident = identifier.strip().upper()
    if ident.isdigit() or (ident.startswith("0") and ident[1:].isdigit()):
        return ident.zfill(10)
    return ticker_map.get(ident)


def get_all_filings(cik):
    """
    Renvoie la liste complète des dépôts d'une société.
    On combine le bloc 'recent' ET les fichiers d'archives paginés,
    sinon on rate les rapports anciens (le 'recent' est plafonné à ~1000 dépôts).
    """
    base = f"https://data.sec.gov/submissions/CIK{cik}.json"
    r = get(base)
    if not r:
        return None, []
    data = r.json()
    name = data.get("name", cik)

    filings = []

    def add_block(block):
        forms = block.get("form", [])
        for i in range(len(forms)):
            filings.append({
                "form": forms[i],
                "accession": block["accessionNumber"][i],
                "filing_date": block["filingDate"][i],
                "report_date": block["reportDate"][i],
                "primary_doc": block["primaryDocument"][i],
            })

    recent = data.get("filings", {}).get("recent", {})
    add_block(recent)

    # Archives historiques (fichiers supplémentaires)
    for extra in data.get("filings", {}).get("files", []):
        r2 = get(f"https://data.sec.gov/submissions/{extra['name']}")
        if r2:
            add_block(r2.json())

    return name, filings


def year_of(filing):
    """Année de référence du rapport (report_date sinon filing_date)."""
    d = filing["report_date"] or filing["filing_date"]
    return int(d[:4]) if d else None


def keep(filing):
    if filing["form"] not in FORM_TYPES:
        return False
    y = year_of(filing)
    if y is None:
        return False
    if YEAR_MIN and y < YEAR_MIN:
        return False
    if YEAR_MAX and y > YEAR_MAX:
        return False
    return True


def doc_url(cik, filing):
    acc_nodash = filing["accession"].replace("-", "")
    cik_int = int(cik)  # sans zéros de tête pour le chemin Archives
    return (f"https://www.sec.gov/Archives/edgar/data/"
            f"{cik_int}/{acc_nodash}/{filing['primary_doc']}")


def html_to_markdown(html_path, md_path):
    """Convertit un HTML en Markdown via markitdown. Renvoie le chemin .md ou ''."""
    if _md_converter is None:
        print("    [!] markitdown non installé -> conversion .md ignorée "
              "(pip install markitdown)")
        return ""
    try:
        result = _md_converter.convert(html_path)
        with open(md_path, "w", encoding="utf-8") as out:
            out.write(result.text_content)
        return md_path
    except Exception as e:
        print(f"    [!] Conversion Markdown échouée : {e}")
        return ""


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_")[:40]


def main():
    if "prenom.nom@" in USER_AGENT:
        print("ATTENTION : remplace USER_AGENT par ton vrai nom + email avant de lancer.\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print("Chargement de la table ticker -> CIK...")
    ticker_map = load_ticker_map()

    listing_rows = []

    for ident in COMPANIES:
        cik = resolve_cik(ident, ticker_map)
        if not cik:
            print(f"[X] Impossible de résoudre '{ident}' (ticker inconnu ? donne le CIK).")
            continue

        name, filings = get_all_filings(cik)
        if filings is None:
            print(f"[X] Pas de données pour {ident} (CIK {cik}).")
            continue

        selected = [f for f in filings if keep(f)]
        print(f"\n=== {name} (CIK {cik}) : {len(selected)} rapport(s) retenu(s) ===")

        comp_dir = os.path.join(OUTPUT_DIR, safe_name(name) or cik)
        os.makedirs(comp_dir, exist_ok=True)

        for f in selected:
            url = doc_url(cik, f)
            y = year_of(f)
            base = f"{y}_{f['form'].replace('/', '-')}"
            html_path = os.path.join(comp_dir, base + ".html")
            md_path = os.path.join(comp_dir, base + ".md")

            r = get(url)
            html_out = ""
            md_out = ""
            if not r:
                print(f"  [X] {base} : téléchargement échoué")
            else:
                # 1) on écrit le HTML (nécessaire pour markitdown, gardé si KEEP_HTML)
                with open(html_path, "w", encoding="utf-8") as out:
                    out.write(r.text)
                html_out = html_path

                # 2) conversion Markdown
                if CONVERT_TO_MD:
                    md_out = html_to_markdown(html_path, md_path)

                # 3) on peut supprimer le HTML si on ne garde que le .md
                if md_out and not KEEP_HTML:
                    os.remove(html_path)
                    html_out = ""

                tag = "html+md" if md_out else "html"
                print(f"  [OK] {base}  ({tag})")

            listing_rows.append({
                "societe": name,
                "cik": cik,
                "forme": f["form"],
                "annee": y,
                "date_depot": f["filing_date"],
                "date_rapport": f["report_date"],
                "accession": f["accession"],
                "fichier_html": html_out,
                "fichier_md": md_out,
                "url_sec": url,
            })

    # Écriture du LISTING
    if listing_rows:
        with open(LISTING_CSV, "w", newline="", encoding="utf-8-sig") as csvf:
            writer = csv.DictWriter(csvf, fieldnames=list(listing_rows[0].keys()))
            writer.writeheader()
            writer.writerows(listing_rows)
        print(f"\nListing écrit : {LISTING_CSV}  ({len(listing_rows)} lignes)")
    else:
        print("\nAucun rapport téléchargé — vérifie ta liste COMPANIES et la période.")


if __name__ == "__main__":
    main()
