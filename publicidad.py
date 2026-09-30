# -*- coding: utf-8 -*-
"""
Registro de consultas que ingresan por PUBLICIDAD (Click-to-WhatsApp) al webhook.

Qué hace:
  - En CADA mensaje entrante detecta si es una consulta originada en publicidad, usando
    dos señales complementarias:
        1) FRASE OBJETIVO: el texto contiene "Hola quiero más información"
           (insensible a mayúsculas, tildes y puntuación).  <-- el parámetro que pediste
        2) REFERRAL: el mensaje trae el objeto `referral`, que Meta adjunta SIEMPRE que
           el cliente llega tocando un anuncio Click-to-WhatsApp (o un posteo con CTA).
           Es la señal más confiable y además nos dice de QUÉ anuncio vino.
  - Guarda cada consulta en la tabla `consultas_publicidad` (idempotente por id de mensaje,
    así los reintentos del webhook no cuentan doble). Se conserva el detalle por mensaje.
  - Cuenta "1 por persona por día": un mismo teléfono suma como mucho 1 vez por día. El
    total de cada ventana son pares distintos (teléfono, día); 'unicos' son personas distintas.
  - Expone endpoints y un panel web en vivo para ver la cantidad por día / semana / mes.

Este módulo es autónomo: no importa nada de `servidor.py`. `servidor.py` lo enciende una
sola vez con `publicidad.configurar(app, execute_db_query, hora_arg, limpiar_numero, version)`.
"""

import io
import csv
import re
import time
import unicodedata
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, Response

# ==========================================================================================
# DEPENDENCIAS INYECTADAS (las pone servidor.py en configurar(); no se importan para evitar
# imports circulares).
# ==========================================================================================
_execute_db_query = None      # función de servidor.py
_hora_arg = None              # devuelve datetime en hora Argentina (UTC-3), naive
_limpiar_numero = None        # deja solo dígitos
_version = "s/d"

# Frase por defecto que busca dentro del mensaje. Se puede sobreescribir sin tocar código
# cargando el parámetro 'frase_publicidad' en la tabla `configuracion`.
FRASE_OBJETIVO_DEFAULT = "Hola quiero más información"

bp = Blueprint("publicidad", __name__)

# Caché chico de la frase para no pegarle a la base en cada mensaje.
_frase_cache = {"valor": None, "ts": 0.0}


# ==========================================================================================
# NORMALIZACIÓN Y DETECCIÓN
# ==========================================================================================
def _normalizar(texto):
    """minúsculas, sin tildes y con la puntuación convertida a espacios. Así
    'Hola, quiero más información!' matchea contra 'hola quiero mas informacion'."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFD", str(texto))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")   # saca tildes
    t = t.lower()
    t = re.sub(r"[^a-z0-9ñ]+", " ", t)     # cualquier símbolo/puntuación -> espacio
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _frase_objetivo():
    """Frase a buscar. Lee 'frase_publicidad' de `configuracion` (cache 60s) y si no hay,
    usa la de por defecto."""
    ahora = time.time()
    if _frase_cache["valor"] is not None and (ahora - _frase_cache["ts"]) < 60:
        return _frase_cache["valor"]
    valor = FRASE_OBJETIVO_DEFAULT
    try:
        res = _execute_db_query(
            "SELECT valor FROM configuracion WHERE parametro = 'frase_publicidad'",
            fetchone=True,
        )
        # Solo aceptamos la frase configurada si TIENE contenido real una vez normalizada.
        # Si alguien carga algo que normaliza a vacío (solo signos/espacios), lo ignoramos y
        # usamos la de por defecto; si no, "" quedaría contenido en todo mensaje y contaría todo.
        if res and res[0] and _normalizar(res[0]):
            valor = res[0]
    except Exception:
        pass
    _frase_cache["valor"] = valor
    _frase_cache["ts"] = ahora
    return valor


def _int(v, defecto):
    """Convierte a int de forma segura; si no se puede (ej. ?limite=abc) devuelve el defecto."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return defecto


