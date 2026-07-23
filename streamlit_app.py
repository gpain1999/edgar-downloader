#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EDGAR Downloader — version Streamlit.

Télécharge les rapports annuels (10-K / 20-F) depuis SEC EDGAR,
les convertit en Markdown et les propose en ZIP.

  - Interface : liste de CIK/tickers, formes, période.
  - Cœur ASYNC : téléchargements en parallèle (aiohttp) sous la limite SEC (<10 req/s).
  - Progression EN DIRECT (barre + journal).
  - ZIP téléchargeable (les .md + listing_filings.csv).

Local :
    pip install -r requirements.txt
    streamlit run streamlit_app.py

Déploiement gratuit : share.streamlit.io (dépôt GitHub public).
"""

import asyncio
import csv
import io
import re
import zipfile
from datetime import datetime

import aiohttp
import streamlit as st
from markdownify import markdownify as _to_md

st.set_page_config(page_title="SEC EDGAR → Markdown", page_icon="📄", layout="centered")


# ---------------------------------------------------------------------------
#  Limiteur de débit async (respect de la limite SEC : < 10 requêtes/seconde)
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, min_interval=0.12):
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self):
        async with self._lock:
            loop = asyncio.get_event_loop()
            delta = loop.time() - self._last
            if delta < self.min_interval:
                await asyncio.sleep(self.min_interval - delta)
            self._last = loop.time()


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------
def year_of(filing):
    d = filing["report_date"] or filing["filing_date"]
    return int(d[:4]) if d else None


def keep(filing, forms, ymin, ymax):
    if filing["form"] not in forms:
        return False
    y = year_of(filing)
    return y is not None and ymin <= y <= ymax


def doc_url(cik, filing):
    acc = filing["accession"].replace("-", "")
    return (f"https://www.sec.gov/Archives/edgar/data/"
            f"{int(cik)}/{acc}/{filing['primary_doc']}")


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_")[:40]


def to_markdown(html_text):
    try:
        return _to_md(html_text, heading_style="ATX", strip=["script", "style"])
    except Exception:
        return None


def resolve_cik(identifier, ticker_map):
    ident = identifier.strip().upper()
    if not ident:
        return None
    if ident.isdigit() or (ident.startswith("0") and ident[1:].isdigit()):
        return ident.zfill(10)
    return ticker_map.get(ident)


# ---------------------------------------------------------------------------
#  Réseau async
# ---------------------------------------------------------------------------
async def fetch(session, url, limiter, as_json=False):
    await limiter.wait()
    for attempt in range(3):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status == 200:
                    return await (r.json() if as_json else r.text())
                if r.status == 429:
                    await asyncio.sleep(2 + attempt * 2)
                    continue
                return None
        except Exception:
            await asyncio.sleep(1.5)
    return None


async def load_ticker_map(session, limiter):
    data = await fetch(session, "https://www.sec.gov/files/company_tickers.json",
                       limiter, as_json=True)
    if not data:
        return {}
    return {row["ticker"].upper(): str(row["cik_str"]).zfill(10) for row in data.values()}


async def get_all_filings(session, cik, limiter):
    data = await fetch(session, f"https://data.sec.gov/submissions/CIK{cik}.json",
                       limiter, as_json=True)
    if not data:
        return None, []
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

    add_block(data.get("filings", {}).get("recent", {}))

    extras = data.get("filings", {}).get("files", [])
    tasks = [fetch(session, f"https://data.sec.gov/submissions/{e['name']}",
                   limiter, as_json=True) for e in extras]
    for block in await asyncio.gather(*tasks):
        if block:
            add_block(block)

    return name, filings


# ---------------------------------------------------------------------------
#  Pipeline principal (async) — met à jour l'UI en direct
# ---------------------------------------------------------------------------
async def run_pipeline(idents, forms, ymin, ymax, user_agent, ui):
    """ui = dict de placeholders Streamlit : bar, status, log."""
    limiter = RateLimiter(0.12)
    logs = []
    files = {}      # chemin relatif -> contenu texte
    listing = []

    def log(msg):
        logs.append(msg)
        ui["log"].code("\n".join(logs[-200:]), language=None)

    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}

    async with aiohttp.ClientSession(headers=headers) as session:
        # 1) Résolution
        need_map = any(not resolve_cik(c, {}) for c in idents)
        if need_map:
            ui["status"].info("Chargement de la table ticker → CIK…")
            ticker_map = await load_ticker_map(session, limiter)
        else:
            ticker_map = {}

        ui["status"].info("Listing des dépôts…")
        companies = []
        for ident in idents:
            cik = resolve_cik(ident, ticker_map)
            if not cik:
                log(f"[X] '{ident}' introuvable (ticker inconnu ? donne le CIK).")
                continue
            name, filings = await get_all_filings(session, cik, limiter)
            if not filings:
                log(f"[X] Aucune donnée pour {ident} (CIK {cik}).")
                continue
            sel = [f for f in filings if keep(f, forms, ymin, ymax)]
            log(f"{name} (CIK {cik}) : {len(sel)} rapport(s).")
            companies.append((cik, name, sel))

        total = sum(len(s) for _, _, s in companies)
        if total == 0:
            ui["status"].warning("Aucun rapport trouvé. Vérifie les CIK, les formes et la période.")
            return None, None, 0, 0

        # 2) Téléchargement + conversion en parallèle
        done = {"n": 0}
        sem = asyncio.Semaphore(5)

        async def handle(cik, name, f):
            async with sem:
                url = doc_url(cik, f)
                y = year_of(f)
                base = f"{y}_{f['form'].replace('/', '-')}"
                rel = f"{safe_name(name) or cik}/{base}.md"

                html = await fetch(session, url, limiter)
                md_path = ""
                if html is None:
                    log(f"[X] {name} {base} : échec téléchargement")
                else:
                    md = await asyncio.to_thread(to_markdown, html)
                    if md:
                        files[rel] = md
                        md_path = rel
                        log(f"[OK] {name} {base}")
                    else:
                        log(f"[!] {name} {base} : conversion échouée")

                listing.append({
                    "societe": name, "cik": cik, "forme": f["form"], "annee": y,
                    "date_depot": f["filing_date"], "date_rapport": f["report_date"],
                    "accession": f["accession"], "fichier_md": md_path, "url_sec": url,
                })
                done["n"] += 1
                ui["bar"].progress(done["n"] / total)
                ui["status"].info(f"{done['n']} / {total} rapports traités…")

        await asyncio.gather(*[handle(cik, name, f)
                               for cik, name, sel in companies for f in sel])

    # 3) Listing CSV
    listing.sort(key=lambda r: (r["societe"], r["annee"] or 0))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(listing[0].keys()))
    w.writeheader()
    w.writerows(listing)
    csv_text = buf.getvalue()

    # 4) ZIP en mémoire
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, content in files.items():
            z.writestr(rel, content)
        z.writestr("listing_filings.csv", csv_text)
    zip_buf.seek(0)

    return zip_buf.getvalue(), csv_text, len(files), total


# ---------------------------------------------------------------------------
#  Interface
# ---------------------------------------------------------------------------
st.title("📄 SEC EDGAR → Markdown")
st.caption("Télécharge les rapports annuels (10-K / 20-F), les convertit en Markdown "
           "et les empaquette en ZIP.")

with st.sidebar:
    st.header("Paramètres")
    user_agent = st.text_input(
        "User-Agent (nom + email)",
        placeholder="Prenom Nom email@exemple.com",
        help="Exigé par la SEC sur chaque requête. Sans email valide, l'API renvoie une erreur 403.",
    )
    forms = st.multiselect("Formes", ["10-K", "20-F"], default=["10-K", "20-F"])
    c1, c2 = st.columns(2)
    ymin = c1.number_input("Année min", 1995, 2100, 2010)
    ymax = c2.number_input("Année max", 1995, 2100, datetime.now().year)
    st.divider()
    st.caption("Source : data.sec.gov — API publique et gratuite.")

ciks_raw = st.text_area(
    "Liste de CIK ou tickers",
    value="0001101026\n0001849853\n0000834365",
    height=140,
    help="Un par ligne, ou séparés par virgule/espace. Les tickers (AAPL) sont acceptés.",
)

launch = st.button("🚀 Lancer le téléchargement", type="primary", use_container_width=True)

if launch:
    idents = [c for c in re.split(r"[\s,;]+", ciks_raw) if c.strip()]
    if not idents:
        st.error("Renseigne au moins un CIK ou ticker.")
    elif "@" not in user_agent:
        st.error("Renseigne un User-Agent avec un email valide (exigé par la SEC).")
    elif not forms:
        st.error("Sélectionne au moins une forme (10-K ou 20-F).")
    else:
        ui = {
            "bar": st.progress(0.0),
            "status": st.empty(),
            "log": st.empty(),
        }
        try:
            zip_bytes, csv_text, n_ok, n_total = asyncio.run(
                run_pipeline(idents, set(forms), int(ymin), int(ymax), user_agent, ui)
            )
            if zip_bytes:
                st.session_state["zip"] = zip_bytes
                st.session_state["csv"] = csv_text
                ui["status"].success(f"Terminé : {n_ok} / {n_total} rapport(s) convertis.")
        except Exception as e:
            st.error(f"Erreur : {e}")

if "zip" in st.session_state:
    st.divider()
    d1, d2 = st.columns(2)
    d1.download_button("⬇️ Télécharger le ZIP", st.session_state["zip"],
                       file_name="edgar_filings.zip", mime="application/zip",
                       use_container_width=True)
    d2.download_button("⬇️ Listing CSV seul", st.session_state["csv"],
                       file_name="listing_filings.csv", mime="text/csv",
                       use_container_width=True)
