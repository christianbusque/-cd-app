#!/usr/bin/env python3
"""
CD Repère — rapport du soir.

Lancé chaque soir à 20 h par la tâche automatique (crontab) du Pi.
Lit la base du rack en LECTURE SEULE et envoie un courriel à Christian (v17, 22 oct. 2026) :
  1. les caissons entrés et sortis du rack (jour, semaine, jour par jour)
  2. les caissons emballés
  3. les palettes par projet
  4. les reprises en pi² et en % de perte (jour, semaine), par cause, poste,
     projet et matériau
  5. l'assemblage   6. le nesting   7. les pièces manquantes
  8. les caissons qui traînent   9. le détail des scans

Le courriel passe par la file de courriels Supabase (même chemin que les
rapports de fin de job). Une copie HTML est aussi gardée sur le Pi, dans
donnees/rapports/.

Utilisation :
  python3 rapport_soir.py            envoi normal (saute samedi/dimanche sans activité)
  python3 rapport_soir.py --test     envoie tout de suite, même sans activité
  python3 rapport_soir.py --apercu   n'envoie rien, écrit seulement le fichier HTML
"""

import glob
import html
import json
import re
import os
import sqlite3
import sys
import traceback
import urllib.request
from datetime import datetime, timedelta

ICI = os.path.dirname(os.path.abspath(__file__))

SUPABASE_URL = "https://xgjlsddlbhmvkwyiwmqc.supabase.co"
CLE_PUBLIQUE = "sb_publishable_jdJMKiYpc_01gpmRgc17iA_yLOMqJ1D"
CLE_RAPPORT = "rj15ko8EBC-fZgf7Tq4nLFR1M6F9Bg0q"

# Projets d'essai faits pendant la construction du rack : on ne les rapporte pas.
MOTS_ESSAI = ("REPERE", "TEST", "ESSAI")

# Statuts d'une pièce qui n'a jamais été scannée.
STATUTS_NON_SCANNEE = ("ATTENDUE",)

anomalies = []


# ----------------------------------------------------------------------
# Accès à la base
# ----------------------------------------------------------------------

def trouver_base():
    for chemin in (os.path.join(ICI, "donnees", "tri.db"),
                   "/home/christian/tri/donnees/tri.db"):
        if os.path.exists(chemin):
            return chemin
    trouves = sorted(glob.glob(os.path.join(ICI, "**", "*.db"), recursive=True),
                     key=os.path.getsize, reverse=True)
    if trouves:
        return trouves[0]
    raise SystemExit("Base de données du rack introuvable dans " + ICI)


def ouvrir(chemin):
    # mode=ro : le rapport ne peut rien modifier, même par erreur.
    cx = sqlite3.connect("file:%s?mode=ro" % chemin, uri=True, timeout=15)
    cx.row_factory = sqlite3.Row
    return cx


def colonnes(cx, table):
    try:
        return {r["name"] for r in cx.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.Error:
        return set()


def requete(cx, sql, params=(), defaut=None, quoi=""):
    """Une requête qui échoue ne doit pas empêcher le courriel de partir :
    on note l'anomalie et on continue avec le reste."""
    try:
        return cx.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        anomalies.append("%s : %s" % (quoi or sql.split()[0], e))
        return [] if defaut is None else defaut


def un(cx, sql, params=(), quoi=""):
    lignes = requete(cx, sql, params, quoi=quoi)
    if not lignes:
        return 0
    v = lignes[0][0]
    return v or 0


# ----------------------------------------------------------------------
# Calculs
# ----------------------------------------------------------------------

def est_essai(code, nom):
    t = ("%s %s" % (code or "", nom or "")).upper()
    return any(m in t for m in MOTS_ESSAI)


def nom_projet(p):
    nom = (p["nom"] or "").strip()
    code = (p["code"] or "").strip()
    if nom and code and nom.upper() != code.upper():
        return "%s (%s)" % (nom, code)
    return nom or code


# Les durees sont en HEURES OUVRABLES (lundi au jeudi, 6 h - 16 h 30 sans
# les pauses ; vendredi seulement si l'usine a travaille). Voir
# heures_ouvrables.py.
_HORAIRE = None


def horaire(cx=None):
    global _HORAIRE
    if _HORAIRE is None:
        try:
            sys.path.insert(0, ICI)
            import heures_ouvrables
            cfg = {}
            chemin = os.path.join(ICI, "config_racks.json")
            if os.path.exists(chemin):
                with open(chemin, encoding="utf-8") as f:
                    cfg = json.load(f)
            _HORAIRE = heures_ouvrables.Horaire(cfg, cx)
        except Exception as e:
            anomalies.append("heures ouvrables indisponibles (%s) : durees sur 24 h" % e)
            _HORAIRE = False
    return _HORAIRE


def heures_ouv(a, z, cx=None):
    """Heures ouvrables entre deux datetime (24 h si le module manque)."""
    if not a or not z or z < a:
        return None
    h = horaire(cx)
    if h:
        return h.heures(a, z)
    return (z - a).total_seconds() / 3600


def duree(heures):
    if heures is None:
        return "—"
    if heures < 1:
        return "%d min" % round(heures * 60)
    jour = horaire().heures_par_jour() if horaire() else 24
    if heures < 2 * jour:
        return "%.1f h" % heures
    return "%.1f jours ouvrables" % (heures / jour)


def parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "")[:19])
    except ValueError:
        return None


