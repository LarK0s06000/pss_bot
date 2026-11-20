import os, re, sys, json, html, time, csv, unicodedata, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote_plus

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE = "https://www.pss-archi.eu"
PAGE_NOUVEAUX = BASE + "/nouveaux_immeubles.html"
PAGE_MODIFS = BASE + "/dernieres_modifications.html"
ETAT = "state.json"

# Statuts qui ne déclenchent AUCUNE alerte
IGNORER = {
    "ic_construit.gif", "ic_annule.gif", "ic_detruit.gif",
    "ic_interrompu.gif", "ic_vision.gif", "ic_ruine.gif",
}
ICONES = {
    "ic_construit.gif": "Construit",
    "ic_renov.gif": "En rénovation",
    "ic_projetr.gif": "Projet retenu",
    "ic_construction.gif": "En construction",
    "ic_projetp.gif": "Projet proposé",
    "ic_annule.gif": "Annulé",
    "ic_detruit.gif": "Détruit",
    "ic_interrompu.gif": "Chantier interrompu",
    "ic_vision.gif": "Vision",
    "ic_ruine.gif": "Ruine",
}
EMOJI = {"Projet retenu": "✅", "Projet proposé": "💡",
         "En construction": "🏗️", "En rénovation": "🛠️"}
H = {"User-Agent": "Mozilla/5.0 (veille personnelle)"}
PARIS = ZoneInfo("Europe/Paris")

MAX_PHOTOS = 4          # photos envoyées par alerte (la 1re porte le texte)
LIMITE_LEGENDE = 1000   # Telegram limite la légende d'une photo à 1024 caractères


def esc(s):
    return html.escape(s or "")


def code_ok(cp):
    return cp.startswith("06") or cp == "98000"


def date_iso(txt):
    txt = txt.strip()
    hm = re.search(r"(\d{1,2})h(\d{2})", txt)
    h, mi = (int(hm.group(1)), int(hm.group(2))) if hm else (0, 0)
    jour = datetime.now(PARIS)
    if txt.lower().startswith("hier"):
        jour -= timedelta(days=1)
    else:
        m = re.search(r"(\d{2})/(\d{2})/(\d{4})", txt)
        if m:
            jour = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return jour.strftime("%Y-%m-%d") + f" {h:02d}:{mi:02d}"


