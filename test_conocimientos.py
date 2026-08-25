# -*- coding: utf-8 -*-
"""Barrido de regresion sobre las 3 tools de conocimiento: todas las familias, todos
los grupos, entradas basura, y validacion de que ninguna respuesta le mienta al cliente."""
import sys, os, re, itertools
sys.path.insert(0, r"C:/Users/WoodTools-02/Desktop/vscode/WoodTools_Webhook")
os.chdir(r"C:/Users/WoodTools-02/Desktop/vscode/WoodTools_Webhook")
import psycopg2, psycopg2.pool
import servidor
from migrador import DATABASE_URL

try: servidor.scheduler.shutdown(wait=False)
except Exception: pass
servidor.db_pool = psycopg2.pool.SimpleConnectionPool(1, 5, DATABASE_URL, sslmode='require')

fallos = []
def chk(cond, msg):
    if not cond:
        fallos.append(msg)

FAM_GRUPOS = servidor.execute_db_query(
    "SELECT familia, grupo, count(*) FROM variantes GROUP BY 1,2 ORDER BY 1,2", fetchall=True)

# ---------- 1. consultar_flujo: ninguna familia puede quedar sin flujo ----------
for f in ['Sierras', 'Fresas', 'Mechas', 'Cuchillas', 'Diamante', 'Cabezales', 'atencion',
          'atención', 'SIERRAS', 'sierra', 'cabezal', 'Diamante ', 'ATENCION']:
    r = servidor.consultar_flujo(f)
    chk(not r.startswith('Familia desconocida'), f"consultar_flujo({f!r}) -> desconocida")
# y una inexistente SI debe avisar, sin nombrar familias que si existen
r = servidor.consultar_flujo('Martillos')
chk(r.startswith('Familia desconocida'), "consultar_flujo('Martillos') deberia ser desconocida")
chk("No existe 'Diamante'" not in r, "el mensaje de error sigue negando Diamante")

# ---------- 2. consultar_catalogo: todo familia+grupo real debe devolver algo ----------
for fam, gru, n in FAM_GRUPOS:
    r = servidor.consultar_catalogo(fam, gru or '')
    chk('DATOS TECNICOS' in r, f"consultar_catalogo({fam},{gru}) [{n} filas] -> {r[:60]}")

# ---------- 3. no se pisan las familias entre si ----------
r = servidor.consultar_catalogo('Cabezales')
chk('Ilma' not in r, "familia 'Cabezales' trae cuchillas Ilma")
r = servidor.consultar_catalogo('Cuchillas', 'cabezales')
chk('Freud' not in r, "Cuchillas/cabezales trae los Cabezales Freud")

# ---------- 4. lado: nunca inferir un giro que el cliente no pidio ----------
for gru in ['pasante', 'ciega', 'bisagra']:
    for l, esperado in [('derecha', {'derecha'}), ('izquierda', {'izquierda'}),
                        ('a la derecha', {'derecha'}), ('giro izquierdo', {'izquierda'}),
                        ('ambas', {'derecha', 'izquierda'}), ('las dos', {'derecha', 'izquierda'}),
                        ('da igual', {'derecha', 'izquierda'}), ('', {'derecha', 'izquierda'}),
                        ('(((', {'derecha', 'izquierda'}), ('%%%', {'derecha', 'izquierda'})]:
        r = servidor.consultar_catalogo('Mechas', gru, '', '', l)
        chk('Sin match' not in r and 'FALLO TECNICO' not in r,
            f"Mechas/{gru} lado={l!r} -> {r[:60]}")
        vistos = {x.lower() for x in re.findall(r'\((Derecha|Izquierda)\)', r)}
        if vistos:
            chk(vistos <= esperado, f"Mechas/{gru} lado={l!r} devolvio {vistos}, esperaba <= {esperado}")

# ---------- 5. consultar_medidas: entradas basura no deben romper ni mentir ----------
for fam in ['Sierras', 'Fresas', 'Mechas', 'Cuchillas', 'Diamante', 'Cabezales', '', 'Martillos']:
    for d in ['', '0', '-5', 'abc', '3,5', '  300  ', 'x' * 300]:
        r = servidor.consultar_medidas(fam, d, '', '', '')
        chk(isinstance(r, str) and 'FALLO TECNICO' not in r,
            f"consultar_medidas({fam!r},{d[:20]!r}) -> {str(r)[:70]}")

# ---------- 6. la medida "mas cercana" que sugiere TIENE que existir ----------
for fam, col, pedidos in [('Sierras', 'diametro_mm', ['280', '999', '1']),
                          ('Mechas', 'diametro_mm', ['3,5', '11', '500']),
                          ('Cuchillas', 'largo_mm', None)]:
    for pedido in (pedidos or []):
        r = servidor.consultar_medidas(fam, pedido, '', '', '')
        for m in re.findall(r'(\d+(?:\.\d+)?)mm', r.split('cercanas que SI tenemos son:')[-1]) \
                 if 'cercanas que SI tenemos' in r else []:
            ex = servidor.execute_db_query(
                f"SELECT 1 FROM variantes WHERE familia ILIKE %s AND {col} = %s LIMIT 1",
                (fam, float(m)), fetchone=True)
            chk(bool(ex), f"{fam}: sugirio {m}mm y NO existe en el catalogo")
