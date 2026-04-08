import os
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect
import requests
import gspread
from google.oauth2.credentials import Credentials 
from apscheduler.schedulers.background import BackgroundScheduler
import google.generativeai as genai
import json
import psycopg2 
from psycopg2 import pool 
import re 

app = Flask(__name__)

# ==========================================
# CONFIGURACIÓN SEGURA (Inteligente)
# ==========================================
posibles_rutas = [
    "/etc/secrets/tokens.json",
    "/etc/secrets/token.json",
    "tokens.json",
    "token.json"
]

ruta_correcta = None
for ruta in posibles_rutas:
    if os.path.exists(ruta):
        ruta_correcta = ruta
        break

try:
    if ruta_correcta:
        with open(ruta_correcta, 'r') as f:
            credenciales_api = json.load(f)
    else:
        credenciales_api = {}
        print("⚠️ No se encontró ningún archivo de tokens. Se intentará usar Variables de Entorno.")
        
    TOKEN_DE_VERIFICACION = credenciales_api.get("TOKEN_DE_VERIFICACION", "")
    CLOUD_API_TOKEN = credenciales_api.get("CLOUD_API_TOKEN", "")
    PHONE_NUMBER_ID = credenciales_api.get("PHONE_NUMBER_ID", "")
    GEMINI_API_KEY = credenciales_api.get("GEMINI_API_KEY", "")
    DATABASE_URL = credenciales_api.get("DATABASE_URL", os.environ.get("DATABASE_URL", ""))
    
    if not DATABASE_URL:
        print("❌ ERROR FATAL: No se detectó la DATABASE_URL de Neon. Revisa tu archivo json en Render.")
        
except Exception as e:
    print(f"⚠️ ATENCIÓN: Error procesando credenciales: {e}")
    TOKEN_DE_VERIFICACION = CLOUD_API_TOKEN = PHONE_NUMBER_ID = GEMINI_API_KEY = DATABASE_URL = ""

genai.configure(api_key=GEMINI_API_KEY)
NOMBRE_HOJA = "Base de datos wt"
RUTA_CREDENCIALES = "/etc/secrets/credenciales.json" if os.path.exists("/etc/secrets/credenciales.json") else "credenciales.json"

# ==========================================
# MAGIA ANTI-CHOQUES: POOL DE CONEXIONES A LA NUBE
# ==========================================
db_pool = None
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL, sslmode='require')
    if db_pool:
        print("✅ Pool de conexiones a PostgreSQL creado exitosamente.")
except Exception as e:
    print(f"❌ Error al conectar a PostgreSQL: {e}")

def execute_db_query(query, params=(), commit=False, fetchone=False, fetchall=False, retries=1):
    if not db_pool:
        print("❌ No hay pool de conexiones disponible.")
        return None
        
    for attempt in range(retries + 1):
        conn = None
        try:
            conn = db_pool.getconn() 
            res = None
            with conn.cursor() as c:
                c.execute(query, params)
                if commit:
                    conn.commit()
                if fetchone:
                    res = c.fetchone()
                elif fetchall:
                    res = c.fetchall()
                else:
                    res = c.rowcount
            
            db_pool.putconn(conn)
            return res
            
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            if conn: db_pool.putconn(conn, close=True)
            if attempt == retries: return None
        except Exception as e:
            print(f"❌ Error en DB ejecutando '{query}': {e}")
            if conn:
                conn.rollback()
                db_pool.putconn(conn)
            return None

def init_db():
    try:
        execute_db_query('''CREATE TABLE IF NOT EXISTS mensajes (id TEXT PRIMARY KEY, telefono TEXT, estado TEXT, fecha TIMESTAMP)''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS chat_sesiones (telefono TEXT PRIMARY KEY, historial TEXT, ultima_interaccion TIMESTAMP)''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS asignaciones_v2 (telefono_cliente TEXT PRIMARY KEY, numero_vendedor TEXT, tipo_campana TEXT, subtipo TEXT, tanda_id TEXT)''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS metricas_campanas (tanda_id TEXT PRIMARY KEY, entregados INTEGER DEFAULT 0, leidos INTEGER DEFAULT 0, respondidos INTEGER DEFAULT 0, derivados INTEGER DEFAULT 0)''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS tracking_metricas (tanda_id TEXT, telefono TEXT, evento TEXT, PRIMARY KEY(tanda_id, telefono, evento))''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS chats_derivados (telefono TEXT PRIMARY KEY, vendedor TEXT, historial TEXT, fecha TIMESTAMP)''', commit=True)
        
        try: execute_db_query("ALTER TABLE chat_sesiones ADD COLUMN advertido INTEGER DEFAULT 0", commit=True)
        except Exception: pass 
        
        try: execute_db_query("ALTER TABLE metricas_campanas ADD COLUMN derivados INTEGER DEFAULT 0", commit=True)
        except Exception: pass 
        
        try: execute_db_query("ALTER TABLE asignaciones_v2 ADD COLUMN fecha_asignacion TIMESTAMP", commit=True)
        except Exception: pass 
        
        try: execute_db_query("INSERT INTO metricas_campanas (tanda_id, entregados, leidos, respondidos, derivados) VALUES ('ORGANICO', 0, 0, 0, 0) ON CONFLICT (tanda_id) DO NOTHING", commit=True)
        except Exception: pass
            
    except Exception as e:
        print(f"Error crítico iniciando tablas: {e}")