def stats_periode(cx, debut, fin):
    """Chiffres entre debut (inclus) et fin (exclu), en heure locale du Pi."""
    d, f = debut.isoformat(timespec="seconds"), fin.isoformat(timespec="seconds")
    s = {}

    # Scans par type d'événement (DEPOT, CONVOYEUR, TAMPON, SORTIE, ERREUR...).
    par_type = requete(cx,
        "SELECT type, COUNT(*) n FROM evenement WHERE horodatage >= ? AND horodatage < ? "
        "GROUP BY type ORDER BY n DESC", (d, f), quoi="événements par type")
    s["par_type"] = {r["type"]: r["n"] for r in par_type}

    # Pièces distinctes scannées (une pièce annulée puis rescannée compte une fois).
    s["pieces"] = un(cx,
        "SELECT COUNT(DISTINCT piece_id) FROM evenement "
        "WHERE horodatage >= ? AND horodatage < ? AND piece_id IS NOT NULL "
        "AND type NOT IN ('ANNULATION','SORTIE','ERREUR','INCONNUE','DOUBLE')",
        (d, f), quoi="pièces scannées")
    s["annulations"] = s["par_type"].get("ANNULATION", 0)

    s["caissons_commences"] = un(cx,
        "SELECT COUNT(*) FROM caisson WHERE premier_scan >= ? AND premier_scan < ?",
        (d, f), quoi="caissons commencés")
    s["caissons_completes"] = un(cx,
        "SELECT COUNT(*) FROM caisson WHERE complete_le >= ? AND complete_le < ?",
        (d, f), quoi="caissons complétés")
    sortis = requete(cx,
        "SELECT premier_scan, complete_le, sorti_le FROM caisson "
        "WHERE sorti_le >= ? AND sorti_le < ?", (d, f), quoi="caissons sortis")
    s["caissons_sortis"] = len(sortis)

    sejours, attentes = [], []
    for r in sortis:
        a, c, z = parse(r["premier_scan"]), parse(r["complete_le"]), parse(r["sorti_le"])
        if a and z and z >= a:
            sejours.append(heures_ouv(a, z, cx))
        if c and z and z >= c:
            attentes.append(heures_ouv(c, z, cx))
    s["sejour_moyen"] = sum(sejours) / len(sejours) if sejours else None
    s["attente_assemblage"] = sum(attentes) / len(attentes) if attentes else None

    erreurs = {t: n for t, n in s["par_type"].items()
               if t not in ("DEPOT", "CONVOYEUR", "SORTIE", "ANNULATION", "TAMPON",
                            "RETOUR_TAMPON", "IMPORT")}
    s["erreurs"] = erreurs
    return s


def projets_suivis(cx):
    """Projets dont au moins une pièce a été scannée (en cours ou passés)."""
    cols_p = colonnes(cx, "piece")
    if "statut" not in cols_p:
        anomalies.append("table piece sans colonne statut")
        return []
    marques = ",".join("'%s'" % x for x in STATUTS_NON_SCANNEE)
    projets = requete(cx,
        "SELECT p.id, p.code, p.nom, "
        "  COUNT(pc.id) AS prevues, "
        "  SUM(CASE WHEN pc.statut NOT IN (%s) THEN 1 ELSE 0 END) AS scannees, "
        "  MAX(pc.depose_le) AS dernier_scan "
        "FROM projet p JOIN caisson k ON k.projet_id = p.id "
        "JOIN piece pc ON pc.caisson_id = k.id "
        "GROUP BY p.id HAVING scannees > 0 "
        "ORDER BY dernier_scan DESC" % marques, quoi="projets suivis")
    return [p for p in projets if not est_essai(p["code"], p["nom"])]


def manquants_du_projet(cx, projet_id):
    marques = ",".join("'%s'" % x for x in STATUTS_NON_SCANNEE)
    caissons = requete(cx,
        "SELECT k.id, k.code, k.statut, k.premier_scan, "
        "  COUNT(pc.id) AS prevues, "
        "  SUM(CASE WHEN pc.statut NOT IN (%s) THEN 1 ELSE 0 END) AS scannees "
        "FROM caisson k JOIN piece pc ON pc.caisson_id = k.id "
        "WHERE k.projet_id = ? GROUP BY k.id ORDER BY k.code" % marques,
        (projet_id,), quoi="caissons du projet")
    resultat = []
    for k in caissons:
        if k["scannees"] >= k["prevues"]:
            continue
        pieces = requete(cx,
            "SELECT COALESCE(NULLIF(description,''), uid) AS nom FROM piece "
            "WHERE caisson_id = ? AND statut IN (%s) ORDER BY description" % marques,
            (k["id"],), quoi="pièces manquantes")
        resultat.append({
            "code": k["code"], "statut": k["statut"], "premier_scan": k["premier_scan"],
            "prevues": k["prevues"], "scannees": k["scannees"],
            "manquantes": [p["nom"] for p in pieces],
        })
    return resultat


def caissons_qui_trainent(cx, maintenant):
    """Caissons dans le rack depuis plus d'une journee de travail (9,5 h
    ouvrables), pas 24 h de calendrier : un caisson commence jeudi a 15 h
    ne « traine » pas lundi matin."""
    lignes = requete(cx,
        "SELECT k.code, k.statut, k.premier_scan, k.complete_le, p.code AS pcode, p.nom AS pnom "
        "FROM caisson k JOIN projet p ON p.id = k.projet_id "
        "WHERE k.statut NOT IN ('SORTI','ATTENDU') AND k.premier_scan IS NOT NULL "
        "ORDER BY k.premier_scan", quoi="caissons qui traînent")
    seuil = horaire(cx).heures_par_jour() if horaire(cx) else 24
    out = []
    for r in lignes:
        if est_essai(r["pcode"], r["pnom"]):
            continue
        debut = parse(r["premier_scan"])
        h = heures_ouv(debut, maintenant, cx) if debut else None
        if h is None or h < seuil:
            continue
        out.append({"code": r["code"], "statut": r["statut"], "heures": h,
                    "projet": nom_projet({"code": r["pcode"], "nom": r["pnom"]})})
    return out


# ----------------------------------------------------------------------
# Mise en page
# ----------------------------------------------------------------------

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]


def date_longue(d):
    return "%s %d %s %d" % (JOURS[d.weekday()], d.day, MOIS[d.month - 1], d.year)


def e(x):
    return html.escape(str(x if x is not None else ""))


