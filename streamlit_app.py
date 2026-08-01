#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EDGAR Downloader — version Streamlit (avec explorateur par code SIC).

Télécharge les rapports annuels (10-K / 20-F) depuis SEC EDGAR,
les convertit en Markdown et les propose en ZIP.

DEUX MODES :
  1. Saisie manuelle : on colle une liste de CIK / tickers.
  2. Explorer par code SIC : on liste TOUTES les sociétés d'un secteur
     (ex. 3841 = instruments médicaux), puis on sélectionne un échantillon.

GARDE-FOUS (important sur le tier gratuit, 1 Go de RAM) :
  - L'exploration SIC ne télécharge AUCUN rapport (juste la liste des sociétés).
  - Estimation du volume avant lancement.
  - Plafond strict du nombre de rapports : au-delà, l'app refuse et propose
    de réduire la période ou l'échantillon.
  - Les .md sont écrits sur disque (pas en RAM), le ZIP est construit depuis
    le disque.

Local :
    pip install -r requirements.txt
    streamlit run streamlit_app.py

Déploiement gratuit : share.streamlit.io (dépôt GitHub public).
"""

import asyncio
import csv
import io
import os
import re
import shutil
import tempfile
import textwrap
import zipfile
from datetime import datetime

import aiohttp
import streamlit as st
from bs4 import BeautifulSoup
from fpdf import FPDF
from markdownify import markdownify as _to_md

st.set_page_config(page_title="SEC EDGAR → Markdown", page_icon="📄", layout="wide")

# ---------------------------------------------------------------------------
#  Garde-fous
# ---------------------------------------------------------------------------
HARD_CAP = 600          # plafond absolu de rapports par lancement
DEFAULT_CAP = 150       # plafond conseillé par défaut

# Codes SIC utiles (HealthTech et connexes)
SIC_CODES = {
    "3841 — Instruments chirurgicaux et médicaux": "3841",
    "3845 — Appareils électromédicaux": "3845",
    "3826 — Instruments d'analyse de laboratoire": "3826",
    "3842 — Fournitures orthopédiques et prothèses": "3842",
    "8011 — Cabinets et cliniques médicales (télémédecine)": "8011",
    "8731 — Recherche commerciale physique et biologique": "8731",
    "7372 — Éditeurs de logiciels (health IT)": "7372",
    "2836 — Produits biologiques": "2836",
    "2834 — Préparations pharmaceutiques": "2834",
    "8090 — Services de santé divers": "8090",
    "8062 — Hôpitaux généraux": "8062",
    "— Autre (saisir le code) —": "",
}


# ---------------------------------------------------------------------------
#  Limiteur de débit async (limite SEC : < 10 requêtes/seconde)
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


# ---------------------------------------------------------------------------
#  Conversion multi-formats : Markdown (.md) / Texte (.txt) / PDF (.pdf)
# ---------------------------------------------------------------------------
_FONT_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "DejaVuSans.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
]


def _font_path():
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _clean_soup(html_text):
    """Retire le XBRL 'inline' caché et les éléments non affichés."""
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup.find_all(["ix:header", "ix:hidden", "script", "style", "head"]):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        tag.decompose()
    return soup


def _md_to_text(md):
    """Markdown -> texte lisible : enlève #, **, * ; garde les tableaux."""
    md = re.sub(r"^#{1,6}\s*", "", md, flags=re.M)
    md = re.sub(r"\*\*(.+?)\*\*", r"\1", md, flags=re.S)
    md = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*(?!\*)", r"\1", md, flags=re.S)
    return md


def _text_to_pdf(text):
    """Texte -> PDF (reflow simple). Robuste aux tokens très longs."""
    lines = []
    for line in text.split("\n"):
        wrapped = textwrap.wrap(line, width=100, break_long_words=True,
                                break_on_hyphens=False)
        lines.extend(wrapped or [""])

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    font = _font_path()
    if font:
        pdf.add_font("body", "", font)
        pdf.set_font("body", size=8)
    else:                                   # secours : latin-1 (accents perdus)
        pdf.set_font("helvetica", size=8)
        lines = [l.encode("latin-1", "replace").decode("latin-1") for l in lines]

    for l in lines:
        try:
            pdf.multi_cell(0, 3.5, l if l else " ")
        except Exception:
            pass                            # ligne pathologique ignorée
    return bytes(pdf.output())


