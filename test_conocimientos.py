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
def hay_productos(r):
    """Al menos una linea de producto ("- Titulo (Marca)..."), sea cual sea el encabezado."""
    return any(l.startswith('- ') for l in (r or '').splitlines())

for fam, gru, n in FAM_GRUPOS:
    r = servidor.consultar_catalogo(fam, gru or '')
    chk(hay_productos(r), f"consultar_catalogo({fam},{gru}) [{n} filas] -> {r[:60]}")
    chk(r.startswith('[NOTA INTERNA'), f"consultar_catalogo({fam},{gru}) no arranca con la nota interna")

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
# La tool contesta "ACCION: ofrecele 250mm, 240mm, 230mm en este mismo mensaje ...".
# Se parsea SOLO la lista ofrecida: el resto del texto nombra la medida pedida
# (en el "PROHIBIDO cotizar Nmm") y no hay que confundirla con una sugerencia.
RE_OFRECE = re.compile(r'ofrecele\s+(.+?),\s*que son las medidas', re.I)

def ofrecidas(txt):
    m = RE_OFRECE.search(txt or '')
    return [float(x) for x in re.findall(r'(\d+(?:\.\d+)?)mm', m.group(1))] if m else []

for fam, col, pedidos in [('Sierras', 'diametro_mm', ['280', '999', '1']),
                          ('Mechas', 'diametro_mm', ['3,5', '11', '500'])]:
    for pedido in pedidos:
        r = servidor.consultar_medidas(fam, pedido, '', '', '')
        sug = ofrecidas(r)
        chk(not ('ofrecele' in r and not sug),
            f"{fam}/{pedido}: dice que ofrece medidas pero no se pudo parsear ninguna")
        for m in sug:
            ex = servidor.execute_db_query(
                f"SELECT 1 FROM variantes WHERE familia ILIKE %s AND {col} = %s LIMIT 1",
                (fam, m), fetchone=True)
            chk(bool(ex), f"{fam}: sugirio {m}mm y NO existe en el catalogo")
            chk(abs(m - float(pedido.replace(',', '.'))) > 1e-9,
                f"{fam}: dijo que no hay {pedido} y despues lo ofrecio")
for pedido in ['260', '99', '2000']:
    r = servidor.consultar_medidas('Cuchillas', '', '', '', '', pedido)
    for m in ofrecidas(r):
        ex = servidor.execute_db_query(
            "SELECT 1 FROM variantes WHERE familia ILIKE 'Cuchillas' AND largo_mm = %s LIMIT 1",
            (m,), fetchone=True)
        chk(bool(ex), f"Cuchillas: sugirio largo {m}mm y NO existe")
        chk(abs(m - float(pedido)) > 1e-9,
            f"Cuchillas: dijo que no hay {pedido} y despues lo ofrecio")

# ---------- 6b. toda respuesta vacia tiene que traer una ACCION, nunca un "no hay" ----------
for fam, args in [('Cuchillas', ('', '', '', '', '260')), ('Sierras', ('280', '', '', '')),
                  ('Fresas', ('9999', '', '', '')), ('Mechas', ('777', '', '', ''))]:
    r = servidor.consultar_medidas(fam, *args)
    chk('[NOTA INTERNA' in r, f"{fam}: respuesta sin nota interna -> {r[:70]}")

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
    exento = any(t in cond.lower() for t in
                 ('no filtra', 'no es un filtro', 'nunca lo pases a una tool'))
    chk(slot in params or exento,
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

# ---------- 12. el saneador impide que salga la tripa de una tool por WhatsApp ----------
crudo = servidor.consultar_medidas('Mechas', '35', '', 'bisagra', '')
limpio = servidor._limpiar_para_cliente(crudo)
for marcador in ['cod_oculto', '(ficha:', 'NOTA INTERNA', 'PROHIBIDO', 'busqueda vacia']:
    chk(marcador not in limpio, f"el saneador dejo pasar '{marcador}'")
chk(any(l.startswith('- ') for l in limpio.splitlines()),
    "el saneador se llevo tambien las lineas de producto")
for crudo2 in [servidor.consultar_catalogo('Mechas', 'bisagra'),
               servidor.consultar_medidas('Cuchillas', '', '', '', '', '260'),
               servidor.consultar_catalogo('Cuchillas', 'planas', '', 'hss')]:
    l2 = servidor._limpiar_para_cliente(crudo2)
    for marcador in ['cod_oculto', '(ficha:', 'NOTA INTERNA']:
        chk(marcador not in l2, f"el saneador dejo pasar '{marcador}' en otra salida")

print()
if fallos:
    print("FALLOS:", len(fallos))
    for f in fallos[:40]:
        print("  -", f)
else:
    print("SIN FALLOS: los 12 bloques de regresion pasan.")