def tableau_stats(titre, s):
    lignes = [
        ("Pièces scannées au poste du rack", s["pieces"]),
        ("Caissons commencés", s["caissons_commences"]),
        ("Caissons complétés", s["caissons_completes"]),
        ("Caissons sortis vers l'assemblage", s["caissons_sortis"]),
        ("Temps moyen dans le rack (1er scan → sortie)", duree(s["sejour_moyen"])),
        ("Attente moyenne avant l'assemblage (complet → sortie)", duree(s["attente_assemblage"])),
        ("Passages au convoyeur", s["par_type"].get("CONVOYEUR", 0)),
        ("Passages au tampon", sum(v for k, v in s["par_type"].items() if "TAMPON" in k)),
        ("Scans annulés", s["annulations"]),
    ]
    h = ["<h3>%s</h3>" % e(titre),
         "<table cellpadding='4' style='border-collapse:collapse'>"]
    for lib, val in lignes:
        h.append("<tr><td style='border-bottom:1px solid #ddd'>%s</td>"
                 "<td style='border-bottom:1px solid #ddd;text-align:right'><b>%s</b></td></tr>"
                 % (e(lib), e(val)))
    if s["erreurs"]:
        det = ", ".join("%s : %d" % (k, v) for k, v in sorted(s["erreurs"].items()))
        h.append("<tr><td>Erreurs et alertes</td><td style='text-align:right'><b>%s</b></td></tr>"
                 % e(det))
    h.append("</table>")
    return "\n".join(h)


def texte_stats(titre, s):
    t = [titre,
         "  Pièces scannées : %s" % s["pieces"],
         "  Caissons commencés : %s · complétés : %s · sortis : %s"
         % (s["caissons_commences"], s["caissons_completes"], s["caissons_sortis"]),
         "  Temps moyen dans le rack : %s · attente avant assemblage : %s"
         % (duree(s["sejour_moyen"]), duree(s["attente_assemblage"])),
         "  Scans annulés : %s" % s["annulations"]]
    if s["erreurs"]:
        t.append("  Erreurs : " + ", ".join("%s %d" % kv for kv in sorted(s["erreurs"].items())))
    return "\n".join(t)


CAUSES_REPRISE = {"CNC": "CNC", "PLAQUEUSE": "Plaqueuse", "VITAP": "Vitap",
                  "MANUTENTION": "Manutention", "MATERIAU": "Défaut matériau",
                  "AUTRE": "Autre"}


def reprises(cx, aujourdhui, lundi, demain, maintenant):
    """Les reprises : declarees aujourd'hui (par cause), cette semaine, et
    celles qui ne sont pas encore revenues. None si la fonction n'existe
    pas encore dans la base."""
    if not requete(cx, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reprise'",
                   quoi="table des reprises"):
        return None
    a, l, d = (x.isoformat(timespec="seconds") for x in (aujourdhui, lundi, demain))
    base_sql = "FROM reprise WHERE annulee_le IS NULL AND declaree_le >= ? AND declaree_le < ?"
    r = {
        "jour": un(cx, "SELECT COUNT(*) " + base_sql, (a, d), quoi="reprises du jour"),
        "semaine": un(cx, "SELECT COUNT(*) " + base_sql, (l, d), quoi="reprises de la semaine"),
        "revenues_jour": un(cx, "SELECT COUNT(*) FROM reprise WHERE annulee_le IS NULL "
                            "AND recue_le >= ? AND recue_le < ?", (a, d),
                            quoi="reprises revenues"),
        "causes_jour": [(CAUSES_REPRISE.get(x["cause"], x["cause"]), x["n"]) for x in requete(
            cx, "SELECT cause, COUNT(*) n " + base_sql + " GROUP BY cause ORDER BY n DESC",
            (a, d), quoi="reprises par cause")],
        "causes_semaine": [(CAUSES_REPRISE.get(x["cause"], x["cause"]), x["n"]) for x in requete(
            cx, "SELECT cause, COUNT(*) n " + base_sql + " GROUP BY cause ORDER BY n DESC",
            (l, d), quoi="reprises par cause (semaine)")],
        "en_attente": [],
    }
    for x in requete(cx, """
            SELECT r.prioritaire, r.declaree_le, r.cause, k.code AS caisson,
                   j.code AS bon, j.nom AS projet, COALESCE(p.description, p.uid) AS piece
            FROM reprise r JOIN piece p ON p.id = r.piece_id
            JOIN caisson k ON k.id = p.caisson_id JOIN projet j ON j.id = k.projet_id
            WHERE r.recue_le IS NULL AND r.annulee_le IS NULL
            ORDER BY r.prioritaire DESC, r.declaree_le""", quoi="reprises en attente"):
        if est_essai(x["bon"], x["projet"]):
            continue
        quand = parse(x["declaree_le"])
        h = heures_ouv(quand, maintenant, cx) if quand else None
        r["en_attente"].append(dict(x, heures=h))
    return r


def nom_nesting(projet, bon, batch=None):
    lot = (" · lot %s" % batch) if batch else ""
    if projet and bon:
        nom = re.sub(r"^\S+_CD\d+\s+", "", projet)
        return "%s — %s%s" % (nom or projet, bon, lot)
    return (projet or bon or "Reprise") + lot


