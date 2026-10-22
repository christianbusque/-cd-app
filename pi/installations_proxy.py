"""
CD Repere — calendrier des installations (relais vers Supabase).

La page /installations ne parle qu'au serveur du rack. C'est lui qui lit et
ecrit les dates dans Supabase avec le code secret garde sur le Pi, dans
installations_config.json :
    { "code_modifier": "101", "code_supabase": "<code de Christian>" }
Ainsi :
  - voir le calendrier ne demande aucun code, mais seulement sur le reseau
    de l'usine ;
  - le code court (101) ne marche que sur le reseau de l'usine.
Bibliotheque standard uniquement.
"""
import json
import os
import urllib.error
import urllib.request

URL = "https://xgjlsddlbhmvkwyiwmqc.supabase.co/rest/v1/rpc/"
CLE = "sb_publishable_jdJMKiYpc_01gpmRgc17iA_yLOMqJ1D"
FICHIER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "installations_config.json")
CHAMPS = ("p_id", "p_titre", "p_detail", "p_type", "p_date_debut", "p_date_fin",
          "p_statut", "p_equipe", "p_source", "p_raison")


def _conf():
    with open(FICHIER, encoding="utf-8") as f:
        return json.load(f)


def _rpc(nom, args):
    req = urllib.request.Request(
        URL + nom, data=json.dumps(args).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "apikey": CLE,
                 "Authorization": "Bearer " + CLE})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode("utf-8"))
            m = d.get("message") or str(d)
        except Exception:
            m = "Erreur %s" % e.code
        return 400, {"message": m}
    except Exception as e:
        return 502, {"message": "Pas de connexion a Internet depuis le rack (%s)" % e}


def lister():
    code, d = _rpc("calendrier_lister", {"p_code": _conf()["code_supabase"]})
    if code == 200:
        d["peut_modifier"] = False  # la page active la modification avec le code
    return code, d


def verifier(code_saisi):
    return bool(code_saisi) and str(code_saisi).strip() == str(_conf()["code_modifier"])


def historique(ident):
    return _rpc("calendrier_historique_lister",
                {"p_code": _conf()["code_supabase"], "p_id": ident})


def definir(corps):
    if not verifier(corps.get("code")):
        return 403, {"message": "Code invalide"}
    args = {k: (corps.get(k) if corps.get(k) not in ("",) else None) for k in CHAMPS}
    args["p_code"] = _conf()["code_supabase"]
    return _rpc("calendrier_definir", args)