def _csv_seguro(v):
    """Neutraliza inyección de fórmulas en CSV (Excel/Sheets): si el valor empieza con
    = + - @ o un control, le antepone una comilla simple para que no se interprete como fórmula."""
    s = "" if v is None else str(v)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r", "\n"):
        s = "'" + s
    return s.replace("\n", " ").replace("\r", " ")


def _texto_del_mensaje(m):
    """Saca el texto legible de un mensaje de WhatsApp (body si es texto; caption si es
    imagen/video/documento)."""
    tipo = m.get("type", "")
    if tipo == "text":
        return (m.get("text") or {}).get("body", "") or ""
    contenido = m.get(tipo)
    if isinstance(contenido, dict):
        return contenido.get("caption", "") or ""
    return ""


# ==========================================================================================
# BASE DE DATOS
# ==========================================================================================
def crear_tabla():
    try:
        _execute_db_query(
            """CREATE TABLE IF NOT EXISTS consultas_publicidad (
                    id_mensaje   TEXT PRIMARY KEY,
                    telefono     TEXT,
                    fecha        TIMESTAMP,
                    tipo_mensaje TEXT,
                    texto        TEXT,
                    origen       TEXT,
                    ref_fuente   TEXT,
                    ref_id       TEXT,
                    ref_titular  TEXT,
                    ref_cuerpo   TEXT,
                    ref_url      TEXT,
                    ctwa_clid    TEXT
               )""",
            commit=True,
        )
        _execute_db_query(
            "CREATE INDEX IF NOT EXISTS idx_consultas_pub_fecha ON consultas_publicidad (fecha)",
            commit=True,
        )
    except Exception as e:
        print(f"[publicidad] no se pudo crear la tabla: {e}", flush=True)


def registrar_consulta(m, telefono):
    """Se llama en CADA mensaje entrante desde el webhook. Si el mensaje entró por
    publicidad (frase objetivo o referral de anuncio) lo guarda; si no, no hace nada.
    Nunca lanza excepción hacia afuera para no interrumpir el webhook."""
    try:
        if _execute_db_query is None:
            return
        id_msg = m.get("id")
        if not id_msg:
            return

        texto = _texto_del_mensaje(m)
        # 'referral' solo debería venir como dict; si Meta (o un payload falso) manda otra cosa,
        # lo tratamos como ausente para no romper los .get() de abajo.
        referral = m.get("referral")
        if not isinstance(referral, dict):
            referral = {}

        frase_norm = _frase_objetivo_normalizada()
        tiene_frase = bool(frase_norm) and frase_norm in _normalizar(texto)
        tiene_referral = bool(referral)   # solo aparece cuando el cliente vino de un anuncio/posteo

        if not (tiene_frase or tiene_referral):
            return

        if tiene_frase and tiene_referral:
            origen = "ambos"
        elif tiene_referral:
            origen = "referral"
        else:
            origen = "frase"

        _execute_db_query(
            """INSERT INTO consultas_publicidad
                   (id_mensaje, telefono, fecha, tipo_mensaje, texto, origen,
                    ref_fuente, ref_id, ref_titular, ref_cuerpo, ref_url, ctwa_clid)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id_mensaje) DO NOTHING""",
            (
                str(id_msg),
                str(telefono),
                _hora_arg(),
                m.get("type", ""),
                (texto or "")[:1000],
                origen,
                referral.get("source_type", ""),
                referral.get("source_id", ""),
                referral.get("headline", ""),
                (referral.get("body", "") or "")[:1000],
                referral.get("source_url", ""),
                referral.get("ctwa_clid", ""),
            ),
            commit=True,
        )
    except Exception as e:
        print(f"[publicidad] error registrando consulta (tel={telefono}): {e}", flush=True)


def _frase_objetivo_normalizada():
    return _normalizar(_frase_objetivo())