def nesting_du_jour(cx, aujourdhui, demain):
    """CD-Nesting : ce qui s'est coupe aujourd'hui a la CNC. None si CD-Nesting
    n'est pas encore installe (table absente)."""
    if not un(cx, "SELECT COUNT(*) FROM sqlite_master WHERE name = 'nesting_feuille'",
              quoi="table nesting"):
        return None
    d, f = aujourdhui.isoformat(timespec="seconds"), demain.isoformat(timespec="seconds")
    r = {"coupees": un(cx,
            "SELECT COALESCE(SUM(fe.qty), 0) FROM nesting_feuille fe JOIN nesting n "
            "ON n.id = fe.nesting_id WHERE n.type = 'PRODUCTION' AND fe.coupee_le >= ? "
            "AND fe.coupee_le < ?", (d, f), quoi="feuilles coupees") or 0,
         "par_bon": [], "en_cours": [], "termines": [], "problemes": [],
         "reprises": [], "perdues": [], "nb_perdues": 0}
    for x in requete(cx,
            "SELECT n.bon, n.projet, n.batch, SUM(fe.qty) AS nb FROM nesting_feuille fe "
            "JOIN nesting n ON n.id = fe.nesting_id WHERE n.type = 'PRODUCTION' "
            "AND fe.coupee_le >= ? AND fe.coupee_le < ? GROUP BY n.id ORDER BY nb DESC",
            (d, f), quoi="coupes par bon"):
        r["par_bon"].append((nom_nesting(x["projet"], x["bon"], x["batch"]), x["nb"]))
    for x in requete(cx,
            "SELECT n.id, n.bon, n.projet, n.batch, n.termine_le, COUNT(fe.id) AS total, "
            "SUM(fe.coupee_le IS NOT NULL) AS coupees, MIN(fe.coupee_le) AS debut, "
            "MAX(fe.coupee_le) AS fin FROM nesting n JOIN nesting_feuille fe "
            "ON fe.nesting_id = n.id WHERE n.type = 'PRODUCTION' AND n.retire_le IS NULL "
            "AND (n.termine_le IS NULL OR (n.termine_le >= ? AND n.termine_le < ?)) "
            "GROUP BY n.id ORDER BY n.ordre, n.id", (d, f), quoi="nestings en cours"):
        nom = nom_nesting(x["projet"], x["bon"], x["batch"])
        if x["termine_le"]:
            h = heures_ouv(parse(x["debut"]), parse(x["fin"]), cx)
            r["termines"].append((nom, x["total"], h))
        else:
            r["en_cours"].append((nom, x["coupees"] or 0, x["total"]))
    for x in requete(cx,
            "SELECT pb.type, pb.note, pb.signale_le, pb.regle_le, n.bon, n.projet "
            "FROM nesting_probleme pb JOIN nesting n ON n.id = pb.nesting_id "
            "WHERE pb.regle_le IS NULL OR (pb.signale_le >= ? AND pb.signale_le < ?) "
            "ORDER BY pb.signale_le", (d, f), quoi="problemes nesting"):
        libelle = {"MATERIEL": "Manque de matériel", "ERRONE": "Nesting erroné",
                   "MACHINE": "Bris machine"}.get(x["type"], "Autre")
        r["problemes"].append("%s — %s%s%s" % (
            libelle, nom_nesting(x["projet"], x["bon"]),
            (" — « %s »" % x["note"]) if x["note"] else "",
            " (réglé)" if x["regle_le"] else " (ouvert)"))
    r["reprises"] = [x["nom_original"] or "Reprise" for x in requete(cx,
            "SELECT nom_original FROM nesting WHERE type = 'REPRISE' AND retire_le IS NULL "
            "AND termine_le IS NULL ORDER BY depose_le", quoi="reprises au nesting")]
    perdues = requete(cx,
            "SELECT k.code AS caisson, COALESCE(p.description, p.uid) AS piece, "
            "fe.numero, n.bon, n.projet FROM nesting_piece np "
            "JOIN nesting_feuille fe ON fe.id = np.feuille_id "
            "JOIN nesting n ON n.id = fe.nesting_id JOIN piece p ON p.id = np.piece_id "
            "JOIN caisson k ON k.id = p.caisson_id WHERE n.type = 'PRODUCTION' "
            "AND n.retire_le IS NULL AND fe.coupee_le < ? AND p.statut = 'ATTENDUE' "
            "AND p.destination = 'RACK' ORDER BY n.bon, k.code", (d,),
            quoi="pieces coupees jamais arrivees")
    r["nb_perdues"] = len(perdues)
    r["perdues"] = ["%s — %s — feuille %d (%s)" % (x["caisson"], x["piece"], x["numero"],
                                                  nom_nesting(x["projet"], x["bon"]))
                    for x in perdues[:25]]
    return r


def assemblage_du_jour(cx, aujourdhui, lundi, demain):
    """v16 : caissons assembles au poste CD Assemblage."""
    if "assemble_le" not in colonnes(cx, "caisson"):
        return None
    a, d, l = aujourdhui.isoformat(), demain.isoformat(), lundi.isoformat()
    jour = requete(cx, "SELECT k.code, j.code AS bon, j.nom, k.sorti_le, k.assemble_le, "
                       "k.assemble_force FROM caisson k JOIN projet j ON j.id = k.projet_id "
                       "WHERE k.assemble_le >= ? AND k.assemble_le < ? ORDER BY k.assemble_le",
                   (a, d), quoi="assemblage du jour")
    semaine = un(cx, "SELECT COUNT(*) FROM caisson WHERE assemble_le >= ? AND assemble_le < ?",
                 (l, d), quoi="assemblage de la semaine")
    delais = [heures_ouv(parse(r["sorti_le"]), parse(r["assemble_le"]), cx)
              for r in jour if r["sorti_le"]]
    delais = [x for x in delais if x is not None]
    par_bon = {}
    for r in jour:
        cle = nom_projet({"nom": r["nom"], "code": r["bon"]})
        par_bon[cle] = par_bon.get(cle, 0) + 1
    return {"jour": len(jour), "semaine": semaine,
            "incomplets": sum(1 for r in jour if r["assemble_force"]),
            "delai_moyen": sum(delais) / len(delais) if delais else None,
            "par_bon": sorted(par_bon.items())}


def projet_court(nom):
    """« 70_CD25289 LA BECQUÉE » -> « LA BECQUÉE »."""
    nom = (nom or "").strip()
    return re.sub(r"^\S+_CD\d+\s+", "", nom) or nom


def emballage(cx, debut, fin):
    """v17 : caissons emballés entre debut et fin, total et par projet."""
    if "emballe_le" not in colonnes(cx, "caisson"):
        return None
    d, f = debut.isoformat(timespec="seconds"), fin.isoformat(timespec="seconds")
    par = requete(cx,
        "SELECT j.nom, j.code, COUNT(*) n FROM caisson k JOIN projet j ON j.id = k.projet_id "
        "WHERE k.emballe_le >= ? AND k.emballe_le < ? GROUP BY j.id ORDER BY j.nom, j.code",
        (d, f), quoi="caissons emballés")
    par = [x for x in par if not est_essai(x["code"], x["nom"])]
    return {"total": sum(x["n"] for x in par),
            "par_projet": [(nom_projet({"nom": projet_court(x["nom"]), "code": x["code"]}), x["n"])
                           for x in par]}