def lire(page):
    r = requests.get(page, headers=H, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    lignes, vus = [], set()
    for a in soup.find_all("a", href=re.compile(r"immeubles/[A-Z]{2}-")):
        tr = a.find_parent("tr")
        if tr is None:
            continue
        url = urljoin(page, a["href"])
        if url in vus:
            continue
        texte = tr.get_text(" ", strip=True)
        m = re.search(r"\((\d{5})\)", texte)
        if not m:
            continue
        vus.add(url)
        tds = tr.find_all("td")
        img = tr.find("img", src=re.compile(r"ic_"))
        ville = ""
        if len(tds) > 1:
            ville = re.sub(r"\s*,\s*(France|Monaco)\s*$", "",
                           tds[1].get_text(" ", strip=True))
        lignes.append({
            "url": url,
            "nom": a.get_text(strip=True),
            "ville": ville,
            "cp": m.group(1),
            "fonction": tds[2].get_text(" ", strip=True) if len(tds) > 2 else "",
            "icone": img["src"].split("/")[-1] if img else None,
            "date": date_iso(tds[-1].get_text()) if tds else "",
        })
    if not lignes:
        print("DEBUG : aucune ligne reconnue sur", page)
        print("Taille de la page :", len(r.text))
        print("Début de la page :", r.text[:800])
        tr = soup.find("tr")
        print("Première ligne de tableau :", str(tr)[:800] if tr else "aucun <tr>")
    return lignes


def statut(l):
    if l["icone"] in IGNORER:
        return None
    return ICONES.get(l["icone"], f"statut inconnu ({l['icone']})")


# ---------------------------------------------------------------------------
# Lecture de la fiche détaillée (maître d'ouvrage, architecte, surface, permis, photos)
# ---------------------------------------------------------------------------
ETIQUETTES = {
    "identifiant pss", "nom", "noms alternatifs", "ville", "quartier",
    "adresse(s)", "coordonnées", "statut", "livraison prévisionnelle",
    "réalisation", "fonction(s)", "style architectural", "données techniques",
    "niveaux", "hauteur totale", "hauteur du toit", "hauteur totale estimée",
    "hauteur du toit estimée", "surface de plancher", "surface du terrain",
    "architectes", "architecte", "maître d'ouvrage", "promoteur",
    "permis de construire", "entreprise(s)", "localisation",
    "notes et références", "à propos de cette fiche",
}


def bloc(lignes, noms, max_lignes=4):
    """Lignes qui suivent une étiquette (ex. 'Maître d'ouvrage') jusqu'à la suivante."""
    noms = {n.lower() for n in noms}
    for i, l in enumerate(lignes):
        if l.lower().rstrip(" :") in noms:
            sortie = []
            for s in lignes[i + 1:i + 1 + max_lignes + 2]:
                if (s.lower().rstrip(" :") in ETIQUETTES or len(s) > 80
                        or s.endswith(".") or s.startswith("Ajoutée par")):
                    break
                sortie.append(s)
                if len(sortie) >= max_lignes:
                    break
            return sortie
    return []


def photos_fiche(soup):
    """Photos du projet : (url grande, url petite, crédit)."""
    marqueur = soup.find(string=re.compile(r"Identifiant PSS"))
    imgs = list(reversed(marqueur.find_all_previous("img"))) if marqueur \
        else soup.find_all("img")
    photos, vues = [], set()
    for img in imgs:
        src = img.get("src", "")
        if "/photos/membres/" not in src:
            continue
        petite = urljoin(BASE, src)
        if petite in vues:
            continue
        vues.add(petite)
        grande = re.sub(r"(/photos/membres/\d+/)s/", r"\1l/", petite, count=1)
        credit = ""
        suivant = img.find_next(string=re.compile("©"))
        if suivant:
            m = re.fullmatch(r"\s*©\s*(\S+)\s*", str(suivant))
            if m:
                credit = "© " + m.group(1)
        photos.append((grande, petite, credit))
    return photos


def lire_fiche(url):
    r = requests.get(url, headers=H, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    lignes = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
    plat = " ".join(lignes)

    d = {}
    d["maitre"] = " ; ".join(bloc(lignes, ["Maître d'ouvrage", "Promoteur"], 3))
    archi = bloc(lignes, ["Architectes", "Architecte"], 4)
    d["architectes"] = f"{archi[0]} ({archi[1]})" if len(archi) == 2 else " ; ".join(archi)

    m = re.search(r"Surface de plancher\s+([\d\s\u00a0\u202f.,]+\s*m²)", plat)
    d["surface"] = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    m = re.search(r"Niveaux\s+(R[+\-]?\S*|\S+)", plat)
    d["niveaux"] = m.group(1) if m else ""
    m = re.search(r"(Livraison prévisionnelle|Réalisation)\s+(≈?\d{4}(?:\s*[-–/]\s*≈?\d{4})?)", plat)
    d["livraison"] = ("Livraison prévue " if m and m.group(1).startswith("Livraison")
                      else "Réalisation ") + m.group(2) if m else ""

    # Permis de construire : numéro (section dédiée ou texte) + date
    numero = " ".join(bloc(lignes, ["Permis de construire"], 2))
    if not numero:
        m = re.search(r"[Pp]ermis de construire n°\s*(\d[\dA-Za-z ]{8,30}?)\s+"
                      r"(?:déposé|délivré|accordé|refusé|modifié)", plat)
        numero = "N° " + m.group(1).strip() if m else ""
    m = re.search(r"((?:déposé|délivré|accordé|refusé)\s+le\s+"
                  r"(?:\d{1,2}(?:er)?\s+\w+\s+\d{4}|\d{2}/\d{2}/\d{4}))", plat)
    d["permis"] = (numero + (" " + m.group(1) if m and numero else "")).strip()
    d["permis_url"] = ""
    for a in soup.find_all("a", href=True):
        if re.search(r"/au\d+$", a["href"]):
            d["permis_url"] = urljoin(BASE, a["href"])
            break

    # Résumé du projet : 1re phrase longue entre "Données techniques" et "Localisation"
    deb = next((i for i, l in enumerate(lignes) if l.lower().startswith("données techniques")), 0)
    fin = next((i for i, l in enumerate(lignes) if l == "Localisation"), len(lignes))
    d["description"] = next((l for l in lignes[deb:fin]
                             if len(l) > 50 and not l.startswith("Permis de construire n")
                             and "Aucune photo" not in l), "")

    h1 = soup.find("h1")
    d["nom"] = h1.get_text(" ", strip=True) if h1 else ""
    marqueur = soup.find(string=re.compile(r"Identifiant PSS"))
    lien_ville = marqueur.find_next("a", href=re.compile(r"/villes/")) if marqueur else None
    if lien_ville is None:
        lien_ville = soup.find("a", href=re.compile(r"/villes/"))
    d["ville"] = lien_ville.get_text(" ", strip=True) if lien_ville else ""
    lien_agence = soup.find("a", href=re.compile(r"/agence/\d+"))
    d["agence_url"] = urljoin(BASE, lien_agence["href"]) if lien_agence else ""

    d["photos"] = photos_fiche(soup)[:MAX_PHOTOS]
    d["credit"] = ", ".join(sorted({p[2] for p in d["photos"] if p[2]}))
    return d


# ---------------------------------------------------------------------------
# Messages Telegram
# ---------------------------------------------------------------------------
def numero_fiche(url):
    m = re.search(r"-(\d+)\.html$", url)
    return m.group(1) if m else ""


def construire_texte(titre, l, st, d):
    lignes = [titre, f"{EMOJI.get(st, '❔')} {esc(st)}", f"<b>{esc(l['nom'])}</b>",
              f"📍 {esc(l['ville'])}"]
    if l.get("fonction"):
        lignes.append(f"Fonction : {esc(l['fonction'])}")
    if d:
        lignes.append("🏢 Maître d'ouvrage : " +
                      (f"<b>{esc(d['maitre'])}</b>" if d["maitre"] else "non renseigné"))
        lignes.append("✏️ Architecte : " + (esc(d["architectes"]) if d["architectes"]
                                            else "non renseigné"))
        tech = [x for x in (f"Surface de plancher {d['surface']}" if d["surface"] else "",
                            d["niveaux"], d["livraison"]) if x]
        if tech:
            lignes.append("📐 " + esc(" · ".join(tech)))
        lignes.append("📄 Permis : " + (esc(d["permis"]) if d["permis"] else "non renseigné"))
        if d["credit"]:
            lignes.append(f"📷 Photos {esc(d['credit'])} (PSS)")
    texte = "\n".join(lignes)
    if d and d["description"]:
        reste = LIMITE_LEGENDE - len(texte) - 4
        desc = d["description"]
        if reste > 40:
            if len(esc(desc)) > reste:
                desc = desc[:reste]
                while len(esc(desc)) > reste - 1:
                    desc = desc[:-5]
                desc = desc.rstrip() + "…"
            texte += "\nℹ️ " + esc(desc)
    return texte


def recherche(requete):
    return "https://www.google.com/search?q=" + quote_plus(requete)


def construire_boutons(l, d, modif):
    ligne1 = [{"text": "📄 Ouvrir la fiche", "url": l["url"]}]
    if modif:
        ligne1.append({"text": "🕘 Historique", "url": f"{BASE}/historique-{numero_fiche(l['url'])}.html"})
    lignes = [ligne1]

    ligne2 = []
    if d and d.get("permis_url"):
        ligne2.append({"text": "📋 Détail du permis", "url": d["permis_url"]})
    if d and d.get("agence_url"):
        ligne2.append({"text": "🏢 Agence de l'architecte", "url": d["agence_url"]})
    if ligne2:
        lignes.append(ligne2)

    # Raccourcis de recherche pour trouver le bureau d'études (BET)
    nom = l["nom"].replace('"', "")
    ville = re.sub(r"\s*\(\d{5}\)", "", l.get("ville", "")).strip()
    agence = ""
    if d and d.get("architectes"):
        agence = d["architectes"].split(" (")[0].split(" ; ")[0].strip().replace('"', "")
    requete_bet = f'"{nom}" {ville}'
    if agence:
        requete_bet += f' "{agence}"'
    requete_bet += ' ("BET fluides" OR "bureau d\'études")'
    lignes.append([
        {"text": "🔎 Chercher le BET", "url": recherche(requete_bet)},
        {"text": "🏛️ Marchés publics",
         "url": recherche(f'"{nom}" {ville} site:boamp.fr')},
    ])
    return lignes


def api(methode, data=None, files=None):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{methode}",
                      data=data, files=files, timeout=60)
    r.raise_for_status()
    return r


def telecharger(url):
    try:
        r = requests.get(url, headers=H, timeout=30)
        if (r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image")
                and len(r.content) < 9_000_000):
            return r.content
    except requests.RequestException:
        pass
    return None


def envoyer(texte, boutons, photos):
    """Photo + texte + boutons ; repli sur un message texte si la photo échoue."""
    markup = json.dumps({"inline_keyboard": boutons})
    base = {"chat_id": CHAT_ID, "parse_mode": "HTML"}
    envoyees = []
    for grande, petite, _ in photos:
        octets = telecharger(grande) or telecharger(petite)
        if octets:
            envoyees.append(octets)
    if envoyees:
        try:
            api("sendPhoto", {**base, "caption": texte, "reply_markup": markup},
                {"photo": ("photo.jpg", envoyees[0])})
            if len(envoyees) > 1:
                media = [{"type": "photo", "media": f"attach://p{i}"}
                         for i in range(1, len(envoyees))]
                fichiers = {f"p{i}": (f"p{i}.jpg", envoyees[i]) for i in range(1, len(envoyees))}
                try:
                    api("sendMediaGroup", {"chat_id": CHAT_ID, "media": json.dumps(media)}, fichiers)
                except requests.RequestException as exc:
                    print("Photos supplémentaires non envoyées :", exc)
            return
        except requests.RequestException as exc:
            print("Envoi de la photo impossible, repli sur le texte :", exc)
    api("sendMessage", {**base, "text": texte, "reply_markup": markup,
                        "disable_web_page_preview": "true"})


def alerte(titre, l, st, modif):
    try:
        d = lire_fiche(l["url"])
    except Exception as exc:  # une fiche illisible ne doit pas bloquer l'alerte
        print("Fiche non lue :", l["url"], exc)
        d = None
    texte = construire_texte(titre, l, st, d)
    envoyer(texte, construire_boutons(l, d, modif), d["photos"] if d else [])


def telegram(txt):
    api("sendMessage", {"chat_id": CHAT_ID, "text": txt})


# ---------------------------------------------------------------------------
# Liste des projets : commande Telegram /projets  ou  python bot.py --liste
# ---------------------------------------------------------------------------
# Listes PSS par statut : 1 = en construction, 2 = projet retenu
LISTES = [
    ("Alpes-Maritimes (06)", BASE + "/immeubles/FR/3-6/{code}/1", ""),
    ("Monaco (98000)", BASE + "/immeubles/MC/5-98000/{code}/1", "Monaco (98000)"),
]
CODES_STATUT = {"1": "En construction", "2": "Projet retenu"}
ORDRE_STATUT = {"En construction": 0, "Projet retenu": 1}
AIDE = ("Commandes :\n"
        "/projets : tous les projets en construction et retenus (06 + Monaco)\n"
        "/projets retenu  ou  /projets construction\n"
        "/projets nice  (ou cannes, monaco, logements, hôtel...) : filtre libre\n"
        "Tri : annee (livraison la plus proche), surface, hauteur, nom, ville\n"
        "Exemple : /projets retenu monaco surface")


def norm(s):
    t = unicodedata.normalize("NFD", str(s).lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def soup_de(url):
    r = requests.get(url, headers=H, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def nombre(txt):
    t = re.sub(r"[\s\u00a0\u202f]", "", txt or "")
    m = re.search(r"\d+(?:[.,]\d+)?", t)
    return float(m.group(0).replace(",", ".")) if m else None


def lignes_tableau(soup, zone, ville_defaut, statut):
    sortie = []
    for table in soup.find_all("table"):
        premiere = table.find("tr")
        if premiere is None:
            continue
        entetes = [norm(c.get_text(" ", strip=True)) for c in premiere.find_all(["th", "td"])]

        def col(mot):
            return next((i for i, h in enumerate(entetes) if mot in h), None)

        for tr in table.find_all("tr"):
            lien = next((a for a in tr.find_all("a", href=True)
                         if re.search(r"/immeubles/[A-Z]{2}-", a["href"])), None)
            if lien is None:
                continue
            cellules = [td.get_text(" ", strip=True) for td in tr.find_all("td")]

            def val(mot):
                i = col(mot)
                return cellules[i] if i is not None and i < len(cellules) else ""

            sortie.append({
                "statut": statut, "zone": zone,
                "nom": lien.get_text(" ", strip=True),
                "url": urljoin(BASE, lien["href"]),
                "ville": val("ville") or ville_defaut,
                "fonction": val("fonction"), "surface": val("surface"),
                "hauteur": val("hauteur"), "annee": val("annee"),
            })
    return sortie


def lire_liste(url, zone, ville_defaut, statut):
    base = url.rsplit("/", 1)[0]
    soup = soup_de(url)
    lignes = lignes_tableau(soup, zone, ville_defaut, statut)
    derniere = 1
    for a in soup.find_all("a", href=True):
        h = urljoin(url, a["href"])
        if h.startswith(base + "/") and h[len(base) + 1:].isdigit():
            derniere = max(derniere, int(h[len(base) + 1:]))
    for page in range(2, min(derniere, 40) + 1):
        time.sleep(1)
        lignes += lignes_tableau(soup_de(f"{base}/{page}"), zone, ville_defaut, statut)
    return lignes


def recuperer_projets(codes):
    projets, erreurs, vus = [], [], set()
    for zone, modele, ville_defaut in LISTES:
        for code in codes:
            try:
                for p in lire_liste(modele.format(code=code), zone, ville_defaut, CODES_STATUT[code]):
                    if p["url"] not in vus:
                        vus.add(p["url"])
                        projets.append(p)
            except Exception as exc:
                erreurs.append(f"{zone} / {CODES_STATUT[code]} : {exc}")
            time.sleep(1)
    return projets, erreurs


def parser_demande(texte):
    codes, filtres, tri = set(), [], None
    for mot in re.split(r"[\s,;]+", norm(texte)):
        if not mot or mot.startswith("/") or mot in ("projet", "projets", "liste", "de", "les", "en"):
            continue
        if mot in ("construction", "chantier", "travaux"):
            codes.add("1")
        elif mot in ("retenu", "retenus"):
            codes.add("2")
        elif mot in ("annee", "livraison", "date"):
            tri = "annee"
        elif mot in ("surface", "hauteur", "nom", "ville", "statut"):
            tri = mot
        else:
            filtres.append(mot)
    return (sorted(codes) or ["1", "2"]), filtres, tri


def filtrer(projets, filtres):
    def ok(p):
        champs = norm(" ".join((p["ville"], p["nom"], p["fonction"], p["zone"])))
        return all(f in champs for f in filtres)
    return [p for p in projets if ok(p)]


def trier(projets, tri):
    def vide_dernier(v, desc=False):
        return (v is None, -(v or 0) if desc else (v or 0))
    cles = {
        "annee": lambda p: (*vide_dernier(nombre(p["annee"])), norm(p["ville"]), norm(p["nom"])),
        "surface": lambda p: (*vide_dernier(nombre(p["surface"]), True), norm(p["nom"])),
        "hauteur": lambda p: (*vide_dernier(nombre(p["hauteur"]), True), norm(p["nom"])),
        "nom": lambda p: norm(p["nom"]),
        "ville": lambda p: (norm(p["ville"]), ORDRE_STATUT[p["statut"]],
                            *vide_dernier(nombre(p["annee"])), norm(p["nom"])),
    }
    defaut = lambda p: (ORDRE_STATUT[p["statut"]], norm(p["ville"]),
                        *vide_dernier(nombre(p["annee"])), norm(p["nom"]))
    return sorted(projets, key=cles.get(tri, defaut))


def exporter(projets, base_nom):
    colonnes = ["Statut", "Nom", "Ville", "Fonction(s)", "Surface", "Hauteur", "Année", "Lien"]
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook()
        ws = wb.active
        ws.title = "Projets"
        ws.append(colonnes)
        for c in ws[1]:
            c.font = Font(bold=True)
        for p in projets:
            ws.append([p["statut"], p["nom"], p["ville"], p["fonction"], p["surface"],
                       p["hauteur"], p["annee"], "Ouvrir la fiche"])
            cellule = ws.cell(row=ws.max_row, column=8)
            cellule.hyperlink = p["url"]
            cellule.style = "Hyperlink"
        for lettre, largeur in zip("ABCDEFGH", (16, 42, 24, 34, 12, 12, 8, 16)):
            ws.column_dimensions[lettre].width = largeur
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        chemin = base_nom + ".xlsx"
        wb.save(chemin)
        return chemin
    except ImportError:
        chemin = base_nom + ".csv"
        with open(chemin, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(colonnes)
            for p in projets:
                w.writerow([p["statut"], p["nom"], p["ville"], p["fonction"], p["surface"],
                            p["hauteur"], p["annee"], p["url"]])
        return chemin


def message_resume(projets, filtres, tri, erreurs):
    lignes = ["📋 <b>Projets en construction / retenus</b> (06 + Monaco)"]
    for statut, emoji in (("En construction", "🏗️"), ("Projet retenu", "✅")):
        sous = [p for p in projets if p["statut"] == statut]
        if sous:
            detail = ", ".join(f"{z.split(' (')[0]} : {sum(1 for p in sous if p['zone'] == z)}"
                               for z, _, _ in LISTES if any(p["zone"] == z for p in sous))
            lignes.append(f"{emoji} {statut} : {len(sous)} ({detail})")
    lignes.append("Filtre : " + (esc(" + ".join(filtres)) if filtres else "aucun"))
    lignes.append("Tri : " + {"annee": "livraison la plus proche", "surface": "plus grande surface",
                              "hauteur": "plus haut", "nom": "nom", "ville": "ville"}.get(
                                  tri, "statut, ville, année"))
    lignes.append("")
    for p in projets[:10]:
        an = f" ({esc(p['annee'])})" if p["annee"] and p["annee"] != "‐" else ""
        lignes.append(f"• {esc(p['nom'])} — {esc(p['ville'])}{an}")
    if len(projets) > 10:
        lignes.append(f"… et {len(projets) - 10} autres dans le fichier.")
    for e_ in erreurs:
        lignes.append("⚠️ " + esc(e_))
    return "\n".join(lignes)


def envoyer_fichier(chemin, legende):
    with open(chemin, "rb") as f:
        api("sendDocument", {"chat_id": CHAT_ID, "caption": legende},
            {"document": (os.path.basename(chemin), f)})


def commande_liste(texte):
    codes, filtres, tri = parser_demande(texte)
    api("sendMessage", {"chat_id": CHAT_ID, "text": "⏳ Je prépare la liste, quelques instants…"})
    projets, erreurs = recuperer_projets(codes)
    projets = trier(filtrer(projets, filtres), tri)
    if not projets:
        api("sendMessage", {"chat_id": CHAT_ID,
                            "text": "Aucun projet ne correspond." + ("".join("\n⚠️ " + x for x in erreurs))})
        return
    api("sendMessage", {"chat_id": CHAT_ID, "parse_mode": "HTML",
                        "text": message_resume(projets, filtres, tri, erreurs),
                        "disable_web_page_preview": "true"})
    chemin = exporter(projets, "projets_06_98000")
    envoyer_fichier(chemin, f"{len(projets)} projets, tableau triable et filtrable")


def commandes_telegram(etat):
    """Répond aux messages /projets et /aide reçus depuis le dernier passage."""
    if not TOKEN:
        return
    reponse = api("getUpdates", {"offset": etat.get("tg_offset", 0), "timeout": 0,
                                 "allowed_updates": json.dumps(["message"])}).json()
    for maj in reponse.get("result", []):
        etat["tg_offset"] = maj["update_id"] + 1
        msg = maj.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
            continue  # on ne répond qu'à vous
        texte = (msg.get("text") or "").strip()
        bas = texte.lower()
        try:
            if bas.startswith("/projets") or bas.startswith("/liste"):
                commande_liste(texte)
            elif bas.startswith("/aide") or bas.startswith("/start"):
                api("sendMessage", {"chat_id": CHAT_ID, "text": AIDE})
        except Exception as exc:
            print("Commande en échec :", texte, exc)
            try:
                api("sendMessage", {"chat_id": CHAT_ID, "text": f"La commande a échoué : {exc}"})
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
def calibrer():
    vus = {}
    for page in (PAGE_NOUVEAUX, PAGE_MODIFS):
        for l in lire(page):
            vus.setdefault(l["icone"], []).append(f"{l['nom']} ({l['cp']})")
    for ic, ex in vus.items():
        print(f"{ic} -> {ICONES.get(ic, '???')} | ex: {ex[:3]}")
    print("Lignes lues (tous départements confondus) :", sum(len(v) for v in vus.values()))


def apercu(url, envoyer_tg=False):
    """Teste la lecture d'une fiche : python bot.py --apercu <url> [--envoyer]"""
    d = lire_fiche(url)
    l = {"url": url, "nom": d["nom"] or "(aperçu)", "ville": d["ville"], "fonction": ""}
    texte = construire_texte("🔎 APERÇU", l, "Projet retenu", d)
    print(re.sub(r"<[^>]+>", "", html.unescape(texte)))
    print("Photos trouvées :", [p[0] for p in d["photos"]])
    print("Lien permis :", d["permis_url"] or "-")
    print("Agence de l'architecte :", d["agence_url"] or "-")
    for rang in construire_boutons(l, d, True):
        for b in rang:
            print("  [%s] %s" % (b["text"], b["url"]))
    if envoyer_tg:
        envoyer(texte, construire_boutons(l, d, True), d["photos"])
        print("Aperçu envoyé sur Telegram.")


def main():
    premier = not os.path.exists(ETAT)
    etat = json.load(open(ETAT)) if not premier else {"nouveaux": [], "modifs": []}
    nouveaux, modifs = set(etat["nouveaux"]), set(etat["modifs"])
    ajoutes = set()
    try:
        commandes_telegram(etat)
    except Exception as exc:  # une commande ne doit jamais bloquer la veille
        print("Commandes Telegram ignorées :", exc)

    for l in lire(PAGE_NOUVEAUX):
        if not code_ok(l["cp"]) or l["url"] in nouveaux:
            continue
        nouveaux.add(l["url"])
        ajoutes.add(l["url"])
        st = statut(l)
        if st and not premier:
            alerte("🆕 NOUVEAU PROJET", l, st, modif=False)

    for l in lire(PAGE_MODIFS):
        cle = f"{l['url']}|{l['date']}"
        if not code_ok(l["cp"]) or cle in modifs:
            continue
        modifs.add(cle)
        st = statut(l)
        if st and not premier and l["url"] not in ajoutes:
            alerte("✏️ PROJET MODIFIÉ", l, st, modif=True)

    # on garde une mémoire bornée
    etat = {"nouveaux": sorted(nouveaux)[-3000:], "modifs": sorted(modifs)[-3000:],
            "tg_offset": etat.get("tg_offset", 0)}
    json.dump(etat, open(ETAT, "w"), indent=0)
    if premier:
        telegram("✅ Bot PSS installé, la surveillance démarre.")


if __name__ == "__main__":
    if "--calibrer" in sys.argv:
        calibrer()
    elif "--liste" in sys.argv:
        commande_liste(" ".join(sys.argv[sys.argv.index("--liste") + 1:]))
    elif "--apercu" in sys.argv:
        apercu(sys.argv[sys.argv.index("--apercu") + 1], "--envoyer" in sys.argv)
    else:
        main()