def render_output(html_text, fmt):
    """Renvoie (bytes, extension) pour le format demandé : 'md', 'txt' ou 'pdf'."""
    try:
        soup = _clean_soup(html_text)
        md = _to_md(str(soup), heading_style="ATX", strip=["script", "style"])
    except Exception:
        return None, fmt

    if fmt == "txt":
        return _md_to_text(md).encode("utf-8"), "txt"
    if fmt == "pdf":
        return _text_to_pdf(_md_to_text(md)), "pdf"
    return md.encode("utf-8"), "md"          # défaut : markdown


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
#  EXPLORATION PAR CODE SIC  (ne télécharge aucun rapport)
# ---------------------------------------------------------------------------
ROW_RE = re.compile(
    r'CIK=(\d{10})[^>]*>\s*\d{10}\s*</a>\s*</td>\s*<td[^>]*>(.*?)</td>',
    re.S | re.I)


async def browse_by_sic(sic, form_type, max_companies, user_agent, progress=None):
    """Liste les sociétés d'un code SIC via browse-edgar (pagination 100/page)."""
    limiter = RateLimiter(0.12)
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    found = []
    seen = set()

    async with aiohttp.ClientSession(headers=headers) as session:
        start = 0
        while len(found) < max_companies:
            url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                   f"&SIC={sic}&type={form_type}&dateb=&owner=include"
                   f"&count=100&start={start}&output=html")
            html = await fetch(session, url, limiter)
            if not html:
                break

            rows = ROW_RE.findall(html)
            if not rows:
                break

            for cik, raw_name in rows:
                if cik in seen:
                    continue
                seen.add(cik)
                name = re.sub(r"<[^>]+>", "", raw_name).strip()
                name = re.sub(r"\s+", " ", name)
                found.append({"CIK": cik, "Société": name})
                if len(found) >= max_companies:
                    break

            if progress:
                progress(len(found))

            if len(rows) < 100:
                break
            start += 100

    return found


