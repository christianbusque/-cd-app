#!/bin/bash
# Installe le calendrier des installations sur le serveur CD Repère (Pi du rack).
# Sauvegarde d'abord, vérifie ensuite, ne redémarre que si tout est correct.
set -e
T=~/tri
D=~/sauvegardes/avant-installations-$(date +%Y%m%d-%H%M)
SRC=https://raw.githubusercontent.com/christianbusque/-cd-app/main/pi
echo "1. Sauvegarde dans $D"
mkdir -p $D/interface
cp $T/serveur.py $D/
cp $T/interface/nesting.html $T/interface/nesting_bureau.html $T/interface/poste.html $D/interface/
echo "2. Telechargement des 2 fichiers"
curl -fsS $SRC/installations.html -o /tmp/installations.html
curl -fsS $SRC/bouton-installations.js -o /tmp/bouton-installations.js
grep -q "calendrier_lister" /tmp/installations.html
grep -q "cd-btn-inst" /tmp/bouton-installations.js
cp /tmp/installations.html /tmp/bouton-installations.js $T/interface/
echo "3. Ajout des adresses /installations et /bouton-installations.js au serveur"
python3 - <<'PY'
import os
f = os.path.expanduser("~/tri/serveur.py")
s = open(f, encoding="utf-8").read()
if "/bouton-installations.js" not in s:
    ancre = '        if chemin in ("/logo.png", "/marque.png"):'
    assert s.count(ancre) == 1, "ancre introuvable"
    ajout = '''        # -- Calendrier des installations --------------------------------
        if chemin in ("/installations", "/installations/"):
            return self._fichier(os.path.join(RACINE, "interface", "installations.html"),
                                 "text/html; charset=utf-8")
        if chemin == "/bouton-installations.js":
            return self._fichier(os.path.join(RACINE, "interface", "bouton-installations.js"),
                                 "application/javascript; charset=utf-8")

'''
    s = s.replace(ancre, ajout + ancre)
    open(f, "w", encoding="utf-8").write(s)
    print("   ajoute")
else:
    print("   deja present")
PY
python3 -m py_compile $T/serveur.py && echo "   serveur.py : aucune erreur de syntaxe"
echo "4. Bouton dans CD-Nesting (2 pages) et CD-Palette (poste)"
for f in nesting nesting_bureau poste; do
  F=$T/interface/$f.html
  sed -i 's#https://app.cuisidesignab.com/bouton-installations.js#/bouton-installations.js#' $F
  grep -q 'src="/bouton-installations.js"' $F || sed -i 's#</body>#<script src="/bouton-installations.js"></script>\n</body>#' $F
  echo "   $f : $(grep -c 'src="/bouton-installations.js"' $F) ligne(s)"
done
echo "5. Redemarrage du serveur"
SVC=$(systemctl list-units --type=service --no-legend | grep -iE "repere|tri" | awk '{print $1}' | head -1)
if [ -n "$SVC" ]; then sudo systemctl restart "$SVC"; echo "   service $SVC redemarre"; sleep 4
else echo "   SERVICE NON TROUVE : envoie ce message a Claude"; exit 1; fi
echo "6. Verification"
for u in /installations /bouton-installations.js /nesting /poste; do
  echo "   $u : $(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080$u)"
done
echo "TERMINE. (Pour revenir en arriere : cp $D/serveur.py $T/ ; cp $D/interface/* $T/interface/ ; puis redemarrer)"
