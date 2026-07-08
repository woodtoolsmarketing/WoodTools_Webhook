# Carga catalogo_variantes.json -> tabla variantes en Supabase (upsert por codigo).
# Reutiliza DATABASE_URL de migrador.py para no duplicar credenciales.
import json
import psycopg2
from psycopg2.extras import execute_values
from migrador import DATABASE_URL

with open("catalogo_variantes.json", encoding="utf-8") as f:
    filas = json.load(f)

rows = [(
    r["codigo"], r["familia"], r["subgrupo"], r["grupo"], r["subtipo"], r["material_corte"],
    r["titulo"], r["marca"], r["uso"], r["material"],
    r["diametro_mm"], r["espesor_mm"], r["eje_mm"], r["dientes_z"], r["spec_raw"]
) for r in filas]

conn = psycopg2.connect(DATABASE_URL, sslmode="require")
cur = conn.cursor()
# Refresh LIMPIO: vaciamos y recargamos todo el catálogo desde producto.js,
# así no quedan filas viejas/incorrectas (rearmado completo).
cur.execute("TRUNCATE TABLE variantes RESTART IDENTITY")
execute_values(cur, """
    INSERT INTO variantes
        (codigo, familia, subgrupo, grupo, subtipo, material_corte,
         titulo, marca, uso, material, diametro_mm, espesor_mm, eje_mm, dientes_z, spec_raw)
    VALUES %s
""", rows, page_size=200)
conn.commit()
cur.execute("SELECT count(*) FROM variantes")
print("OK variantes en DB:", cur.fetchone()[0])
cur.close()
conn.close()