# ---------------------------------------------------------------------------
#  Pipeline de téléchargement (async) — écrit sur DISQUE, pas en RAM
# ---------------------------------------------------------------------------
async def run_pipeline(idents, forms, ymin, ymax, user_agent, cap, fmt, ui):
    limiter = RateLimiter(0.12)
    logs = []
    listing = []
    workdir = tempfile.mkdtemp(prefix="edgar_")

    def log(msg):
        logs.append(msg)
        ui["log"].code("\n".join(logs[-150:]), language=None)

    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            # --- Phase 1 : listing des dépôts (léger) ---
            need_map = any(not resolve_cik(c, {}) for c in idents)
            if need_map:
                ui["status"].info("Chargement de la table ticker → CIK…")
                ticker_map = await load_ticker_map(session, limiter)
            else:
                ticker_map = {}

            ui["status"].info(f"Listing des dépôts pour {len(idents)} société(s)…")
            sem_list = asyncio.Semaphore(5)

            async def list_one(ident):
                async with sem_list:
                    cik = resolve_cik(ident, ticker_map)
                    if not cik:
                        log(f"[X] '{ident}' introuvable.")
                        return None
                    name, filings = await get_all_filings(session, cik, limiter)
                    if not filings:
                        log(f"[X] Aucune donnée pour {ident} (CIK {cik}).")
                        return None
                    sel = [f for f in filings if keep(f, forms, ymin, ymax)]
                    return (cik, name, sel)

            results = await asyncio.gather(*[list_one(i) for i in idents])
            companies = [r for r in results if r]
            total = sum(len(s) for _, _, s in companies)

            log(f"→ {len(companies)} société(s), {total} rapport(s) correspondants.")

            if total == 0:
                ui["status"].warning("Aucun rapport trouvé. Vérifie les CIK, formes et période.")
                return None, None, 0, 0

            # --- GARDE-FOU : plafond ---
            if total > cap:
                ui["status"].error(
                    f"⛔ {total} rapports dépassent le plafond de {cap}. "
                    "Réduis la période, le nombre de sociétés, ou augmente le plafond "
                    "dans la barre latérale (au risque de saturer la mémoire)."
                )
                return None, None, 0, total

            # --- Phase 2 : téléchargement + conversion ---
            done = {"n": 0}
            sem = asyncio.Semaphore(5)

            async def handle(cik, name, f):
                async with sem:
                    url = doc_url(cik, f)
                    y = year_of(f)
                    base = f"{y}_{f['form'].replace('/', '-')}"
                    folder = safe_name(name) or cik

                    html = await fetch(session, url, limiter)
                    out_path = ""
                    if html is None:
                        log(f"[X] {name} {base} : échec téléchargement")
                    else:
                        data, ext = await asyncio.to_thread(render_output, html, fmt)
                        del html                      # libère la RAM tout de suite
                        if data:
                            d = os.path.join(workdir, folder)
                            os.makedirs(d, exist_ok=True)
                            fname = f"{base}.{ext}"
                            with open(os.path.join(d, fname), "wb") as out:
                                out.write(data)
                            out_path = f"{folder}/{fname}"
                            log(f"[OK] {name} {base}.{ext}")
                        else:
                            log(f"[!] {name} {base} : conversion échouée")

                    listing.append({
                        "societe": name, "cik": cik, "forme": f["form"], "annee": y,
                        "date_depot": f["filing_date"], "date_rapport": f["report_date"],
                        "accession": f["accession"], "fichier": out_path, "url_sec": url,
                    })
                    done["n"] += 1
                    ui["bar"].progress(done["n"] / total)
                    ui["status"].info(f"{done['n']} / {total} rapports traités…")

            await asyncio.gather(*[handle(cik, name, f)
                                   for cik, name, sel in companies for f in sel])

        # --- Phase 3 : listing CSV + ZIP depuis le disque ---
        listing.sort(key=lambda r: (r["societe"], r["annee"] or 0))
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(listing[0].keys()))
        w.writeheader()
        w.writerows(listing)
        csv_text = buf.getvalue()

        with open(os.path.join(workdir, "listing_filings.csv"), "w",
                  encoding="utf-8-sig", newline="") as cf:
            cf.write(csv_text)

        zip_path = os.path.join(tempfile.gettempdir(), "edgar_filings.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(workdir):
                for fn in files:
                    full = os.path.join(root, fn)
                    z.write(full, os.path.relpath(full, workdir))

        with open(zip_path, "rb") as zf:
            zip_bytes = zf.read()
        os.remove(zip_path)

        n_ok = sum(1 for r in listing if r["fichier"])
        return zip_bytes, csv_text, n_ok, total

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
#  INTERFACE
# ---------------------------------------------------------------------------
st.title("📄 SEC EDGAR → Markdown")
st.caption("Télécharge les rapports annuels (10-K / 20-F), les convertit en Markdown "
           "et les empaquette en ZIP.")

with st.sidebar:
    st.header("⚙️ Paramètres")
    user_agent = st.text_input(
        "User-Agent (nom + email)",
        placeholder="Prenom Nom email@exemple.com",
        help="Exigé par la SEC sur chaque requête. Sans email valide → erreur 403.",
    )
    forms = st.multiselect("Formes", ["10-K", "20-F"], default=["10-K", "20-F"])

    fmt_label = st.radio(
        "Format de sortie",
        ["Markdown (.md)", "Texte (.txt)", "PDF (.pdf)"],
        help="Markdown : idéal pour l'extraction. Texte : brut lisible. "
             "PDF : document reflowé (plus lent à générer).",
    )
    fmt = {"Markdown (.md)": "md", "Texte (.txt)": "txt", "PDF (.pdf)": "pdf"}[fmt_label]

    c1, c2 = st.columns(2)
    ymin = c1.number_input("Année min", 1995, 2100, 2020)
    ymax = c2.number_input("Année max", 1995, 2100, datetime.now().year)

    st.divider()
    st.subheader("🛡️ Garde-fous")
    cap = st.slider("Plafond de rapports par lancement", 20, HARD_CAP, DEFAULT_CAP, step=10,
                    help="L'app refuse de lancer au-delà. Protège la mémoire (1 Go) "
                         "et le quota SEC.")
    st.caption(f"Au-delà de ~{DEFAULT_CAP} rapports, procède plutôt par lots successifs.")

    st.divider()
    st.caption("Source : data.sec.gov — API publique et gratuite.")

mode = st.radio("Mode de sélection", ["✍️ Saisie manuelle", "🔎 Explorer par code SIC"],
                horizontal=True)

# --- Mode 2 : explorateur SIC -------------------------------------------------
if mode == "🔎 Explorer par code SIC":
    st.markdown("**Étape 1 — Lister les sociétés d'un secteur** "
                "_(aucun rapport n'est téléchargé à ce stade)_")

    e1, e2, e3 = st.columns([3, 1, 1])
    sic_label = e1.selectbox("Secteur (code SIC)", list(SIC_CODES.keys()))
    sic = SIC_CODES[sic_label]
    if not sic:
        sic = e1.text_input("Code SIC personnalisé", placeholder="ex. 3841")
    max_comp = e2.number_input("Max sociétés", 10, 500, 100, step=10)
    form_filter = e3.selectbox("Ayant déposé", ["10-K", "20-F"])

    if st.button("🔎 Lister les sociétés", use_container_width=True):
        if "@" not in user_agent:
            st.error("Renseigne d'abord un User-Agent avec un email (barre latérale).")
        elif not sic:
            st.error("Indique un code SIC.")
        else:
            ph = st.empty()
            ph.info("Exploration en cours…")
            try:
                comps = asyncio.run(browse_by_sic(
                    sic, form_filter, int(max_comp), user_agent,
                    progress=lambda n: ph.info(f"{n} sociétés trouvées…")))
                st.session_state["sic_results"] = comps
                ph.success(f"{len(comps)} société(s) trouvée(s) pour le SIC {sic}.")
            except Exception as e:
                ph.error(f"Erreur d'exploration : {e}")

    if st.session_state.get("sic_results"):
        comps = st.session_state["sic_results"]
        st.markdown("**Étape 2 — Choisir l'échantillon**")

        n_years = max(1, int(ymax) - int(ymin) + 1)
        s1, s2 = st.columns([1, 2])
        n_take = s1.number_input("Nombre de sociétés à retenir", 1, len(comps),
                                 min(20, len(comps)))
        est = int(n_take) * n_years
        if est > cap:
            s2.error(f"⚠️ Estimation ≈ **{est} rapports** ({n_take} sociétés × {n_years} ans) "
                     f"→ dépasse le plafond de {cap}. Réduis la sélection ou la période.")
        else:
            s2.success(f"Estimation ≈ **{est} rapports** "
                       f"({n_take} sociétés × {n_years} ans). Dans le plafond de {cap}.")

        selection = comps[:int(n_take)]
        st.dataframe(selection, use_container_width=True, height=240)

        ciks_raw = "\n".join(c["CIK"] for c in selection)
        with st.expander("Voir / copier la liste de CIK"):
            st.code(ciks_raw)
    else:
        ciks_raw = ""

# --- Mode 1 : saisie manuelle -------------------------------------------------
else:
    ciks_raw = st.text_area(
        "Liste de CIK ou tickers",
        value="0001477449\n0001093557\n0001388658",
        height=140,
        help="Un par ligne, ou séparés par virgule/espace. Les tickers (AAPL) sont acceptés.",
    )
    n_ids = len([c for c in re.split(r"[\s,;]+", ciks_raw) if c.strip()])
    n_years = max(1, int(ymax) - int(ymin) + 1)
    est = n_ids * n_years
    if n_ids:
        (st.error if est > cap else st.info)(
            f"Estimation ≈ {est} rapports ({n_ids} sociétés × {n_years} ans) — "
            f"plafond : {cap}.")

# --- Lancement ----------------------------------------------------------------
st.divider()
launch = st.button("🚀 Lancer le téléchargement", type="primary", use_container_width=True)

if launch:
    idents = [c for c in re.split(r"[\s,;]+", ciks_raw) if c.strip()]
    if not idents:
        st.error("Aucune société sélectionnée.")
    elif "@" not in user_agent:
        st.error("Renseigne un User-Agent avec un email valide (barre latérale).")
    elif not forms:
        st.error("Sélectionne au moins une forme (10-K ou 20-F).")
    else:
        ui = {"bar": st.progress(0.0), "status": st.empty(), "log": st.empty()}
        try:
            zip_bytes, csv_text, n_ok, n_total = asyncio.run(
                run_pipeline(idents, set(forms), int(ymin), int(ymax),
                             user_agent, int(cap), fmt, ui))
            if zip_bytes:
                st.session_state["zip"] = zip_bytes
                st.session_state["csv"] = csv_text
                ui["status"].success(f"✅ Terminé : {n_ok} / {n_total} rapport(s) convertis.")
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