for pedido in ['260', '99', '2000']:
    r = servidor.consultar_medidas('Cuchillas', '', '', '', '', pedido)
    if 'cercanas que SI tenemos' in r:
        for m in re.findall(r'(\d+(?:\.\d+)?)mm', r.split('cercanas que SI tenemos son:')[-1]):
            ex = servidor.execute_db_query(
                "SELECT 1 FROM variantes WHERE familia ILIKE 'Cuchillas' AND largo_mm = %s LIMIT 1",
                (float(m),), fetchone=True)
            chk(bool(ex), f"Cuchillas: sugirio largo {m}mm y NO existe")
        chk(pedido + 'mm' not in r.split('son:')[-1],
            f"Cuchillas: dijo 'no hay {pedido}' y despues ofrecio {pedido}")

# ---------- 7. ningun diametro devuelto puede ser inventado ----------
rows = servidor.execute_db_query(
    "SELECT codigo, diametro_mm, spec_raw, titulo FROM variantes WHERE diametro_mm IS NOT NULL",
    fetchall=True)
for cod, d, spec, tit in rows:
    chk(re.search(r'(^|[^0-9])%d([^0-9]|$)' % d, (spec or '') + ' ' + (tit or '')),
        f"{cod}: diametro_mm={d} no aparece en su ficha ({spec})")

# ---------- 8. los regulables no pueden anunciarse como medida fija ----------
regs = servidor.execute_db_query(
    "SELECT codigo, diametro_mm, diametro_min_mm, diametro_max_mm FROM variantes "
    "WHERE diametro_min_mm IS NOT NULL", fetchall=True)
for cod, d, mn, mx in regs:
    chk(d is None, f"{cod}: es regulable {mn}-{mx} pero tiene diametro fijo {d}")

# ---------- 9. el flujo no puede pedir algo que la tool no sabe filtrar ----------
import inspect
params = set()
for fn in (servidor.consultar_catalogo, servidor.consultar_medidas):
    params |= set(inspect.signature(fn).parameters)
for fam, orden, slot, cond in servidor.execute_db_query(
        "SELECT familia, orden, slot, COALESCE(condicion,'') FROM flujo_pregunta ORDER BY familia, orden",
        fetchall=True):
    chk(slot in params or 'NO filtra' in cond or 'no filtra' in cond.lower(),
        f"flujo_pregunta {fam}/{orden}: slot '{slot}' no es parametro de ninguna tool")

# ---------- 10. cada opcion del flujo tiene que existir en el catalogo ----------
for fam, slot, ops in servidor.execute_db_query(
        "SELECT familia, slot, opciones FROM flujo_pregunta WHERE slot IN ('grupo','subtipo','material_corte')",
        fetchall=True):
    # 'opciones' trae el valor canonico + sinonimos entre parentesis:
    # "planas (=cepillo, garlopa), chipera (=chipeadora)". Se corta por comas de
    # nivel 0 y se toma el token canonico (lo previo al parentesis).
    def _canon(txt):
        out, buf, prof = [], '', 0
        for ch in txt or '':
            if ch == '(': prof += 1
            elif ch == ')': prof -= 1
            if ch == ',' and prof == 0:
                out.append(buf); buf = ''
            else:
                buf += ch
        out.append(buf)
        return [o.split('(')[0].strip() for o in out if o.split('(')[0].strip()]
    for op in _canon(ops):
        ex = servidor.execute_db_query(
            f"SELECT 1 FROM variantes WHERE familia ILIKE %s AND {slot} = %s LIMIT 1",
            (fam, op), fetchone=True)
        chk(bool(ex), f"flujo {fam}: la opcion '{op}' de {slot} no existe en variantes")
# ...y al reves: ningun grupo con productos puede quedar inalcanzable
for fam, gru, n in FAM_GRUPOS:
    if not gru:
        continue
    ops = servidor.execute_db_query(
        "SELECT opciones FROM flujo_pregunta WHERE familia ILIKE %s AND slot='grupo'",
        (fam,), fetchone=True)
    chk(ops and re.search(r'(^|,)\s*' + re.escape(gru) + r'\s*($|,|\()', ops[0]), f"{fam}: grupo '{gru}' ({n} productos) no figura en las opciones del flujo")

# ---------- 11. las marcas del flujo tienen que ser las reales ----------
for fam, in servidor.execute_db_query(
        "SELECT DISTINCT familia FROM variantes", fetchall=True):
    nota = servidor.execute_db_query(
        "SELECT nota_familia FROM flujo_familia WHERE familia ILIKE %s", (fam,), fetchone=True)
    if not nota:
        fallos.append(f"{fam}: no tiene fila en flujo_familia"); continue
    reales = {m for (m,) in servidor.execute_db_query(
        "SELECT DISTINCT marca FROM variantes WHERE familia ILIKE %s", (fam,), fetchall=True)}
    for falsa in {'Freud', 'Franzoi', 'WoodTools', 'Italiana', 'Nordutensili', 'Ilma', 'Schiavon'} - reales:
        chk(not re.search(r'Marcas?:[^.]*\b' + falsa + r'\b', nota[0]),
            f"{fam}: la nota nombra la marca '{falsa}' que NO existe en esa familia (reales: {reales})")

print()
if fallos:
    print("FALLOS:", len(fallos))
    for f in fallos[:40]:
        print("  -", f)
else:
    print("SIN FALLOS: los 11 bloques de regresion pasan.")
