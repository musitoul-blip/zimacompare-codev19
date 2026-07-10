#!/bin/sh
# =====================================================================
#  bugcheck.sh  -  filet anti-bug ZimaCompare&Tag v10  (v2)
#  Usage : sudo -v && sudo sh bugcheck.sh   (conteneur zimacompare-v15 en service)
#  Verdict PASS / A VOIR. Ignore le bruit connu (RUF012/B007/F401/unicode),
#  ne signale que les VRAIS bugs + erreurs runtime.
# =====================================================================
C=zimacompare-v19
PORT=8519
FAIL=0

echo "===== 1/7  SYNTAXE (compileall) ====="
if sudo docker exec $C sh -lc "cd /app && python3 -m compileall -q ."; then
  echo "  OK"
else
  echo "  >> ECHEC SYNTAXE"; FAIL=1
fi

echo "===== 2/7  RUFF (vrais bugs uniquement) ====="
RUFF_REAL="F821,F823,F811,F841,F501,F502,F506,F522,F601,F602,F631,F632,F633,F701,F702,F706,F707,PLE,B006,B012,B018"
OUT=$(sudo docker exec $C sh -lc "pip install ruff -q 2>/dev/null; cd /app && ruff check --select $RUFF_REAL --exclude '__pycache__' . 2>&1")
echo "$OUT" | tail -20
HARD=$(echo "$OUT" | grep -E "F821|F823|F811|F50|F60|F63|F70|PLE|B006|B012|B018")
if [ -n "$HARD" ]; then
  echo "  >> BUGS POTENTIELS (voir ci-dessus)"; FAIL=1
else
  echo "  OK (au pire des var inutilisees cosmetiques F841)"
fi

echo "===== 3/7  IMPORTS (runtime, meme sys.path que l'app) ====="
IMP=$(sudo docker exec -i $C python3 - <<'PY'
import importlib, sys, os
ok = True
sys.path.insert(0, '/app')              # racine backend
sys.path.insert(0, '/app/tagaudit')     # comme tagscan.py (core/providers/audit/engine/export)
mods = [f[:-3] for f in os.listdir('/app') if f.endswith('.py') and f != '__init__.py']
for sub in ('core', 'providers', 'audit', 'engine', 'export'):
    d = '/app/tagaudit/' + sub
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f.endswith('.py') and f != '__init__.py':
                mods.append(sub + '.' + f[:-3])
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        line = (str(e).splitlines() or [''])[0]
        print("IMPORT KO:", m, "->", type(e).__name__, line)
        ok = False
print("ALLOK" if ok else "SOMEFAIL")
PY
)
echo "$IMP" | grep -v ALLOK | grep -v SOMEFAIL
if echo "$IMP" | grep -q "^ALLOK$\|ALLOK"; then echo "  OK"; else echo "  >> IMPORT(S) CASSE(S)"; FAIL=1; fi

echo "===== 4/7  SMOKE ENDPOINTS GET (5xx/000 = bug handler ; 4xx = validation, OK) ====="
for p in /api/health /api/status /api/file-types /api/profiles /api/tag/progress /api/tag/report.html /api/smart/devices /api/audit-registry /api/audit-params /api/ui-prefs/audit_col_widths; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT$p" 2>/dev/null)
  case "$code" in
    5*|000) echo "  $code $p   <<< BUG"; FAIL=1 ;;
    *)      echo "  $code $p" ;;
  esac
done

echo "===== 5/7  AUDITS ENREGISTRES SANS METHODE (specifique projet) ====="
MISS=$(sudo docker exec $C sh -lc "cd /app/tagaudit/audit && REF=\$(grep -oE 'self\._audit_[a-z0-9_]+' audit_engine.py | sed 's/self\.//' | sort -u); DEF=\$(grep -oE 'def _audit_[a-z0-9_]+' audit_engine.py | sed 's/def //' | sort -u); for r in \$REF; do echo \"\$DEF\" | grep -qx \"\$r\" || echo \"  MANQUE: \$r\"; done")
if [ -n "$MISS" ]; then echo "$MISS"; echo "  >> AUDIT(S) FANTOME(S)"; FAIL=1; else echo "  OK (tous les audits enregistres ont leur methode)"; fi

echo "===== 6/7  BASE audit_registry (integrite + seed idempotent) ====="
# check 6 base audit_registry : 3 tables presentes, init_and_seed ne plante pas, audits coherents
DB=$(sudo docker exec $C python3 -c "
import sqlite3, sys
sys.path.insert(0, '/app/tagaudit')
try:
    from core import audit_registry as R
    R.init_and_seed()          # idempotent : ne doit pas planter sur base existante
    c = sqlite3.connect('/app_data/audit_registry.db')
    tabs = set(r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\"))
    need = {'audit_registry','ui_prefs','audit_params'}
    miss = need - tabs
    n = c.execute('SELECT COUNT(*) FROM audit_registry').fetchone()[0]
    if miss:
        print('TABLES_MANQUANTES:', sorted(miss))
    elif n < 1:
        print('REGISTRY_VIDE')
    else:
        print('DBOK n=%d' % n)
except Exception as e:
    print('DBERR:', e)
" 2>&1)
echo "$DB" | grep -v DBOK
if echo "$DB" | grep -q "DBOK"; then echo "  OK ($(echo "$DB" | grep -oE 'n=[0-9]+'))"; else echo "  >> BASE audit_registry KO"; FAIL=1; fi

echo "===== 7/7  BUNDLE FRONT (build produit + page servie) ====="
# check 7 : le bundle JS existe et n'est pas vide, et GET / renvoie 200 (HTML)
JS=$(sudo docker exec $C sh -lc 'f=$(ls -1 /usr/share/nginx/html/assets/index-*.js 2>/dev/null | head -1); if [ -n "$f" ] && [ -s "$f" ]; then echo "JSOK $(wc -c < "$f")"; else echo "JSMISSING"; fi')
ROOT=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT/" 2>/dev/null)
echo "  bundle: $JS | GET /: $ROOT"
if echo "$JS" | grep -q "JSOK" && [ "$ROOT" = "200" ]; then echo "  OK (bundle present, page servie)"; else echo "  >> FRONT non servi (build oublie ?)  [bugcheck reste backend ; verif navigateur tjrs requise]"; FAIL=1; fi

echo "================================================================"
if [ $FAIL = 0 ]; then echo "VERDICT : PASS  -  aucun bug bloquant detecte"; else echo "VERDICT : A VOIR  -  items '>>' ci-dessus"; fi
