#!/bin/bash
# Calendrier des installations, v2 : voir sans code, modifier avec 101.
# Usage : curl -fsS <adresse>/installer-v2.sh | bash -s <code_secret_de_Christian>
set -e
T=~/tri
CODE_SUPABASE="$1"
if [ -z "$CODE_SUPABASE" ]; then echo "ARRET : le code secret est manquant a la fin de la commande"; exit 1; fi
D=~/sauvegardes/avant-installations-v2-$(date +%Y%m%d-%H%M)
SRC=https://raw.githubusercontent.com/christianbusque/-cd-app/main/pi

echo "1. Sauvegarde dans $D"
mkdir -p $D/interface
cp $T/serveur.py $D/
cp $T/interface/installations.html $D/interface/ 2>/dev/null || true

echo "2. Telechargement"
curl -fsS $SRC/installations.html -o /tmp/installations.html
curl -fsS $SRC/installations_proxy.py -o /tmp/installations_proxy.py
grep -q "api('/api/installations')" /tmp/installations.html
grep -q "def definir" /tmp/installations_proxy.py
cp /tmp/installations.html $T/interface/
cp /tmp/installations_proxy.py $T/

echo "3. Code de modification (101) et code secret, gardes seulement sur ce Pi"
printf '{"code_modifier": "101", "code_supabase": "%s"}\n' "$CODE_SUPABASE" > $T/installations_config.json
chmod 600 $T/installations_config.json

echo "4. Ajout du relais au serveur"
python3 - "$T/serveur.py" <<'PY'
import sys
f = sys.argv[1]
s = open(f, encoding="utf-8").read()
if "installations_proxy" in s:
    print("   deja present"); sys.exit(0)
def inserer(s, ancre, ajout, avant=True):
    assert s.count(ancre) == 1, "ancre introuvable : " + ancre[:50]
    return s.replace(ancre, (ajout + ancre) if avant else (ancre + ajout))
s = inserer(s, "import poste_palette\n", "import installations_proxy\n", avant=False)
s = inserer(s, '        if chemin == "/bouton-installations.js":',
'''        if chemin == "/api/installations":
            code, d = installations_proxy.lister()
            return self._repondre(d, code)
''')
ancre = "        corps = self._lire_corps()\n\n        # -- CD Assemblage (v16)"
assert s.count(ancre) == 1, "ancre do_POST introuvable"
s = s.replace(ancre, '''        corps = self._lire_corps()

        # -- Calendrier des installations (relais vers Supabase) -----------
        if chemin == "/api/installations/verifier":
            return self._repondre({"ok": installations_proxy.verifier(corps.get("code"))})
        if chemin == "/api/installations/historique":
            code, d = installations_proxy.historique(corps.get("id"))
            return self._repondre(d, code)
        if chemin == "/api/installations/definir":
            code, d = installations_proxy.definir(corps)
            return self._repondre(d, code)

        # -- CD Assemblage (v16)''')
open(f, "w", encoding="utf-8").write(s)
print("   ajoute")
PY
cd $T && python3 -m py_compile serveur.py installations_proxy.py && echo "   aucune erreur de syntaxe"

echo "5. Test du lien avec Supabase (avant de redemarrer)"
python3 -c "
import installations_proxy as p
c, d = p.lister()
print('   lecture :', c, (str(len(d.get('evenements', []))) + ' evenements') if c == 200 else d)
print('   code 101 accepte :', p.verifier('101'), '| code 999 refuse :', not p.verifier('999'))
assert c == 200
"

echo "6. Redemarrage du serveur"
sudo systemctl restart cd-repere.service; sleep 4
echo "7. Verification"
echo "   /installations : $(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/installations)"
echo "   /api/installations : $(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/api/installations)"
echo "   /nesting : $(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/nesting)"
echo "   /poste : $(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/poste)"
echo "   verifier 101 : $(curl -s -X POST -H 'Content-Type: application/json' -d '{"code":"101"}' http://localhost:8080/api/installations/verifier)"
echo "TERMINE. (Retour en arriere : cp $D/serveur.py $T/ ; sudo systemctl restart cd-repere.service)"
