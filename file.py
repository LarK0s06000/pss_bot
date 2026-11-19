import os, re, sys, json, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE = "[pss-archi.eu](https://www.pss-archi.eu)"
PAGE_NOUVEAUX = BASE + "/nouveaux_immeubles.html"
PAGE_MODIFS = BASE + "/dernieres_modifications.html"
ETAT = "state.json"

# icône -> statut. ic_projetr = projet retenu (à confirmer au calibrage)
ICONES = {
    "ic_projetr.gif": "Projet retenu",
    # "ic_xxxx.gif": "Projet proposé",
    # "ic_xxxx.gif": "En construction",
    # "ic_xxxx.gif": "En rénovation",
}
IGNORER = set()  # ex. {"ic_construit.gif", "ic_annule.gif"}

EMOJI = {"Projet retenu": "✅", "Projet proposé": "💡",
         "En construction": "🏗️", "En rénovation": "🛠️"}
H = {"User-Agent": "Mozilla/5.0 (veille personnelle)"}
PARIS = ZoneInfo("Europe/Paris")


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
    lignes = []
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        lien = tds[0].find("a", href=re.compile(r"/immeubles/"))
        m = re.search(r"\((\d{5})\)", tds[1].get_text())
        if not lien or not m:
            continue
        img = tr.find("img", src=re.compile(r"ic_"))
        url = lien["href"] if lien["href"].startswith("http") else BASE + lien["href"]
        lignes.append({
            "url": url, "nom": lien.get_text(strip=True),
            "ville": re.sub(r"\s*,\s*France\s*$", "", tds[1].get_text(" ", strip=True)),
            "cp": m.group(1),
            "icone": img["src"].split("/")[-1] if img else None,
            "date": date_iso(tds[-1].get_text()),
        })
    return lignes


def statut(l):
    if l["icone"] in IGNORER:
        return None
    return ICONES.get(l["icone"], f"statut inconnu ({l['icone']})")


def telegram(txt):
    requests.post(f"[api.telegram.org](https://api.telegram.org/bot{TOKEN}/sendMessage)",
                  data={"chat_id": CHAT_ID, "text": txt}, timeout=20).raise_for_status()


def msg(titre, l, st):
    return f"{titre}\n{EMOJI.get(st, '❔')} {st}\n{l['nom']}\n{l['ville']}\n{l['url']}"


def calibrer():
    vus = {}
    for page in (PAGE_NOUVEAUX, PAGE_MODIFS):
        for l in lire(page):
            vus.setdefault(l["icone"], []).append(f"{l['nom']} ({l['cp']})")
    for ic, ex in vus.items():
        print(f"{ic} -> {ICONES.get(ic, '???')} | ex: {ex[:3]}")
    print("Lignes lues (tous départements confondus) :", sum(len(v) for v in vus.values()))


def main():
    premier = not os.path.exists(ETAT)
    etat = json.load(open(ETAT)) if not premier else {"nouveaux": [], "modifs": []}
    nouveaux, modifs = set(etat["nouveaux"]), set(etat["modifs"])
    ajoutes = set()

    for l in lire(PAGE_NOUVEAUX):
        if not code_ok(l["cp"]) or l["url"] in nouveaux:
            continue
        nouveaux.add(l["url"])
        ajoutes.add(l["url"])
        st = statut(l)
        if st and not premier:
            telegram(msg("🆕 NOUVEAU PROJET", l, st))

    for l in lire(PAGE_MODIFS):
        cle = f"{l['url']}|{l['date']}"
        if not code_ok(l["cp"]) or cle in modifs:
            continue
        modifs.add(cle)
        st = statut(l)
        if st and not premier and l["url"] not in ajoutes:
            telegram(msg("✏️ PROJET MODIFIÉ", l, st))

    # on garde une mémoire bornée
    etat = {"nouveaux": sorted(nouveaux)[-3000:], "modifs": sorted(modifs)[-3000:]}
    json.dump(etat, open(ETAT, "w"), indent=0)
    if premier:
        telegram("✅ Bot PSS installé, la surveillance démarre.")


if __name__ == "__main__":
    calibrer() if "--calibrer" in sys.argv else main()