# ==========================================================================================
# CONSULTAS DE LECTURA (resumen / serie / lista)
# ==========================================================================================
def _limites_de_tiempo():
    """Devuelve los cortes temporales en hora Argentina (naive), coherentes con cómo se
    guarda `fecha`."""
    ahora = _hora_arg()
    hoy = datetime(ahora.year, ahora.month, ahora.day)
    ayer = hoy - timedelta(days=1)
    inicio_semana = hoy - timedelta(days=hoy.weekday())          # lunes 00:00 de esta semana
    inicio_mes = datetime(ahora.year, ahora.month, 1)
    hace_7 = ahora - timedelta(days=7)
    hace_30 = ahora - timedelta(days=30)
    return {
        "ahora": ahora, "hoy": hoy, "ayer": ayer,
        "inicio_semana": inicio_semana, "inicio_mes": inicio_mes,
        "hace_7": hace_7, "hace_30": hace_30,
    }


def resumen():
    # Conteo "1 por persona por día": el total de cada ventana cuenta pares distintos
    # (telefono, día), así un mismo número suma como mucho 1 por día. 'unicos' cuenta
    # personas distintas (sin importar cuántos días consultaron).
    t = _limites_de_tiempo()
    fila = _execute_db_query(
        """SELECT
              COUNT(DISTINCT (telefono, fecha::date)) FILTER (WHERE fecha >= %(hoy)s)                       AS hoy_t,
              COUNT(DISTINCT telefono)                FILTER (WHERE fecha >= %(hoy)s)                       AS hoy_u,
              COUNT(DISTINCT (telefono, fecha::date)) FILTER (WHERE fecha >= %(ayer)s AND fecha < %(hoy)s)  AS ayer_t,
              COUNT(DISTINCT telefono)                FILTER (WHERE fecha >= %(ayer)s AND fecha < %(hoy)s)  AS ayer_u,
              COUNT(DISTINCT (telefono, fecha::date)) FILTER (WHERE fecha >= %(sem)s)                       AS sem_t,
              COUNT(DISTINCT telefono)                FILTER (WHERE fecha >= %(sem)s)                       AS sem_u,
              COUNT(DISTINCT (telefono, fecha::date)) FILTER (WHERE fecha >= %(mes)s)                       AS mes_t,
              COUNT(DISTINCT telefono)                FILTER (WHERE fecha >= %(mes)s)                       AS mes_u,
              COUNT(DISTINCT (telefono, fecha::date)) FILTER (WHERE fecha >= %(h7)s)                        AS s7_t,
              COUNT(DISTINCT telefono)                FILTER (WHERE fecha >= %(h7)s)                        AS s7_u,
              COUNT(DISTINCT (telefono, fecha::date)) FILTER (WHERE fecha >= %(h30)s)                       AS s30_t,
              COUNT(DISTINCT telefono)                FILTER (WHERE fecha >= %(h30)s)                       AS s30_u,
              COUNT(DISTINCT (telefono, fecha::date))                                                       AS tot_t,
              COUNT(DISTINCT telefono)                                                                      AS tot_u,
              COUNT(DISTINCT (telefono, fecha::date)) FILTER (WHERE origen = 'frase')                       AS o_frase,
              COUNT(DISTINCT (telefono, fecha::date)) FILTER (WHERE origen = 'referral')                    AS o_ref,
              COUNT(DISTINCT (telefono, fecha::date)) FILTER (WHERE origen = 'ambos')                       AS o_ambos
           FROM consultas_publicidad""",
        {
            "hoy": t["hoy"], "ayer": t["ayer"], "sem": t["inicio_semana"],
            "mes": t["inicio_mes"], "h7": t["hace_7"], "h30": t["hace_30"],
        },
        fetchone=True,
    ) or [0] * 17

    def par(a, b):
        return {"total": int(fila[a] or 0), "unicos": int(fila[b] or 0)}

    return {
        "hoy": par(0, 1),
        "ayer": par(2, 3),
        "esta_semana": par(4, 5),
        "este_mes": par(6, 7),
        "ultimos_7_dias": par(8, 9),
        "ultimos_30_dias": par(10, 11),
        "total": par(12, 13),
        "por_origen": {
            "frase": int(fila[14] or 0),
            "referral": int(fila[15] or 0),
            "ambos": int(fila[16] or 0),
        },
        "frase_objetivo": _frase_objetivo(),
        "actualizado": t["ahora"].strftime("%Y-%m-%d %H:%M:%S"),
    }