init_db()

# ==========================================
# FUNCIONES BÁSICAS Y DE ENVÍO
# ==========================================
def limpiar_numero(num):
    return ''.join(filter(str.isdigit, str(num)))

def extraer_10_digitos(num):
    solo_numeros = limpiar_numero(num)
    return solo_numeros[-10:] if len(solo_numeros) >= 10 else solo_numeros

def enviar_mensaje_whatsapp(telefono_destino, texto, link_boton=None):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {CLOUD_API_TOKEN}", "Content-Type": "application/json"}
    
    if link_boton:
        data = {
            "messaging_product": "whatsapp",
            "to": telefono_destino,
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "body": { "text": texto },
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": "Hablar con asesor",
                        "url": link_boton
                    }
                }
            }
        }
    else:
        data = {"messaging_product": "whatsapp", "to": telefono_destino, "type": "text", "text": {"body": texto}}
        
    res = requests.post(url, headers=headers, json=data)
    
    if res.status_code >= 400 and link_boton:
        texto_fallback = f"{texto}\n\n👉 {link_boton}"
        data_fallback = {"messaging_product": "whatsapp", "to": telefono_destino, "type": "text", "text": {"body": texto_fallback}}
        requests.post(url, headers=headers, json=data_fallback)

def registrar_metrica(evento, telefono):
    try:
        tel_10 = extraer_10_digitos(telefono)
        res = execute_db_query("SELECT tanda_id FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), fetchone=True)
        
        if res and res[0]:
            t_id = res[0]
            execute_db_query("""
                INSERT INTO tracking_metricas (tanda_id, telefono, evento) 
                VALUES (%s, %s, %s) 
                ON CONFLICT (tanda_id, telefono, evento) DO NOTHING
            """, (t_id, tel_10, evento), commit=True)
            
            if evento == 'delivered':
                execute_db_query("UPDATE metricas_campanas SET entregados = entregados + 1 WHERE tanda_id = %s", (t_id,), commit=True)
            elif evento == 'read':
                execute_db_query("UPDATE metricas_campanas SET leidos = leidos + 1 WHERE tanda_id = %s", (t_id,), commit=True)
            elif evento == 'responded':
                execute_db_query("UPDATE metricas_campanas SET respondidos = respondidos + 1 WHERE tanda_id = %s", (t_id,), commit=True)
                
    except Exception as e: print(f"Error métricas: {e}")

def bloquear_numero_en_sheets(telefono):
    try:
        if not os.path.exists(RUTA_CREDENCIALES): return False
        gc = gspread.service_account(filename=RUTA_CREDENCIALES)
        sh = gc.open(NOMBRE_HOJA)
        
        for ws in sh.worksheets():
            try:
                celda = ws.find(telefono)
                if celda:
                    ws.update_cell(celda.row, celda.col, f"0000{telefono}")
                    return True
            except gspread.exceptions.CellNotFound: continue
    except Exception as e: print(f"❌ Error conectando a Sheets: {e}", flush=True)

# ==========================================
# RUTINAS AISLADAS CON POOL
# ==========================================
def revisar_rutinas_de_tiempo():
    try:
        ahora = datetime.now()
        
        hace_48_horas = ahora - timedelta(hours=48)
        para_borrar = execute_db_query("SELECT id, telefono FROM mensajes WHERE estado='sent' AND fecha < %s", (hace_48_horas,), fetchall=True)
        if para_borrar:
            for msg_id, telefono in para_borrar:
                bloquear_numero_en_sheets(telefono)
                execute_db_query("DELETE FROM mensajes WHERE id=%s", (msg_id,), commit=True)
                
        execute_db_query("DELETE FROM asignaciones_v2 WHERE (fecha_asignacion < %s OR fecha_asignacion IS NULL) AND telefono_cliente NOT IN (SELECT telefono FROM chat_sesiones)", (hace_48_horas,), commit=True)
            
        hace_1_hora = ahora - timedelta(hours=1)
        para_derivar = execute_db_query("SELECT telefono, historial FROM chat_sesiones WHERE ultima_interaccion < %s", (hace_1_hora,), fetchall=True)
        if para_derivar:
            for telefono, historial_str in para_derivar:
                try:
                    tel_10 = extraer_10_digitos(telefono)
                    res_vend = execute_db_query("SELECT numero_vendedor, tipo_campana FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), fetchone=True)
                    
                    vendedor_asignado = res_vend[0] if res_vend else "Sin asignar"
                    campana = res_vend[1] if res_vend else "Contacto Orgánico"
                    
                    execute_db_query("""
                        INSERT INTO chats_derivados (telefono, vendedor, historial, fecha) 
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (telefono) DO UPDATE SET historial=EXCLUDED.historial, fecha=EXCLUDED.fecha
                    """, (telefono, vendedor_asignado, historial_str, datetime.now()), commit=True)

                    historial = json.loads(historial_str)
                    ultimo_msg_cliente = "Sin mensajes recientes."
                    for msg in reversed(historial):
                        if msg.get("role") == "user" and "CONTEXTO" not in msg.get("parts", [""])[0]:
                            ultimo_msg_cliente = msg["parts"][0]
                            break

                    aviso_asesor = (
                        f"🤖 *AVISO DEL BOT AUTOMÁTICO*\n\n"
                        f"El cliente con número +{telefono} ingresó por la campaña *{campana}*, pero el chat expiró tras 1 hora de inactividad.\n\n"
                        f"💬 *Último mensaje del cliente:*\n\"{ultimo_msg_cliente}\"\n\n"
                        f"👉 *Acción requerida:* Por favor, revisa el panel de 'Chats Abandonados' en el sistema y contactalo directamente."
                    )
                    
                    if vendedor_asignado and vendedor_asignado != "Sin asignar" and vendedor_asignado != "5491145394279": 
                        enviar_mensaje_whatsapp(vendedor_asignado, aviso_asesor)
                    else:
                        enviar_mensaje_whatsapp("5491145394279", aviso_asesor)

                    aviso_cliente = "⚠️ ¡Hola! Como pasó 1 hora de inactividad, cerramos esta conversación automática. Tu asesor asignado se pondrá en contacto con vos a la brevedad para continuar atendiéndote de forma personalizada. ¡Gracias!"
                    enviar_mensaje_whatsapp(telefono, aviso_cliente)

                    execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (telefono,), commit=True)
                    execute_db_query("DELETE FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), commit=True)
                except Exception as inner_e:
                    print(f"Fallo derivando chat {telefono}: {inner_e}")

    except Exception as e:
        print(f"Error general en rutinas de tiempo: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(func=revisar_rutinas_de_tiempo, trigger="interval", minutes=5)
scheduler.start()

# ==========================================
# CEREBRO IA: LÓGICA CONDICIONAL Y ORGÁNICA (EXPERTO TÉCNICO)
# ==========================================
BASE_CONOCIMIENTO = """
=== BASE DE CONOCIMIENTO TÉCNICO ===
Eres un asesor profesional sobre carpintería. Destácate por dar consejos para que las personas compren herramientas de la mejor calidad. Ofrece opciones de gran calidad/alto precio (como Freud) y opciones más económicas de excelente calidad profesional (como Franzoi o línea WoodTools). Aclara siempre que todas son de calidad profesional.
MARCAS PERMITIDAS: Solo brinda información de herramientas WoodTools, Freud o Franzoi.
PRECIOS Y PROMOCIONES: TIENES ESTRICTAMENTE PROHIBIDO DAR PRECIOS, INVENTAR DESCUENTOS O EXPLICAR PROMOCIONES BAJO NINGÚN CONCEPTO. Si te preguntan por precios o promociones, diles amablemente que no manejas los valores comerciales y redirígelos INMEDIATAMENTE al chat de WhatsApp con el asesor para que les arme un presupuesto a medida.

* SIERRAS CIRCULARES (Discos de corte):
  - Al ofrecer discos, pregunta qué material cortan. 
  - Si es MELAMINA, pregunta si usa ángulo positivo o negativo.
    - Ángulo Positivo (Para máquinas industriales con incisor): Son la mejor opción para máquinas industriales. Códigos: LG3D 0400 (Freud, Ext:250mm, Esp:3.2mm, Int:30mm, HM Widia), LG3D 0600 (Freud, Ext:300mm, Esp:3.2mm, Int:30mm, HM Widia), SSK12 001. Otros: LG3D 0400/LI25M31FA3, LU3D 0600/LI25M 31FA3, LU3D 0200 (Ext:220mm), FREUD Línea Wood Tools (Ext:220mm). El incisor para melamina es LI25M31FA3 (Freud, Ext:125mm, Esp:3.1mm, Int:20mm).
    - Ángulo Negativo (Para usar sin incisor en máquinas de banco): Códigos: LU3F 0200 o LU3F-0200 250 Z80 (Freud, Ext:250mm, Esp:3.2mm, Int:30mm, Widia), LU3F 0300 (Freud, Ext:300mm, HM Widia, Aglom/MDF/Madera/Melamina), FR12L001H (Freud, Ext:185mm, Esp:2.4mm, Int:20mm), LU3E 0200 (Freud, Ext:250mm), SSK3F 0300, F03FS09801 (Freud, Ext:185mm, Esp:2.4mm, Int:20mm). LU3F 0400 (Ext:350mm).
  - Si es MADERA: Son para todo tipo de máquinas (industriales y de carpinteros). Códigos: LG2A 2100 (Freud, Ext:300mm, diente alterno), LG2B 1100 (Freud, 300mm), LG2A 1700 (Freud, 250mm), SC4505204F (Franzoi, 450mm, Esp:5.1mm, tirantería), SC3004164F (Franzoi, 300mm, Esp:4mm, tirantería), LG2A 2800 (Freud, 350mm, maciza/blanda/dura), LU2A 1600 (Freud, 250mm, a favor veta), LU1D 0500, LU2A 2500 (Freud, 350mm, a favor/contra veta), SC35045244F, LU2B 0700 (Freud, 250mm), SC4504248F (Franzoi, 450mm), LU2C 2000 (Freud, 350mm), LU2A 0700, LU2B 1600, LU2B 1900 (Freud, 400mm), LU2C 1200 (Freud, 250mm), LU2C 1500 (Freud, 300mm), LU2A 3100 (Freud, 400mm), LU2A 0800 (Freud, 200mm), LU2A 3300 (Freud, 400mm), FI14M AA3 (Freud, 150mm, Esp:1.5mm), LU2B 2100 (Freud, 500mm, Esp:4.4mm), LU2B 0200 (Freud, 180mm, Int:40mm), LU2A 0500 (Freud, 180mm). SC60055244F (Franzoi, 600mm, máquinas múltiples).

* FRESAS (Router/Tupí):
  - Pregunta qué busca hacer.
  - Canales o Ranuras: Fresas rectas (Códigos empiezan con FRS o FRG). Ej: FRS0054/1006 (Fresas Rectas HM, D:150mm, B:5-100mm, d:40mm, Z:4/6), FRSI01542/10066 (Con Incisores HM, B:15-100mm, Incisores:2-6), FRG0510 y FRG1039 (Ranurar Regulables HM, B:5-10mm o 10-39mm).
  - Cepillado: Códigos empiezan con CB. Ej: CB0500640 hasta CB22012100 (Cabezales Cepilladores HM, D:125mm, d:40mm, B:55 a 220mm).
  - Angulares: Códigos empiezan con FA. Ej: FA104/506 (Fresas en ángulo HM, D:150mm, B:10-50mm, d:40mm).
  - Moldura: Códigos F04C0, F2C, FZS, FR104/156, JFRD, JFFI, JFMS, JFMD, JFMP, JFMP3416G, JFMP34166M, JFDE, JFDSG, FRP5533, JFMPV14, FCPV, JFMPVR, JPMS10, FP402. Ejemplos: 1/4 círculo cóncavo/convexo (F04C014...), 1/2 círculo (F2C014...), Zócalo/Contramarco (FZS128, FZS129), Rinconera simple/doble (FR104/156, JFRD), Frente Inglés (JFFI01/05), Machimbre simple/doble (JFMS1234, JFMD1234), Machimbre piso (JFMP3411, con grampa JFMP3416G, con microbisel JFMP34166M), Deck (JFDE4, JFDSG14), Replán tablero (FRP5533, D:200mm, B:55mm), Molduras puertas/ventanas (JFMPV14, FCPV41, JFMPVR, JFPMS10), Multimoldura (FP402, D:150mm, B:45mm).
  - Encastre/Uniones: Códigos JFE, FG46S. Ej: Fresa para Finger HM (JFE254 para 22mm, JFE5022 para 45mm), Ensamble Cónico (JFE8122, JFE8121), Encastre 90º/180º (JFE8Z122, JFE8Z124, JFME68), Fresa para Finger HS (FG46S CB2, D:160mm, Acero HSS).
  - Radiales: Códigos empiezan con FRM04. Ej: Fresa para Radios Múltiples HM (FMR04, D:140mm, B:35mm).

* MECHAS:
  - Pregunta qué quiere hacer.
  - Perforaciones Pasantes (Atraviesan la madera): Códigos empiezan con MPD y MPI (Fresa Italiana, metal duro, vástago 10mm, diámetros: 3 a 15mm).
  - Perforaciones Ciegas (No pasantes): Códigos empiezan con MCD y MCI (Fresa Italiana, metal duro, vástago 10mm, diámetros: 3 a 15mm).
  - Bisagras: Códigos empiezan con MBD y MBI (Fresa Bisagra Italiana, Widia, vástago 10mm, diámetros: 12 a 40mm).
  - Cortar Melamina (Mesa Nesting): CNC NESTING (Carburo de tungsteno, vástago 10mm, diámetro 8mm). Puede usarse para consultar largo/ancho.

* CUCHILLAS (Insertos de corte):
  - Pregunta si son planos o para moldear.
  - Para cepillar (Planos): Ofrecer "Insertos de corte planas para cepillar" (Acero rápido HSS). Modelo CHC050420HSS. Medidas transversales: 30mm y 35mm. Largos desde 100mm hasta 1080mm.
  - Para moldear (Dorso ranurado): Ofrecer "Insertos para cepillado de dorso ranurado" (Italiana). Modelos CHCR0100404 (40x4mm), CHCR0100505 (50x4mm), CHCR0100604 (60x4mm). Largos disponibles: 25 a 650mm.
"""

def obtener_prompt_personalizado(telefono_cliente_completo):
    tel_10_digitos = extraer_10_digitos(telefono_cliente_completo)
    res = execute_db_query("SELECT numero_vendedor, tipo_campana, subtipo, tanda_id FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10_digitos,), fetchone=True)

    es_organico = not res
    tanda_id = "ORGANICO" if es_organico else (res[3] if res and len(res) > 3 else "TANDA_DESCONOCIDA")
    tipo_camp = "Contacto Orgánico" if es_organico else (res[1] if res else "Promociones")

    # Obtención de nombres y vendedores
    mapa_nombres = {
        "5491145394279": "Valentín", "5491157528428": "Emmanuel", "5491134811771": "Ariel",
        "5491165630406": "Carlos", "5491164591316": "Roberto Golik", "5491157528427": "Nicolas Saad",
        "5491153455274": "Ezequiel Calvi", "5491156321012": "Alan Calvi", "5491168457778": "Luis Quevedo"
    }

    if not es_organico:
        numero_db = res[0] if res[0] not in ["0", "", "Sin asignar", None] else None
        if numero_db and numero_db in mapa_nombres:
            nombre_vendedor_ia = mapa_nombres[numero_db]
            tel_vend = numero_db
        else:
            nombre_vendedor_ia = "tu asesor"
            tel_vend = "5491145394279"
            
        texto_contexto = f"""CONTEXTO DE LA CAMPAÑA: El cliente está respondiendo a la campaña "{tipo_camp}".
EL VENDEDOR ASIGNADO: El vendedor es {nombre_vendedor_ia} ({tel_vend}). Si el nombre es "tu asesor", no inventes nombres propios."""
    else:
        nombre_vendedor_ia = "[Elegido_por_ti]"
        tel_vend = "[Tel_Elegido]"
        texto_contexto = """CONTEXTO: Este es un cliente "Orgánico" (nos contactó por su cuenta). No tiene vendedor asignado.
ELECCIÓN DEL ASESOR: Si el cliente ya conoce a un asesor, envíalo con ese. Si el cliente responde que "no sabe", "cualquiera" o "me da igual", ELIGE TÚ UN ASESOR AL AZAR EXCLUSIVAMENTE ENTRE: Carlos (5491165630406), Valentín (5491145394279) o Emmanuel (5491157528428)."""

    return f"""
Eres el asistente virtual de recepción de WoodTools. Habla en español argentino (usa 'vos', empático y servicial).
Usa formato de WhatsApp (*negritas* y emojis), NUNCA uses markdown de asteriscos dobles (**).

{BASE_CONOCIMIENTO}

{texto_contexto}

TU LABOR PRINCIPAL (INDAGACIÓN EXCLUYENTE):
Además de informar sobre los productos usando la Base de Conocimiento, OBLIGATORIAMENTE debes INDAGAR. 
Ve preguntando de manera sutil en la conversación:
- ¿Qué herramienta necesita exactamente?
- ¿Qué materiales quiere cortar o trabajar?
- ¿En qué medida (largo, ancho, diámetro, etc.)?
- ¿Cuál es la máquina que utiliza?
- ¿Qué cantidad (unidades) necesita?
- (Si no tiene vendedor): ¿Con qué asesor comercial prefiere hablar? (Ofrece a Carlos, Valentín o Emmanuel).

TUS REGLAS DE CHARLA (¡ESTRICTAS E INQUEBRANTABLES!):
1. Saluda cordialmente. ESTÁ PROHIBIDO USAR LA PALABRA "CAMPAÑA".
2. ESTÁ TERMINANTEMENTE PROHIBIDO generar el enlace final de WhatsApp si el cliente aún no te ha respondido los datos básicos (herramienta, material y cantidad). Debes preguntar primero.
3. REGLA DE HIERRO SOBRE PRECIOS Y PROMOCIONES: TIENES ESTRICTAMENTE PROHIBIDO DAR PRECIOS, PORCENTAJES DE DESCUENTO O DETALLES DE PROMOCIONES. Si el cliente pregunta "¿cuánto cuesta?" o "¿qué promociones hay?", dile amablemente que no manejas los valores comerciales y derívalo INMEDIATAMENTE al asesor enviando el enlace. Todo lo comercial lo manejará el vendedor.
4. NO respondes NADA que salga de tu objetivo técnico de WoodTools. NO hablas de envíos.
5. CIERRE Y DERIVACIÓN: SOLO DESPUÉS de que hayas recolectado toda la información (Código/Modelo de la herramienta, detalles del material/máquina y cantidad de unidades), O si el cliente insiste en pedir precios, despídete cordialmente y mándale el enlace EXACTO de WhatsApp.

FORMATO DEL ENLACE AL FINAL (¡Súper Estricto!):
El enlace debe contener la información recolectada.
- Reemplaza [TELEFONO_ASESOR] con el número exacto del asesor asignado o elegido por ti.
- Reemplaza [CODIGO], [INFO] y [CANTIDAD] con los datos reales que descubriste en la charla.
- Codifica todos los espacios del texto con '%20'.
Cuando te toque despedirte, el enlace EXACTO debe ir AL FINAL de tu mensaje, separado por un espacio, así:
https://woodtools-webhook.onrender.com/wa/{tanda_id}/{tel_10_digitos}/[TELEFONO_ASESOR]?text=Hola,%20necesito%20cotizar:%20[CODIGO]%20-%20[INFO]%20-%20[CANTIDAD]%20unidades
"""

def procesar_mensaje_con_gemini(telefono_cliente, texto_entrante):
    # -------------------------------------------------------------
    # COMANDO ESPECIAL: RESETEAR PRUEBAS Y GUARDAR EN ABANDONADOS
    # -------------------------------------------------------------
    if texto_entrante.strip().lower() in ["reset", "resetear", "reiniciar"]:
        tel_10 = extraer_10_digitos(telefono_cliente)
        res_hist = execute_db_query("SELECT historial FROM chat_sesiones WHERE telefono = %s", (telefono_cliente,), fetchone=True)
        if res_hist:
            execute_db_query("""
                INSERT INTO chats_derivados (telefono, vendedor, historial, fecha) 
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telefono) DO UPDATE SET historial=EXCLUDED.historial, fecha=EXCLUDED.fecha
            """, (telefono_cliente, "Cerrado por Reset", res_hist[0], datetime.now()), commit=True)
            
        execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (telefono_cliente,), commit=True)
        execute_db_query("DELETE FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), commit=True)
        enviar_mensaje_whatsapp(telefono_cliente, "✅ *Memoria y campaña borrada exitosamente.*\nEl historial quedó registrado en Abandonados y tu número está limpio.\n\nEscribime un 'Hola' para empezar desde cero como un cliente Orgánico.")
        return

    resultado = execute_db_query("SELECT historial, ultima_interaccion FROM chat_sesiones WHERE telefono = %s", (telefono_cliente,), fetchone=True)
    tel_10 = extraer_10_digitos(telefono_cliente)
    
    if resultado:
        historial_str = resultado[0]
        ultima_interaccion = resultado[1]
        if ultima_interaccion and datetime.now() - ultima_interaccion > timedelta(hours=1):
            res_vend = execute_db_query("SELECT numero_vendedor FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), fetchone=True)
            vendedor_asignado = res_vend[0] if res_vend else "Sin asignar"
            execute_db_query("""
                INSERT INTO chats_derivados (telefono, vendedor, historial, fecha) 
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telefono) DO UPDATE SET historial=EXCLUDED.historial, fecha=EXCLUDED.fecha
            """, (telefono_cliente, vendedor_asignado, historial_str, datetime.now()), commit=True)
            
            execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (telefono_cliente,), commit=True)
            execute_db_query("DELETE FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), commit=True)
            resultado = None  
            
    res_tanda = execute_db_query("SELECT tanda_id FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), fetchone=True)
    tanda_id_actual = res_tanda[0] if (res_tanda and res_tanda[0]) else "ORGANICO"
    
    if not resultado and tanda_id_actual == "ORGANICO":
        execute_db_query("UPDATE metricas_campanas SET respondidos = respondidos + 1 WHERE tanda_id = 'ORGANICO'", commit=True)
    
    prompt_dinamico = obtener_prompt_personalizado(telefono_cliente)
    
    if resultado:
        historial = json.loads(resultado[0])
        if len(historial) > 0 and historial[0]["role"] == "user":
            historial[0]["parts"] = [prompt_dinamico]
    else:
        historial = [
            {"role": "user", "parts": [prompt_dinamico]},
            {"role": "model", "parts": ["Entendido. Aplicaré las reglas de forma hiper estricta."]}
        ]
        
    historial.append({"role": "user", "parts": [texto_entrante]})
    
    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
        chat = model.start_chat(history=historial[:-1])
        respuesta = chat.send_message(texto_entrante)
        
        texto_respuesta = respuesta.text
        texto_limpio = texto_respuesta
        link_extraido = None
        
        match = re.search(r'(https://woodtools-webhook\.onrender\.com/wa/\S+)', texto_respuesta)
        if match:
            link_extraido = match.group(1)
            texto_limpio = texto_respuesta.replace(link_extraido, "").strip()
            texto_limpio = texto_limpio.replace("👉", "").replace("Hacé clic en este enlace para hablar con él", "").strip()
        
        if link_extraido or "Te voy a derivar con" in texto_respuesta:
            vendedor_asignado = "Orgánico / Asignado por IA" if tanda_id_actual == "ORGANICO" else "Vendedor de Campaña"
            
            historial.append({"role": "model", "parts": [texto_respuesta]})
            
            execute_db_query("""
                INSERT INTO chats_derivados (telefono, vendedor, historial, fecha) 
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telefono) DO UPDATE SET historial=EXCLUDED.historial, fecha=EXCLUDED.fecha
            """, (telefono_cliente, vendedor_asignado, json.dumps(historial), datetime.now()), commit=True)
            
            execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (telefono_cliente,), commit=True)
            execute_db_query("DELETE FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), commit=True)
        else:
            historial.append({"role": "model", "parts": [texto_respuesta]})
            execute_db_query("""
                INSERT INTO chat_sesiones (telefono, historial, ultima_interaccion, advertido) 
                VALUES (%s, %s, %s, 0) 
                ON CONFLICT (telefono) 
                DO UPDATE SET historial = EXCLUDED.historial, ultima_interaccion = EXCLUDED.ultima_interaccion, advertido = 0
            """, (telefono_cliente, json.dumps(historial), datetime.now()), commit=True)
            
        enviar_mensaje_whatsapp(telefono_cliente, texto_limpio, link_boton=link_extraido)
        
    except Exception as e:
        print(f"Error con Gemini: {e}")
        enviar_mensaje_whatsapp(telefono_cliente, f"🤖 ERROR TÉCNICO: {e}")

# ==========================================
# RUTAS DEL WEBHOOK Y NUEVOS ENDPOINTS
# ==========================================
@app.route('/', methods=['GET', 'POST'])
def inicio():
    return "🚀 Webhook WoodTools + IA Gemini (Experto Técnico y Orgánico) 🚀", 200

@app.route('/wa/<tanda_id>/<telefono_cliente>/<vendedor>', methods=['GET'])
def redirect_whatsapp(tanda_id, telefono_cliente, vendedor):
    texto = request.args.get('text', '')
    try:
        if tanda_id != "ORGANICO":
            res = execute_db_query("""
                INSERT INTO tracking_metricas (tanda_id, telefono, evento) 
                VALUES (%s, %s, %s) 
                ON CONFLICT (tanda_id, telefono, evento) DO NOTHING
            """, (tanda_id, telefono_cliente, 'clicked_link'), commit=True)
            
            if res and res > 0:
                execute_db_query("UPDATE metricas_campanas SET derivados = derivados + 1 WHERE tanda_id = %s", (tanda_id,), commit=True)
        else:
            execute_db_query("UPDATE metricas_campanas SET derivados = derivados + 1 WHERE tanda_id = 'ORGANICO'", commit=True)
            
    except Exception as e:
        print(f"Error tracking click: {e}")
        
    texto_codificado = urllib.parse.quote(texto)
    vendedor_link = vendedor
    if vendedor_link.startswith("549") and len(vendedor_link) == 13:
        vendedor_link = "54" + vendedor_link[3:]
    
    html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Conectando...</title>
        <style>
            body {{ font-family: Arial, sans-serif; text-align: center; margin-top: 50px; color: #333; background-color: #f8f8f8; }}
            .loader {{ border: 4px solid #e0e0e0; border-top: 4px solid #25D366; border-radius: 50%; width: 40px; height: 40px; animation: spin 1s linear infinite; margin: 20px auto; }}
            @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
            a {{ color: #25D366; text-decoration: none; font-weight: bold; font-size: 1.1rem; }}
        </style>
        <script>
            window.onload = function() {{
                window.location.replace("whatsapp://send?phone={vendedor_link}&text={texto_codificado}");
                setTimeout(function() {{
                    window.location.replace("https://wa.me/{vendedor_link}?text={texto_codificado}");
                }}, 2000);
            }};
        </script>
    </head>
    <body>
        <h2>Conectando con tu asesor...</h2>
        <div class="loader"></div>
        <p>Si la aplicación no se abre automáticamente,<br><br><a href="https://wa.me/{vendedor_link}?text={texto_codificado}">Haz clic aquí para chatear</a>.</p>
    </body>
    </html>
    """
    return html

@app.route('/asignar_vendedor', methods=['POST'])
def asignar_vendedor():
    data = request.json
    telefono_cliente_10 = extraer_10_digitos(data.get('cliente', ''))
    numero_vendedor = limpiar_numero(data.get('vendedor_tel', ''))
    tipo_campana = data.get('tipo_campana', 'Promociones')
    subtipo = data.get('subtipo', '')
    tanda_id = data.get('tanda_id', '')
    
    if telefono_cliente_10 and numero_vendedor:
        execute_db_query("""
            INSERT INTO asignaciones_v2 (telefono_cliente, numero_vendedor, tipo_campana, subtipo, tanda_id, fecha_asignacion) 
            VALUES (%s, %s, %s, %s, %s, %s) 
            ON CONFLICT (telefono_cliente) 
            DO UPDATE SET numero_vendedor=EXCLUDED.numero_vendedor, tipo_campana=EXCLUDED.tipo_campana, subtipo=EXCLUDED.subtipo, tanda_id=EXCLUDED.tanda_id, fecha_asignacion=EXCLUDED.fecha_asignacion
        """, (telefono_cliente_10, numero_vendedor, tipo_campana, subtipo, tanda_id, datetime.now()), commit=True)
        
        if tanda_id:
            execute_db_query("""
                INSERT INTO metricas_campanas (tanda_id, entregados, leidos, respondidos) 
                VALUES (%s, 0, 0, 0) 
                ON CONFLICT (tanda_id) DO NOTHING
            """, (tanda_id,), commit=True)
            
        return jsonify({"status": "asignado"}), 200
    return jsonify({"error": "faltan datos"}), 400

@app.route('/webhook', methods=['GET'])
def verificar_webhook():
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    if mode == 'subscribe' and token == TOKEN_DE_VERIFICACION: 
        return request.args.get('hub.challenge'), 200
    return 'Faltan parámetros', 400

@app.route('/webhook', methods=['POST'])
def recibir_notificaciones():
    cuerpo = request.get_json()
    if cuerpo:
        try:
            cambios = cuerpo['entry'][0]['changes'][0]['value']
            
            if 'messages' in cambios:
                mensaje = cambios['messages'][0]
                if mensaje['type'] == 'text': 
                    telefono_cliente = limpiar_numero(mensaje['from'])
                    texto_cliente = mensaje['text']['body']
                    
                    print(f"📩 MENSAJE de {telefono_cliente}: {texto_cliente}", flush=True)
                    registrar_metrica('responded', telefono_cliente) 
                    procesar_mensaje_con_gemini(telefono_cliente, texto_cliente)
                
            elif 'statuses' in cambios:
                estado = cambios['statuses'][0]['status'] 
                telefono = limpiar_numero(cambios['statuses'][0]['recipient_id'])
                msg_id = cambios['statuses'][0]['id']
                
                registrar_metrica(estado, telefono) 
                
                if estado == 'sent':
                    execute_db_query("""
                        INSERT INTO mensajes (id, telefono, estado, fecha) 
                        VALUES (%s, %s, %s, %s) 
                        ON CONFLICT (id) DO UPDATE SET estado=EXCLUDED.estado, fecha=EXCLUDED.fecha
                    """, (msg_id, telefono, estado, datetime.now()), commit=True)
                elif estado in ['delivered', 'read']:
                    execute_db_query("DELETE FROM mensajes WHERE id=%s", (msg_id,), commit=True)
                elif estado == 'failed':
                    bloquear_numero_en_sheets(telefono)
                    execute_db_query("DELETE FROM mensajes WHERE id=%s", (msg_id,), commit=True)
                
        except Exception as e: pass
    return jsonify({"status": "ok"}), 200

@app.route('/metricas', methods=['GET'])
def obtener_metricas():
    try:
        filas = execute_db_query("SELECT tanda_id, entregados, leidos, respondidos, derivados FROM metricas_campanas", fetchall=True)
        if filas is None: return jsonify({}), 200

        datos_nube = {}
        for fila in filas:
            datos_nube[fila[0]] = {
                "entregados": fila[1], 
                "leidos": fila[2], 
                "respondidos": fila[3],
                "derivados": fila[4] if len(fila) > 4 and fila[4] is not None else 0
            }
        return jsonify(datos_nube), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/tracking_general', methods=['GET'])
def obtener_tracking_general():
    try:
        filas = execute_db_query("SELECT tanda_id, telefono, evento FROM tracking_metricas", fetchall=True)
        if filas is None: return jsonify({}), 200

        datos_tracking = {}
        for tanda_id, telefono, evento in filas:
            if tanda_id not in datos_tracking:
                datos_tracking[tanda_id] = {}
            jerarquia = {'sent': 1, 'delivered': 2, 'read': 3, 'responded': 4, 'clicked_link': 5}
            evento_actual = datos_tracking[tanda_id].get(telefono, 'sent')
            if jerarquia.get(evento, 0) > jerarquia.get(evento_actual, 0):
                datos_tracking[tanda_id][telefono] = evento

        return jsonify(datos_tracking), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/derivados', methods=['GET'])
def obtener_derivados():
    try:
        filas = execute_db_query("SELECT telefono, vendedor, historial, fecha FROM chats_derivados ORDER BY fecha DESC", fetchall=True)
        if filas is None: return jsonify([]), 200
        
        datos = [{"telefono": f[0], "vendedor": f[1], "historial": json.loads(f[2]), "fecha": f[3].strftime("%Y-%m-%d %H:%M:%S")} for f in filas]
        return jsonify(datos), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/derivados/<telefono>', methods=['DELETE'])
def borrar_derivado(telefono):
    try:
        execute_db_query("DELETE FROM chats_derivados WHERE telefono = %s", (telefono,), commit=True)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    puerto = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=puerto)