def palettes_par_projet(cx, lundi, demain):
    """v17 : palettes de la semaine (et toutes celles encore ouvertes), par projet."""
    if not un(cx, "SELECT COUNT(*) FROM sqlite_master WHERE name = 'palette'", quoi="table palette"):
        return None
    l, d = lundi.isoformat(timespec="seconds"), demain.isoformat(timespec="seconds")
    lignes = requete(cx,
        "SELECT p.id, p.bon, p.nom_projet, p.numero, p.ouverte_le, p.fermee_le, "
        "  (SELECT COUNT(*) FROM caisson k WHERE k.palette_id = p.id) AS caissons "
        "FROM palette p WHERE p.fermee_le IS NULL OR (p.fermee_le >= ? AND p.fermee_le < ?) "
        "   OR (p.ouverte_le >= ? AND p.ouverte_le < ?) "
        "ORDER BY p.nom_projet, p.bon, p.numero", (l, d, l, d), quoi="palettes")
    projets = {}
    for x in lignes:
        if est_essai(x["bon"], x["nom_projet"]):
            continue
        pr = projets.setdefault(projet_court(x["nom_projet"]) or x["bon"], {})
        b = pr.setdefault(x["bon"], {"fermees": 0, "ouvertes": 0, "caissons": 0, "numeros": []})
        if x["fermee_le"]:
            b["fermees"] += 1
        else:
            b["ouvertes"] += 1
        b["caissons"] += x["caissons"] or 0
        b["numeros"].append("P%d%s" % (x["numero"], "" if x["fermee_le"] else " (ouverte)"))
    return projets


POSTES_REPRISE = {"ENTREE": "Rack (entrée)", "SORTIE": "Rack (sortie)", "CNC": "CNC",
                  "ASSEMBLAGE": "Assemblage", "EMBALLAGE": "Emballage", "PALETTE": "Palettes",
                  "PLAQUEUSE": "Plaqueuse", "VITAP": "Vitap"}


def pi2(longueur, largeur):
    """Superficie d'une pièce en pi² (dimensions en pouces)."""
    try:
        if longueur and largeur and longueur > 0 and largeur > 0:
            return float(longueur) * float(largeur) / 144.0
    except (TypeError, ValueError):
        pass
    return None


def perte_reprises(cx, debut, fin):
    """v17 : reprises en pi² et en % de ce qui a été produit au rack.
    % = pi² des pièces à refaire ÷ pi² des pièces scannées au rack, même période."""
    if not requete(cx, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reprise'",
                   quoi="table des reprises"):
        return None
    d, f = debut.isoformat(timespec="seconds"), fin.isoformat(timespec="seconds")
    produit, sans_dim_prod = 0.0, 0
    for x in requete(cx,
            "SELECT p.longueur, p.largeur, j.code, j.nom FROM piece p "
            "JOIN caisson k ON k.id = p.caisson_id JOIN projet j ON j.id = k.projet_id "
            "WHERE p.depose_le >= ? AND p.depose_le < ?", (d, f), quoi="pi² produits"):
        if est_essai(x["code"], x["nom"]):
            continue
        a = pi2(x["longueur"], x["largeur"])
        if a is None:
            sans_dim_prod += 1
        else:
            produit += a
    r = {"nb": 0, "pi2": 0.0, "sans_dim": 0, "produit": produit, "sans_dim_prod": sans_dim_prod,
         "cause": {}, "poste": {}, "projet": {}, "materiau": {}}
    for x in requete(cx,
            "SELECT r.cause, r.poste, p.longueur, p.largeur, p.materiau, j.code, j.nom "
            "FROM reprise r JOIN piece p ON p.id = r.piece_id "
            "JOIN caisson k ON k.id = p.caisson_id JOIN projet j ON j.id = k.projet_id "
            "WHERE r.annulee_le IS NULL AND r.declaree_le >= ? AND r.declaree_le < ?",
            (d, f), quoi="pi² refaits"):
        if est_essai(x["code"], x["nom"]):
            continue
        a = pi2(x["longueur"], x["largeur"])
        r["nb"] += 1
        if a is None:
            r["sans_dim"] += 1
            a = 0.0
        r["pi2"] += a
        cles = {"cause": CAUSES_REPRISE.get(x["cause"], x["cause"] or "Autre"),
                "poste": POSTES_REPRISE.get((x["poste"] or "").upper(), x["poste"] or "?"),
                "projet": projet_court(x["nom"]) or x["code"],
                "materiau": (x["materiau"] or "Matériau inconnu").strip()}
        for dim, cle in cles.items():
            c = r[dim].setdefault(cle, [0, 0.0])
            c[0] += 1
            c[1] += a
    r["pct"] = (r["pi2"] / produit * 100.0) if produit > 0 else None
    return r


def fmt_pi2(x):
    return ("%.1f" % x).replace(".", ",") + " pi²"


def fmt_pct(x):
    return "—" if x is None else ("%.1f" % x).replace(".", ",") + " %"


def html_perte(titre, p):
    h = ["<p><b>%s</b> : <b>%d</b> pièce(s) à refaire · <b>%s</b> refaits sur %s produits au rack "
         "· perte <b>%s</b></p>" % (e(titre), p["nb"], e(fmt_pi2(p["pi2"])), e(fmt_pi2(p["produit"])),
                                    e(fmt_pct(p["pct"])))]
    if p["nb"]:
        h.append("<table cellpadding='4' style='border-collapse:collapse'>"
                 "<tr style='background:#f0f0f0'><th align='left'>Par</th><th align='left'>Détail</th>"
                 "<th>Pièces</th><th>pi²</th><th>% de la perte</th></tr>")
        for dim, lib in (("cause", "Cause"), ("poste", "Poste"), ("projet", "Projet"),
                         ("materiau", "Matériau")):
            for i, (cle, (n, a)) in enumerate(sorted(p[dim].items(), key=lambda kv: -kv[1][1])):
                part = (a / p["pi2"] * 100.0) if p["pi2"] > 0 else None
                h.append("<tr><td style='border-top:%s'>%s</td><td>%s</td><td align='center'>%d</td>"
                         "<td align='right'>%s</td><td align='right'>%s</td></tr>"
                         % ("1px solid #bbb" if i == 0 else "0", e(lib) if i == 0 else "", e(cle), n,
                            e(fmt_pi2(a)), e(fmt_pct(part))))
        h.append("</table>")
    if p["sans_dim"]:
        h.append("<p style='color:#666;font-size:12px'>%d reprise(s) sans dimensions : comptées "
                 "dans le nombre, pas dans les pi².</p>" % p["sans_dim"])
    return "\n".join(h)