def serie(periodo="dia", limite=14):
    """Serie temporal para gráficos.
       periodo='dia'  -> últimos `limite` días (rellena los días sin consultas con 0)
       periodo='semana' -> últimas `limite` semanas
       periodo='mes'  -> últimos `limite` meses
    """
    limite = max(1, min(_int(limite, 14), 366))
    t = _limites_de_tiempo()

    if periodo == "semana":
        desde = t["inicio_semana"] - timedelta(weeks=limite - 1)
        rows = _execute_db_query(
            "SELECT date_trunc('week', fecha) AS b, COUNT(DISTINCT (telefono, fecha::date)), COUNT(DISTINCT telefono) "
            "FROM consultas_publicidad WHERE fecha >= %s GROUP BY b ORDER BY b",
            (desde,), fetchall=True,
        ) or []
        return [{"periodo": b.strftime("%Y-%m-%d"), "total": int(c or 0), "unicos": int(u or 0)}
                for (b, c, u) in rows]

    if periodo == "mes":
        # Retrocedemos exactamente (limite-1) meses con aritmética de meses (no restando días,
        # que por los meses de 28/30/31 podía meter una barra de más).
        idx = (t["inicio_mes"].year * 12 + (t["inicio_mes"].month - 1)) - (limite - 1)
        desde = datetime(idx // 12, idx % 12 + 1, 1)
        rows = _execute_db_query(
            "SELECT date_trunc('month', fecha) AS b, COUNT(DISTINCT (telefono, fecha::date)), COUNT(DISTINCT telefono) "
            "FROM consultas_publicidad WHERE fecha >= %s GROUP BY b ORDER BY b",
            (desde,), fetchall=True,
        ) or []
        return [{"periodo": b.strftime("%Y-%m"), "total": int(c or 0), "unicos": int(u or 0)}
                for (b, c, u) in rows]

    # periodo == 'dia' (por defecto): relleno los días sin datos con 0.
    # Dentro de un mismo día, "1 por persona por día" = teléfonos distintos de ese día,
    # así que total y unicos coinciden en la vista diaria.
    desde = t["hoy"] - timedelta(days=limite - 1)
    rows = _execute_db_query(
        "SELECT fecha::date AS d, COUNT(DISTINCT telefono), COUNT(DISTINCT telefono) "
        "FROM consultas_publicidad WHERE fecha >= %s GROUP BY d ORDER BY d",
        (desde,), fetchall=True,
    ) or []
    por_dia = {}
    for (d, c, u) in rows:
        clave = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        por_dia[clave] = (int(c or 0), int(u or 0))
    salida = []
    for i in range(limite):
        dia = (desde + timedelta(days=i)).strftime("%Y-%m-%d")
        c, u = por_dia.get(dia, (0, 0))
        salida.append({"periodo": dia, "total": c, "unicos": u})
    return salida


def ultimas_consultas(limite=50):
    limite = max(1, min(_int(limite, 50), 500))
    # Una fila por persona por día (su PRIMER mensaje de ese día, que es el que trae el dato
    # del anuncio), coherente con el conteo "1 por persona por día".
    rows = _execute_db_query(
        "SELECT fecha, telefono, origen, ref_titular, ref_id, texto FROM ("
        "  SELECT DISTINCT ON (telefono, fecha::date) fecha, telefono, origen, ref_titular, ref_id, texto "
        "  FROM consultas_publicidad ORDER BY telefono, fecha::date, fecha ASC"
        ") q ORDER BY fecha DESC LIMIT %s",
        (limite,), fetchall=True,
    ) or []
    out = []
    for (fecha, tel, origen, titular, ref_id, texto) in rows:
        out.append({
            "fecha": fecha.strftime("%Y-%m-%d %H:%M") if hasattr(fecha, "strftime") else str(fecha),
            "telefono": tel or "",
            "origen": origen or "",
            "anuncio": titular or "",
            "ad_id": ref_id or "",
            "texto": (texto or "")[:200],
        })
    return out


# ==========================================================================================
# ENDPOINTS JSON / CSV
# ==========================================================================================
@bp.route("/publicidad/resumen", methods=["GET"])
def _ep_resumen():
    try:
        return jsonify(resumen()), 200
    except Exception as e:
        print(f"[publicidad] /publicidad/resumen: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


@bp.route("/publicidad/serie", methods=["GET"])
def _ep_serie():
    try:
        periodo = request.args.get("periodo", "dia")
        limite = request.args.get("limite", 14)
        return jsonify(serie(periodo, limite)), 200
    except Exception as e:
        print(f"[publicidad] /publicidad/serie: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


@bp.route("/publicidad/consultas", methods=["GET"])
def _ep_consultas():
    try:
        limite = request.args.get("limite", 50)
        return jsonify(ultimas_consultas(limite)), 200
    except Exception as e:
        print(f"[publicidad] /publicidad/consultas: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


@bp.route("/publicidad.csv", methods=["GET"])
def _ep_csv():
    try:
        rows = _execute_db_query(
            "SELECT fecha, telefono, origen, ref_fuente, ref_id, ref_titular, ctwa_clid, texto FROM ("
            "  SELECT DISTINCT ON (telefono, fecha::date) fecha, telefono, origen, ref_fuente, ref_id, ref_titular, ctwa_clid, texto "
            "  FROM consultas_publicidad ORDER BY telefono, fecha::date, fecha ASC"
            ") q ORDER BY fecha DESC",
            fetchall=True,
        ) or []
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["fecha", "telefono", "origen", "fuente", "ad_id", "anuncio", "ctwa_clid", "texto"])
        for (fecha, tel, origen, fuente, ref_id, titular, clid, texto) in rows:
            f = fecha.strftime("%Y-%m-%d %H:%M:%S") if hasattr(fecha, "strftime") else str(fecha)
            # Campos de texto (algunos vienen del cliente) pasan por _csv_seguro para evitar
            # que Excel/Sheets interprete un mensaje tipo "=HYPERLINK(...)" como fórmula.
            w.writerow([f, _csv_seguro(tel), _csv_seguro(origen), _csv_seguro(fuente),
                        _csv_seguro(ref_id), _csv_seguro(titular), _csv_seguro(clid),
                        _csv_seguro(texto)])
        salida = buf.getvalue()
        buf.close()
        return Response(
            salida,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=consultas_publicidad.csv"},
        )
    except Exception as e:
        print(f"[publicidad] /publicidad.csv: {e}", flush=True)
        return Response("error", status=500)


# ==========================================================================================
# PANEL WEB EN VIVO
# ==========================================================================================
@bp.route("/publicidad", methods=["GET"])
def _ep_panel_alias():
    return _ep_panel()


@bp.route("/panel_publicidad", methods=["GET"])
def _ep_panel():
    return Response(PANEL_HTML.replace("@@VERSION@@", str(_version)), mimetype="text/html")


PANEL_HTML = r"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WoodTools · Consultas por publicidad</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:#f0f2f5;color:#1c1e21;padding:16px}
.wrap{max-width:1000px;margin:0 auto}
header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:16px}
.marca{font-size:22px;font-weight:800;color:#a41e22}
.marca small{display:block;font-size:12px;font-weight:500;color:#65676b}
.estado{font-size:12px;color:#65676b;text-align:right}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#31a24c;margin-right:5px;vertical-align:middle}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.card{background:#fff;border-radius:14px;padding:18px 16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
.card .lbl{font-size:13px;color:#65676b;text-transform:uppercase;letter-spacing:.4px}
.card .num{font-size:40px;font-weight:800;line-height:1.1;margin-top:6px;color:#a41e22}
.card .sub{font-size:12px;color:#8a8d91;margin-top:4px}
.card.destacada{background:#a41e22;color:#fff}
.card.destacada .lbl,.card.destacada .sub{color:rgba(255,255,255,.85)}
.card.destacada .num{color:#fff}
.panel{background:#fff;border-radius:14px;padding:18px 16px;box-shadow:0 1px 3px rgba(0,0,0,.1);margin-bottom:16px}
.panel h2{font-size:15px;margin-bottom:14px;color:#1c1e21}
.chart{display:flex;align-items:flex-end;gap:6px;height:180px;padding-top:10px}
.bar{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%}
.bar .b{width:100%;background:#a41e22;border-radius:5px 5px 0 0;min-height:2px;transition:height .3s;position:relative}
.bar .b:hover{background:#c62828}
.bar .v{font-size:11px;font-weight:700;color:#a41e22;margin-bottom:3px;height:14px}
.bar .d{font-size:10px;color:#8a8d91;margin-top:5px;white-space:nowrap}
.chart.linea{display:block;height:auto;align-items:stretch;padding-top:0}
.chart.linea svg{width:100%;height:auto;display:block}
.chart.linea .dot{transition:r .15s}
.chart.linea .dot:hover{r:5.5}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid #eceef0}
th{font-size:11px;text-transform:uppercase;letter-spacing:.3px;color:#8a8d91}
td.tel{font-weight:600}
.tag{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px}
.tag.referral{background:#e7f3ff;color:#1877f2}
.tag.frase{background:#e6f4ea;color:#1e7e34}
.tag.ambos{background:#fdecea;color:#a41e22}
.vacio{color:#8a8d91;text-align:center;padding:24px;font-size:14px}
.controls{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.controls button{border:1px solid #d0d2d6;background:#fff;border-radius:20px;padding:6px 14px;font-size:13px;cursor:pointer;color:#1c1e21}
.controls button.on{background:#a41e22;color:#fff;border-color:#a41e22}
.foot{text-align:center;font-size:11px;color:#b0b3b8;margin-top:8px}
a.csv{font-size:12px;color:#a41e22;text-decoration:none;font-weight:600}
</style></head>
<body><div class="wrap">
<header>
  <div class="marca">🪵 WoodTools <small>Consultas que entran por publicidad (WhatsApp)</small></div>
  <div class="estado"><span class="dot"></span><span id="estado">Conectando…</span><br>
     <span id="frase" style="color:#b0b3b8"></span></div>
</header>

<div class="cards" id="cards"></div>

<div class="panel">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
    <h2 style="margin:0">Evolución</h2>
    <div class="controls" id="controls">
      <button data-p="dia" data-l="14" class="on">14 días</button>
      <button data-p="dia" data-l="30">30 días</button>
      <button data-p="semana" data-l="12">Semanas</button>
      <button data-p="mes" data-l="12">Meses</button>
    </div>
  </div>
  <div class="chart" id="chart"></div>
</div>

<div class="panel">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
    <h2 style="margin:0">Últimas consultas</h2>
    <a class="csv" href="/publicidad.csv">⬇ Descargar CSV</a>
  </div>
  <div id="tabla"></div>
</div>

<div class="foot">Se actualiza solo cada 30 s · versión conocimiento @@VERSION@@</div>
</div>

<script>
var periodo = "dia", limite = 14;

function esc(s){return (s==null?"":String(s)).replace(/[&<>"]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c];});}

function pintarCards(r){
  var c = [
    {lbl:"Hoy", d:r.hoy, destacada:true},
    {lbl:"Esta semana", d:r.esta_semana},
    {lbl:"Este mes", d:r.este_mes},
    {lbl:"Ayer", d:r.ayer},
    {lbl:"Últimos 30 días", d:r.ultimos_30_dias},
    {lbl:"Total histórico", d:r.total}
  ];
  document.getElementById("cards").innerHTML = c.map(function(x){
    return '<div class="card'+(x.destacada?' destacada':'')+'">'+
      '<div class="lbl">'+x.lbl+'</div>'+
      '<div class="num">'+(x.d?x.d.total:0)+'</div>'+
      '<div class="sub">'+(x.d?x.d.unicos:0)+' contactos distintos</div></div>';
  }).join("");
  document.getElementById("frase").textContent = 'Frase: "'+esc(r.frase_objetivo)+'"';
}

function etiquetaDia(p){ var a=p.split("-"); return a[2]+"/"+a[1]; }

function pintarChart(serie){
  // La vista diaria (14/30 días) se dibuja como gráfico de LÍNEAS; semanas/meses como barras.
  if(periodo==="dia") pintarLinea(serie);
  else pintarBarras(serie);
}

function techo(m){
  m = Math.max(m, 1);
  var mag = Math.pow(10, Math.floor(Math.log10(m)));
  var cand = [1,2,2.5,5,10].map(function(x){return x*mag;});
  for(var i=0;i<cand.length;i++){ if(cand[i] >= m) return cand[i]; }
  return 10*mag;
}

function pintarLinea(serie){
  var cont = document.getElementById("chart");
  cont.className = "chart linea";
  if(!serie || !serie.length){ cont.innerHTML = '<div class="vacio">Sin datos todavía</div>'; return; }
  var n = serie.length;
  var W=720, H=240, L=34, R=14, T=22, B=26;
  var pw = W-L-R, ph = H-T-B, base = T+ph;
  var maxv = Math.max.apply(null, serie.map(function(s){return s.total;}));
  var top = techo(maxv);
  var X = function(i){ return n===1 ? L+pw/2 : L + i*pw/(n-1); };
  var Y = function(v){ return T + ph*(1 - v/top); };
  // Elegimos ~7 marcas del eje X repartidas parejo, siempre con la primera y la última,
  // así no se solapan las fechas aunque haya muchos puntos (ej. 30 días).
  var ticks = Math.min(n, 7), marcas = {};
  for(var m0=0;m0<ticks;m0++){ marcas[ticks===1 ? 0 : Math.round(m0*(n-1)/(ticks-1))] = true; }

  var g="";
  // grilla horizontal + valores del eje Y
  for(var k=0;k<=4;k++){
    var yk = T + ph*(1-k/4);
    g += '<line x1="'+L+'" y1="'+yk+'" x2="'+(L+pw)+'" y2="'+yk+'" stroke="#eceef0" stroke-width="1"/>';
    g += '<text x="'+(L-6)+'" y="'+(yk+3)+'" text-anchor="end" font-size="10" fill="#b0b3b8">'+Math.round(top*k/4)+'</text>';
  }
  // puntos
  var pts = serie.map(function(s,i){ return X(i)+","+Y(s.total); }).join(" ");
  // área bajo la línea
  var area = "M "+X(0)+" "+base+" L "+serie.map(function(s,i){return X(i)+" "+Y(s.total);}).join(" L ")+" L "+X(n-1)+" "+base+" Z";
  g += '<path d="'+area+'" fill="rgba(164,30,34,.10)"/>';
  g += '<polyline points="'+pts+'" fill="none" stroke="#a41e22" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>';
  // dots + valores + fechas
  serie.forEach(function(s,i){
    var x=X(i), y=Y(s.total);
    g += '<circle class="dot" cx="'+x+'" cy="'+y+'" r="3.5" fill="#a41e22"><title>'+esc(s.periodo)+': '+s.total+' consultas</title></circle>';
    if(s.total>0) g += '<text x="'+x+'" y="'+(y-8)+'" text-anchor="middle" font-size="10" font-weight="700" fill="#a41e22">'+s.total+'</text>';
    if(marcas[i]) g += '<text x="'+x+'" y="'+(base+16)+'" text-anchor="middle" font-size="10" fill="#8a8d91">'+etiquetaDia(s.periodo)+'</text>';
  });
  cont.innerHTML = '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">'+g+'</svg>';
}

function pintarBarras(serie){
  var cont = document.getElementById("chart");
  cont.className = "chart";
  if(!serie || !serie.length){ cont.innerHTML = '<div class="vacio">Sin datos todavía</div>'; return; }
  var max = Math.max.apply(null, serie.map(function(s){return s.total;}));
  if(max <= 0) max = 1;
  cont.innerHTML = serie.map(function(s){
    var h = Math.round((s.total/max)*140);
    var etq = s.periodo;
    if(periodo==="mes"){ var q=s.periodo.split("-"); etq=q[1]+"/"+q[0].slice(2); }
    else if(periodo==="semana"){ var w=s.periodo.split("-"); etq=w[2]+"/"+w[1]; }
    return '<div class="bar" title="'+esc(s.periodo)+': '+s.total+' consultas ('+s.unicos+' contactos)">'+
      '<div class="v">'+(s.total||"")+'</div>'+
      '<div class="b" style="height:'+h+'px"></div>'+
      '<div class="d">'+etq+'</div></div>';
  }).join("");
}

function pintarTabla(rows){
  var cont = document.getElementById("tabla");
  if(!rows || !rows.length){ cont.innerHTML = '<div class="vacio">Todavía no ingresaron consultas por publicidad.</div>'; return; }
  var body = rows.map(function(x){
    var extra = x.anuncio ? esc(x.anuncio) : (x.texto?esc(x.texto):"—");
    return '<tr><td>'+esc(x.fecha)+'</td>'+
      '<td class="tel">'+esc(x.telefono)+'</td>'+
      '<td><span class="tag '+esc(x.origen)+'">'+esc(x.origen)+'</span></td>'+
      '<td>'+extra+'</td></tr>';
  }).join("");
  cont.innerHTML = '<table><thead><tr><th>Fecha</th><th>Teléfono</th><th>Origen</th><th>Anuncio / mensaje</th></tr></thead><tbody>'+body+'</tbody></table>';
}

function cargar(){
  fetch("/publicidad/resumen").then(function(r){return r.json();}).then(function(r){
    pintarCards(r);
    var d = new Date();
    document.getElementById("estado").textContent = "En vivo · "+d.toLocaleTimeString("es-AR");
  }).catch(function(){ document.getElementById("estado").textContent = "Sin conexión"; });

  fetch("/publicidad/serie?periodo="+periodo+"&limite="+limite)
    .then(function(r){return r.json();}).then(pintarChart).catch(function(){});

  fetch("/publicidad/consultas?limite=25")
    .then(function(r){return r.json();}).then(pintarTabla).catch(function(){});
}

document.getElementById("controls").addEventListener("click", function(e){
  if(e.target.tagName!=="BUTTON") return;
  periodo = e.target.getAttribute("data-p");
  limite = parseInt(e.target.getAttribute("data-l"),10);
  var btns = this.querySelectorAll("button");
  for(var i=0;i<btns.length;i++) btns[i].classList.remove("on");
  e.target.classList.add("on");
  cargar();
});

cargar();
setInterval(cargar, 30000);
</script>
</body></html>"""


# ==========================================================================================
# ENCENDIDO (lo llama servidor.py)
# ==========================================================================================
def configurar(app, execute_db_query, hora_arg, limpiar_numero, version="s/d"):
    global _execute_db_query, _hora_arg, _limpiar_numero, _version
    _execute_db_query = execute_db_query
    _hora_arg = hora_arg
    _limpiar_numero = limpiar_numero
    _version = version
    crear_tabla()
    try:
        app.register_blueprint(bp)
    except Exception as e:
        print(f"[publicidad] no se pudo registrar el blueprint (¿ya estaba?): {e}", flush=True)
    print("✅ Módulo de consultas por publicidad activo (/panel_publicidad).", flush=True)
