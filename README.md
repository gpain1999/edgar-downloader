# EDGAR Downloader — version Streamlit

App web qui télécharge les rapports annuels (10-K / 20-F) depuis SEC EDGAR,
les convertit en Markdown et les propose en ZIP.

Une fois déployée, **tes collègues n'installent rien** : ils ouvrent une URL.

## Structure du dépôt

```
edgar-downloader/
├── streamlit_app.py        ← l'application web (fichier principal)
├── requirements.txt        ← dépendances de l'app
├── README.md
├── .gitignore
├── .streamlit/
│   └── config.toml         ← thème de l'interface
└── cli/                    ← bonus : version ligne de commande
    ├── edgar_download.py
    └── requirements.txt
```

Seuls `streamlit_app.py` et `requirements.txt` (à la racine) sont nécessaires
au déploiement. Le dossier `cli/` est la version script, utile en local.

---

## Créer le dépôt GitHub

**Option A — sans git (interface web) :** crée un dépôt public sur GitHub,
puis "Add file" → "Upload files" et dépose le contenu du dossier.

**Option B — en ligne de commande :**

```bash
cd edgar-downloader
git init
git add .
git commit -m "EDGAR downloader : app Streamlit + script CLI"
git branch -M main
git remote add origin https://github.com/TON-COMPTE/edgar-downloader.git
git push -u origin main
```

---

## Déployer sur Streamlit Community Cloud (gratuit) — ~5 min

1. **Crée un dépôt GitHub PUBLIC** avec les deux fichiers à la racine.
   (Interface web GitHub : "Add file" → "Upload files" suffit, pas besoin de git.)
   ⚠️ Le dépôt doit être **public** : c'est une exigence du tier gratuit.

2. Va sur **https://share.streamlit.io** et connecte-toi avec GitHub.

3. Clique **Create app** → **Deploy a public app from GitHub**, puis renseigne :
   - **Repository** : ton dépôt
   - **Branch** : `main`
   - **Main file path** : `streamlit_app.py`

4. Clique **Deploy**. Après 2–3 minutes, l'app est en ligne sur une URL du type
   `https://ton-app.streamlit.app`.

5. Partage l'URL. Chacun saisit sa liste de CIK, son nom + email dans le champ
   User-Agent, et télécharge le ZIP.

---

## Lancer en local (pour tester avant de déployer)

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

---

## Bon à savoir (limites du tier gratuit)

- **Dépôt public obligatoire.** Le code est visible de tous. Ici sans importance :
  aucun secret n'est stocké (chaque utilisateur saisit son propre User-Agent).

- **1 Go de RAM par app.** Le ZIP est construit en mémoire, donc évite de lancer
  200 rapports d'un coup. Par lots de ~50, aucun souci.

- **Mise en veille.** Après quelques jours sans visite, l'app s'endort ; la
  première visite suivante la réveille en ~30 s. Ensuite c'est fluide.

- **IP partagée.** La limite SEC de 10 requêtes/seconde s'applique à l'IP du
  serveur Streamlit. Si plusieurs collègues lancent en même temps, ils partagent
  ce quota (l'app respecte la limite, ça ralentit simplement un peu).

- **User-Agent obligatoire.** La SEC exige un en-tête avec un email valide.
  Sans email, l'app refuse de lancer.

---

## Streamlit ou Render ?

Les deux sont gratuits et hébergent du Python. Streamlit est **plus simple**
(pas de HTML à maintenir, déploiement en 3 champs) et offre **1 Go de RAM**
contre 512 Mo chez Render. Render permet en revanche un dépôt **privé** et une
interface entièrement sur mesure.

Pour un usage interne entre collègues : **Streamlit**.