def texte_perte(titre, p):
    t = ["  %s : %d pièce(s), %s refaits sur %s produits au rack, perte %s"
         % (titre, p["nb"], fmt_pi2(p["pi2"]), fmt_pi2(p["produit"]), fmt_pct(p["pct"]))]
    for dim, lib in (("cause", "cause"), ("poste", "poste"), ("projet", "projet"),
                     ("materiau", "matériau")):
        if p[dim]:
            t.append("    Par %s : %s" % (lib, ", ".join(
                "%s %s (%d)" % (cle, fmt_pi2(a), n)
                for cle, (n, a) in sorted(p[dim].items(), key=lambda kv: -kv[1][1]))))
    return t


def construire(cx, maintenant):
    horaire(cx)        # heures ouvrables, avec les vendredis travailles
    aujourdhui = maintenant.replace(hour=0, minute=0, second=0, microsecond=0)
    lundi = aujourdhui - timedelta(days=aujourdhui.weekday())
    demain = aujourdhui + timedelta(days=1)

    jour = stats_periode(cx, aujourdhui, demain)
    semaine = stats_periode(cx, lundi, demain)
    par_jour = []
    d = lundi
    while d <= aujourdhui:
        sj = stats_periode(cx, d, d + timedelta(days=1))
        par_jour.append((d, sj))
        d += timedelta(days=1)

    projets = projets_suivis(cx)
    details = []
    total_manquantes = 0
    for p in projets:
        m = manquants_du_projet(cx, p["id"])
        n = sum(len(k["manquantes"]) for k in m)
        total_manquantes += n
        details.append((p, m, n))
    trainent = caissons_qui_trainent(cx, maintenant)
    rep_ = reprises(cx, aujourdhui, lundi, demain, maintenant)
    nest = nesting_du_jour(cx, aujourdhui, demain)
    asm = assemblage_du_jour(cx, aujourdhui, lundi, demain)
    emb_j = emballage(cx, aujourdhui, demain)
    emb_s = emballage(cx, lundi, demain)
    pal = palettes_par_projet(cx, lundi, demain)
    perte_j = perte_reprises(cx, aujourdhui, demain)
    perte_s = perte_reprises(cx, lundi, demain)

    sujet = ("CD Repère — Rapport du %s — %d pièces, %d caissons sortis, %d pièces manquantes"
             % (date_longue(aujourdhui), jour["pieces"], jour["caissons_sortis"],
                total_manquantes))
    if nest is not None:
        sujet += ", %d feuilles coupées" % nest["coupees"]
    if perte_j is not None and perte_j["pct"] is not None:
        sujet += ", reprises %s" % fmt_pct(perte_j["pct"])

    H = ["<div style='font-family:Arial,sans-serif;font-size:14px;color:#222'>",
         "<h2>CD Repère — rapport du %s</h2>" % e(date_longue(aujourdhui)),
         "<p>Généré automatiquement par le rack à %s.</p>" % maintenant.strftime("%H h %M")]

    # 1. Caissons entrés et sortis du rack
    H.append("<h3>1. Caissons dans le rack</h3>")
    for lib, s in (("Aujourd'hui", jour), ("Semaine (depuis le %s)" % date_longue(lundi), semaine)):
        H.append("<p><b>%s</b> — entrés dans le rack : <b>%d</b> · sortis du rack : <b>%d</b> "
                 "(complétés : %d) · temps moyen dans le rack : %s</p>"
                 % (e(lib), s["caissons_commences"], s["caissons_sortis"], s["caissons_completes"],
                    e(duree(s["sejour_moyen"]))))
    H.append("<table cellpadding='4' style='border-collapse:collapse;margin-top:6px'>"
             "<tr style='background:#f0f0f0'><th align='left'>Jour</th><th>Entrés</th>"
             "<th>Sortis</th><th>Complétés</th><th>Pièces</th><th>Temps moyen</th></tr>")
    for d, sj in par_jour:
        H.append("<tr><td>%s</td><td align='center'>%s</td><td align='center'>%s</td>"
                 "<td align='center'>%s</td><td align='center'>%s</td><td align='center'>%s</td></tr>"
                 % (e(JOURS[d.weekday()].capitalize() + " " + str(d.day)), sj["caissons_commences"],
                    sj["caissons_sortis"], sj["caissons_completes"], sj["pieces"],
                    duree(sj["sejour_moyen"])))
    H.append("</table>")

    # 2. Caissons emballés
    if emb_j is not None:
        H.append("<h3>2. Caissons emballés</h3>")
        H.append("<p>Aujourd'hui : <b>%d</b>%s</p>" % (emb_j["total"], (" — " + ", ".join(
            "%s : %d" % (e(n), k) for n, k in emb_j["par_projet"])) if emb_j["par_projet"] else ""))
        H.append("<p>Semaine : <b>%d</b>%s</p>" % (emb_s["total"], (" — " + ", ".join(
            "%s : %d" % (e(n), k) for n, k in emb_s["par_projet"])) if emb_s["par_projet"] else ""))

    # 3. Palettes par projet
    if pal is not None:
        H.append("<h3>3. Palettes par projet</h3>")
        if not pal:
            H.append("<p>Aucune palette cette semaine.</p>")
        for projet in sorted(pal):
            bons = pal[projet]
            tf = sum(b["fermees"] for b in bons.values())
            to = sum(b["ouvertes"] for b in bons.values())
            tc = sum(b["caissons"] for b in bons.values())
            H.append("<p><b>%s</b> — %d palette(s) fermée(s), %d ouverte(s), %d caisson(s) sur palette</p><ul>"
                     % (e(projet), tf, to, tc))
            for bon in sorted(bons):
                b = bons[bon]
                H.append("<li>%s : %s — %d caisson(s)</li>" % (e(bon), e(", ".join(b["numeros"])),
                                                              b["caissons"]))
            H.append("</ul>")

    # 4. Reprises : perte en pi²
    if perte_j is not None:
        H.append("<h3>4. Reprises — perte de matériel</h3>")
        H.append(html_perte("Aujourd'hui", perte_j))
        H.append(html_perte("Semaine", perte_s))
        H.append("<p style='color:#666;font-size:12px'>Perte = pi² des pièces à refaire ÷ pi² des "
                 "pièces scannées au rack pendant la même période.</p>")
        if rep_ is not None:
            H.append("<p>Revenues aujourd'hui : %d.</p>" % rep_["revenues_jour"])
            if rep_["en_attente"]:
                H.append("<p>Pas encore revenues : %d</p><ul>" % len(rep_["en_attente"]))
                for x in rep_["en_attente"]:
                    H.append("<li>%s<b>%s</b> — %s — %s — depuis %s</li>"
                             % ("<span style='color:#C62828'>PRIORITAIRE</span> "
                                if x["prioritaire"] else "", e(x["caisson"]), e(x["piece"]),
                                e(projet_court(x["projet"]) or x["bon"]), e(duree(x["heures"]))))
                H.append("</ul>")
            else:
                H.append("<p>Aucune reprise en attente.</p>")

    # 5. Assemblage
    if asm is not None:
        H.append("<h3>5. Assemblage (CD Assemblage)</h3>")
        H.append("<p>Aujourd'hui : <b>%d caisson(s) assemblé(s)</b>%s. Semaine : <b>%d</b>.</p>"
                 % (asm["jour"], (" — dont %d incomplet(s) au moment de l'assemblage"
                                  % asm["incomplets"]) if asm["incomplets"] else "",
                    asm["semaine"]))
        if asm["par_bon"]:
            H.append("<ul>%s</ul>" % "".join("<li>%s : %d</li>" % (e(n), k)
                                             for n, k in asm["par_bon"]))
        if asm["delai_moyen"] is not None:
            H.append("<p>Délai moyen entre la sortie du rack et l'assemblage : %s.</p>"
                     % e(duree(asm["delai_moyen"])))

    # 6. Nesting (CNC)
    if nest is not None:
        H.append("<h3>6. Nesting (CNC)</h3>")
        H.append("<p>Aujourd'hui : <b>%d feuille(s) coupée(s)</b>%s.</p>" % (
            nest["coupees"], (" — " + ", ".join("%s : %d" % (e(n), k) for n, k in nest["par_bon"]))
            if nest["par_bon"] else ""))
        if nest["termines"]:
            H.append("<p>Terminés aujourd'hui :</p><ul>%s</ul>" % "".join(
                "<li><b>%s</b> — %d feuilles — temps de coupe %s</li>" % (e(n), t, e(duree(h)))
                for n, t, h in nest["termines"]))
        if nest["en_cours"]:
            H.append("<p>Incomplets :</p><ul>%s</ul>" % "".join(
                "<li><b>%s</b> — %d/%d feuilles coupées, <b>%d restante(s)</b></li>"
                % (e(n), c, t, t - c) for n, c, t in nest["en_cours"]))
        if nest["problemes"]:
            H.append("<p style='color:#C62828'>Problèmes signalés :</p><ul>%s</ul>" % "".join(
                "<li>%s</li>" % e(x) for x in nest["problemes"]))
        if nest["reprises"]:
            H.append("<p>Reprises au nesting pas encore coupées : %s</p>"
                     % e(", ".join(nest["reprises"])))
        if nest["nb_perdues"]:
            H.append("<p style='color:#C62828'>Pièces coupées avant aujourd'hui mais jamais "
                     "arrivées au rack : <b>%d</b></p><ul>%s</ul>" % (
                         nest["nb_perdues"], "".join("<li>%s</li>" % e(x) for x in nest["perdues"])))

    # 7. Pièces manquantes
    H.append("<h3>7. Pièces manquantes par projet</h3>")
    if not details:
        H.append("<p>Aucun projet n'a encore de pièce scannée.</p>")
    for p, m, n in details:
        etat = "toutes les pièces scannées" if n == 0 else "%d pièce(s) manquante(s)" % n
        H.append("<p><b>%s</b> — %d/%d pièces scannées — %s</p>"
                 % (e(nom_projet(p)), p["scannees"], p["prevues"], e(etat)))
        if m:
            H.append("<ul>")
            for k in m:
                if k["scannees"] == 0:
                    H.append("<li><b>%s</b> : aucune pièce scannée (%d prévues) — "
                             "pas encore produit ou étiquettes non scannées</li>"
                             % (e(k["code"]), k["prevues"]))
                else:
                    H.append("<li><b>%s</b> (%s, %d/%d) — manque : %s</li>"
                             % (e(k["code"]), e(k["statut"]), k["scannees"], k["prevues"],
                                e(", ".join(k["manquantes"]))))
            H.append("</ul>")
    H.append("<p style='color:#666;font-size:12px'>Une pièce « manquante » n'a jamais été "
             "scannée au poste du rack : elle peut être perdue, pas encore produite, ou "
             "passée sans être scannée.</p>")

    # 8. Caissons qui traînent
    H.append("<h3>8. Caissons dans le rack depuis plus d'une journée de travail</h3>")
    if not trainent:
        H.append("<p>Aucun.</p>")
    else:
        H.append("<ul>")
        for t in trainent:
            H.append("<li><b>%s</b> — %s — %s — depuis %s</li>"
                     % (e(t["code"]), e(t["projet"]), e(t["statut"]), e(duree(t["heures"]))))
        H.append("</ul>")

    # 9. Détail des scans (anciennes sections 1 et 2)
    H.append(tableau_stats("9. Détail des scans — aujourd'hui", jour))
    H.append(tableau_stats("Détail des scans — semaine en cours", semaine))

    if anomalies:
        H.append("<h3>Anomalies du rapport</h3><ul>%s</ul>"
                 % "".join("<li>%s</li>" % e(a) for a in anomalies))
    H.append("</div>")

    # Version texte (lue par le /morning et les clients sans HTML)
    T = ["CD Repère — rapport du %s" % date_longue(aujourdhui), "", "CAISSONS DANS LE RACK"]
    for lib, s in (("Aujourd'hui", jour), ("Semaine", semaine)):
        T.append("  %s : %d entrés, %d sortis (%d complétés), temps moyen %s"
                 % (lib, s["caissons_commences"], s["caissons_sortis"], s["caissons_completes"],
                    duree(s["sejour_moyen"])))
    for d, sj in par_jour:
        T.append("    %s %d : %s entrés, %s sortis, %s complétés, %s pièces"
                 % (JOURS[d.weekday()], d.day, sj["caissons_commences"], sj["caissons_sortis"],
                    sj["caissons_completes"], sj["pieces"]))
    if emb_j is not None:
        T += ["", "CAISSONS EMBALLÉS",
              "  Aujourd'hui : %d%s" % (emb_j["total"], (" — " + ", ".join("%s : %d" % x for x in emb_j["par_projet"])) if emb_j["par_projet"] else ""),
              "  Semaine : %d%s" % (emb_s["total"], (" — " + ", ".join("%s : %d" % x for x in emb_s["par_projet"])) if emb_s["par_projet"] else "")]
    if pal is not None:
        T += ["", "PALETTES PAR PROJET"]
        for projet in sorted(pal):
            for bon, b in sorted(pal[projet].items()):
                T.append("  %s — %s : %s — %d caisson(s)" % (projet, bon, ", ".join(b["numeros"]),
                                                             b["caissons"]))
        if not pal:
            T.append("  Aucune palette cette semaine.")
    if perte_j is not None:
        T += ["", "REPRISES — PERTE DE MATÉRIEL"]
        T += texte_perte("Aujourd'hui", perte_j)
        T += texte_perte("Semaine", perte_s)
        if rep_ is not None:
            T += ["  - %s%s — %s — depuis %s" % ("PRIORITAIRE " if x["prioritaire"] else "",
                                                 x["caisson"], x["piece"], duree(x["heures"]))
                  for x in rep_["en_attente"]] or ["  Aucune reprise en attente."]
    if asm is not None:
        T += ["", "ASSEMBLAGE",
              "  Aujourd'hui : %d caisson(s) assemblé(s)%s — semaine : %d"
              % (asm["jour"], " (dont %d incomplet(s))" % asm["incomplets"]
                 if asm["incomplets"] else "", asm["semaine"])]
        T += ["  - %s : %d" % x for x in asm["par_bon"]]
        if asm["delai_moyen"] is not None:
            T.append("  Délai moyen sortie du rack -> assemblé : %s" % duree(asm["delai_moyen"]))
    if nest is not None:
        T += ["", "NESTING (CNC)",
              "  Aujourd'hui : %d feuille(s) coupée(s)%s" % (
                  nest["coupees"], (" — " + ", ".join("%s : %d" % x for x in nest["par_bon"]))
                  if nest["par_bon"] else "")]
        T += ["  Terminé : %s — %d feuilles — temps de coupe %s" % (n, t, duree(h))
              for n, t, h in nest["termines"]]
        T += ["  Incomplet : %s — %d/%d feuilles, %d restante(s)" % (n, c, t, t - c)
              for n, c, t in nest["en_cours"]]
        T += ["  PROBLÈME : " + x for x in nest["problemes"]]
        if nest["reprises"]:
            T.append("  Reprises au nesting pas encore coupées : " + ", ".join(nest["reprises"]))
        if nest["nb_perdues"]:
            T.append("  Pièces coupées mais jamais arrivées au rack : %d" % nest["nb_perdues"])
            T += ["    - " + x for x in nest["perdues"]]
    T += ["", "PIÈCES MANQUANTES PAR PROJET"]
    for p, m, n in details:
        T.append("- %s : %d/%d scannées, %d manquante(s)"
                 % (nom_projet(p), p["scannees"], p["prevues"], n))
        for k in m:
            if k["scannees"] == 0:
                T.append("    %s : aucune pièce scannée (%d prévues)" % (k["code"], k["prevues"]))
            else:
                T.append("    %s : manque %s" % (k["code"], ", ".join(k["manquantes"])))
    T += ["", "CAISSONS DANS LE RACK DEPUIS PLUS D'UNE JOURNEE DE TRAVAIL"]
    T += ["- %s — %s — depuis %s" % (t["code"], t["projet"], duree(t["heures"]))
          for t in trainent] or ["Aucun."]
    T += ["", texte_stats("DÉTAIL DES SCANS — AUJOURD'HUI", jour), "",
          texte_stats("DÉTAIL DES SCANS — SEMAINE", semaine)]
    if anomalies:
        T += ["", "ANOMALIES"] + ["- " + a for a in anomalies]

    actif = (jour["pieces"] > 0 or jour["caissons_sortis"] > 0
             or bool(rep_ and rep_["jour"]) or bool(nest and nest["coupees"])
             or bool(asm and asm["jour"]) or bool(emb_j and emb_j["total"]))
    return sujet, "\n".join(H), "\n".join(T), actif


# ----------------------------------------------------------------------
# Envoi
# ----------------------------------------------------------------------

def envoyer(sujet, corps_html, corps_texte):
    donnees = json.dumps({"p_cle": CLE_RAPPORT, "p_sujet": sujet[:300],
                          "p_html": corps_html, "p_texte": corps_texte}).encode("utf-8")
    req = urllib.request.Request(
        SUPABASE_URL + "/rest/v1/rpc/repere_envoyer_rapport", data=donnees, method="POST",
        headers={"apikey": CLE_PUBLIQUE, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def main():
    test = "--test" in sys.argv
    apercu = "--apercu" in sys.argv
    maintenant = datetime.now()
    try:
        chemin = trouver_base()
        cx = ouvrir(chemin)
        sujet, h, t, actif = construire(cx, maintenant)
    except Exception:
        # Même en cas de plantage, Christian doit le savoir : on envoie l'erreur.
        err = traceback.format_exc()
        sujet = "CD Repère — rapport du soir EN ERREUR"
        h = "<p>Le rapport n'a pas pu être construit.</p><pre>%s</pre>" % html.escape(err)
        t = "Le rapport n'a pas pu être construit.\n\n" + err
        actif = True

    dossier = os.path.join(ICI, "donnees", "rapports")
    os.makedirs(dossier, exist_ok=True)
    fichier = os.path.join(dossier, maintenant.strftime("rapport_%Y-%m-%d.html"))
    with open(fichier, "w", encoding="utf-8") as f:
        f.write(h)
    print("Copie écrite :", fichier)
    print("Sujet :", sujet)

    if apercu:
        print("Aperçu seulement, rien n'est envoyé.")
        return
    if not actif and maintenant.weekday() >= 5 and not test:
        print("Fin de semaine sans activité : pas de courriel.")
        return
    print("Envoyé, numéro dans la file :", envoyer(sujet, h, t))


if __name__ == "__main__":
    main()
