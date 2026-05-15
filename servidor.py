import os
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect
import requests
import gspread
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request 
import google.generativeai as genai
import json
import psycopg2 
from psycopg2 import pool 
import re 
import io
from PIL import Image
import threading
from threading import Lock
from apscheduler.schedulers.background import BackgroundScheduler
import unicodedata
import time

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
# MAGIA ANTI-CHOQUES Y SISTEMA DE COLAS
# ==========================================
db_pool = None
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL, sslmode='require')
    if db_pool:
        print("✅ Pool de conexiones a PostgreSQL creado exitosamente.")
except Exception as e:
    print(f"❌ Error al conectar a PostgreSQL: {e}")

chat_locks = {}
locks_lock = Lock()
processed_msg_ids = set()

def get_chat_lock(telefono):
    with locks_lock:
        if telefono not in chat_locks:
            chat_locks[telefono] = Lock()
        return chat_locks[telefono]

def hora_arg():
    """Devuelve la hora actual en Argentina (UTC-3)"""
    return datetime.utcnow() - timedelta(hours=3)

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
        
        execute_db_query('''CREATE TABLE IF NOT EXISTS configuracion (parametro TEXT PRIMARY KEY, valor TEXT)''', commit=True)
        
        try: execute_db_query("ALTER TABLE chat_sesiones ADD COLUMN advertido INTEGER DEFAULT 0", commit=True)
        except Exception: pass 
        try: execute_db_query("ALTER TABLE metricas_campanas ADD COLUMN derivados INTEGER DEFAULT 0", commit=True)
        except Exception: pass 
        try: execute_db_query("ALTER TABLE asignaciones_v2 ADD COLUMN fecha_asignacion TIMESTAMP", commit=True)
        except Exception: pass 
        try: execute_db_query("INSERT INTO metricas_campanas (tanda_id, entregados, leidos, respondidos, derivados) VALUES ('ORGANICO', 0, 0, 0, 0) ON CONFLICT (tanda_id) DO NOTHING", commit=True)
        except Exception: pass
        try: execute_db_query("INSERT INTO configuracion (parametro, valor) VALUES ('modo_bot', 'AUTO') ON CONFLICT (parametro) DO NOTHING", commit=True)
        except Exception: pass
            
    except Exception as e:
        print(f"Error crítico iniciando tablas: {e}")

init_db()

# ==========================================
# CEREBRO: DETERMINAR MODO DE BOT
# ==========================================
def determinar_modo_bot():
    res = execute_db_query("SELECT valor FROM configuracion WHERE parametro = 'modo_bot'", fetchone=True)
    conf = res[0] if res else 'AUTO'
    
    if conf == 'ON': return "INTELIGENTE"
    elif conf == 'OFF': return "BASICO"
        
    ahora = hora_arg()
    if ahora.weekday() <= 4 and 8 <= ahora.hour < 17:
        return "BASICO"
    return "INTELIGENTE"

# ==========================================
# ENDPOINTS Y RUTINAS (Omitidos por brevedad, dejados intactos)
# ==========================================
@app.route('/estado_bot', methods=['GET'])
def obtener_estado_bot():
    res = execute_db_query("SELECT valor FROM configuracion WHERE parametro = 'modo_bot'", fetchone=True)
    conf = res[0] if res else 'AUTO'
    modo_actual = determinar_modo_bot()
    return jsonify({"configuracion": conf, "modo_actual": modo_actual}), 200

@app.route('/estado_bot', methods=['POST'])
def configurar_estado_bot():
    data = request.json
    nuevo_estado = data.get('configuracion', 'AUTO')
    if nuevo_estado in ['AUTO', 'ON', 'OFF']:
        execute_db_query("UPDATE configuracion SET valor = %s WHERE parametro = 'modo_bot'", (nuevo_estado,), commit=True)
        return jsonify({"status": "ok", "configuracion": nuevo_estado}), 200
    return jsonify({"error": "Estado inválido. Debe ser AUTO, ON, u OFF."}), 400

def limpiar_numero(num): return ''.join(filter(str.isdigit, str(num)))
def extraer_10_digitos(num): return limpiar_numero(num)[-10:] if len(limpiar_numero(num)) >= 10 else limpiar_numero(num)

def enviar_mensaje_whatsapp(telefono_destino, texto, link_boton=None):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {CLOUD_API_TOKEN}", "Content-Type": "application/json"}
    if link_boton:
        data = {"messaging_product": "whatsapp", "to": telefono_destino, "type": "interactive", "interactive": {"type": "cta_url", "body": { "text": texto }, "action": {"name": "cta_url", "parameters": {"display_text": "Hablar con asesor", "url": link_boton}}}}
    else:
        data = {"messaging_product": "whatsapp", "to": telefono_destino, "type": "text", "text": {"body": texto}}
    res = requests.post(url, headers=headers, json=data)
    if res.status_code >= 400 and link_boton:
        texto_fallback = f"{texto}\n\n👉 {link_boton}"
        data_fallback = {"messaging_product": "whatsapp", "to": telefono_destino, "type": "text", "text": {"body": texto_fallback}}
        requests.post(url, headers=headers, json=data_fallback)

def descargar_imagen_whatsapp(media_id):
    try:
        url_meta = f"https://graph.facebook.com/v18.0/{media_id}"
        headers = {"Authorization": f"Bearer {CLOUD_API_TOKEN}"}
        res_info = requests.get(url_meta, headers=headers)
        if res_info.status_code == 200:
            media_url = res_info.json().get('url')
            if media_url:
                res_img = requests.get(media_url, headers=headers)
                if res_img.status_code == 200:
                    return Image.open(io.BytesIO(res_img.content))
        return None
    except Exception as e: return None

def registrar_metrica(evento, telefono):
    try:
        tel_10 = extraer_10_digitos(telefono)
        res = execute_db_query("SELECT tanda_id FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), fetchone=True)
        if res and res[0]:
            t_id = res[0]
            execute_db_query("INSERT INTO tracking_metricas (tanda_id, telefono, evento) VALUES (%s, %s, %s) ON CONFLICT (tanda_id, telefono, evento) DO NOTHING", (t_id, tel_10, evento), commit=True)
            if evento == 'delivered': execute_db_query("UPDATE metricas_campanas SET entregados = entregados + 1 WHERE tanda_id = %s", (t_id,), commit=True)
            elif evento == 'read': execute_db_query("UPDATE metricas_campanas SET leidos = leidos + 1 WHERE tanda_id = %s", (t_id,), commit=True)
            elif evento == 'responded': execute_db_query("UPDATE metricas_campanas SET respondidos = respondidos + 1 WHERE tanda_id = %s", (t_id,), commit=True)
    except Exception as e: pass

def bloquear_numero_en_sheets(telefono, max_retries=3):
    try:
        if not os.path.exists(RUTA_CREDENCIALES): return False
        gc = gspread.service_account(filename=RUTA_CREDENCIALES)
        for intento in range(max_retries):
            try:
                sh = gc.open(NOMBRE_HOJA)
                for ws in sh.worksheets():
                    celda = ws.find(telefono)
                    if celda:
                        ws.update_cell(celda.row, celda.col, f"0000{telefono}")
                        return True
                return False
            except gspread.exceptions.APIError as e:
                if e.response.status_code == 429: time.sleep(2 ** intento)
                else: raise e
    except Exception as e: pass

def revisar_rutinas_de_tiempo():
    try:
        ahora = hora_arg()
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
                    historial = json.loads(historial_str)
                    historial_limpio = historial[2:] if len(historial) >= 2 else historial
                    execute_db_query("INSERT INTO chats_derivados (telefono, vendedor, historial, fecha) VALUES (%s, %s, %s, %s) ON CONFLICT (telefono) DO UPDATE SET historial=EXCLUDED.historial, fecha=EXCLUDED.fecha", (telefono, vendedor_asignado, json.dumps(historial_limpio), hora_arg()), commit=True)
                    ultimo_msg_cliente = "Sin mensajes recientes."
                    for msg in reversed(historial_limpio):
                        if msg.get("role") == "user":
                            ultimo_msg_cliente = msg["parts"][0]
                            break
                    aviso_asesor = f"🤖 *AVISO DEL BOT AUTOMÁTICO*\n\nEl cliente con número +{telefono} ingresó por la campaña *{campana}*, pero el chat expiró tras 1 hora de inactividad.\n\n💬 *Último mensaje del cliente:*\n\"{ultimo_msg_cliente}\"\n\n👉 *Acción requerida:* Por favor, revisa el panel de 'Chats Pendientes' en el sistema y contactalo directamente."
                    if vendedor_asignado and vendedor_asignado != "Sin asignar" and vendedor_asignado != "5491145394279": enviar_mensaje_whatsapp(vendedor_asignado, aviso_asesor)
                    else: enviar_mensaje_whatsapp("5491145394279", aviso_asesor)
                    aviso_cliente = "⚠️ ¡Hola! Como pasó 1 hora de inactividad sin respuesta, cerramos esta conversación automática. Tu asesor asignado te contactará a la brevedad. ¡Gracias!"
                    enviar_mensaje_whatsapp(telefono, aviso_cliente)
                    execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (telefono,), commit=True)
                    execute_db_query("DELETE FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), commit=True)
                except Exception as inner_e: pass
    except Exception as e: pass

scheduler = BackgroundScheduler()
scheduler.add_job(func=revisar_rutinas_de_tiempo, trigger="interval", minutes=5)
scheduler.start()

# ==========================================
# 1. HERRAMIENTA: BASE DE DATOS DE PRODUCTOS
# ==========================================
def consultar_catalogo(familia: str, aplicacion_o_material: str) -> str:
    """
    Busca herramientas exactas en la base de datos de stock según familia y material.
    
    Args:
        familia: Categoría principal (Sierras, Fresas, Mechas, Cuchillas).
        aplicacion_o_material: Uso o material (melamina, madera, aluminio, ranuras, bisagra).
    """
    try:
        query = '''
            SELECT marca, nombre_publico, codigo_interno, aplicacion_material, medidas_y_specs, descripcion_tecnica 
            FROM productos 
            WHERE familia ILIKE %s AND aplicacion_material ILIKE %s
        '''
        resultados = execute_db_query(query, (f"%{familia}%", f"%{aplicacion_o_material}%"), fetchall=True)
        if not resultados:
            return f"No se encontraron productos para familia '{familia}' y material '{aplicacion_o_material}'."
            
        texto_resultado = "DATOS TÉCNICOS ENCONTRADOS EN BASE DE DATOS:\n"
        for prod in resultados:
            texto_resultado += f"- Producto: {prod[1]} (Marca: {prod[0]})\n"
            texto_resultado += f"  Código interno (NO DECIR AL CLIENTE): {prod[2]}\n"
            texto_resultado += f"  Aplicación: {prod[3]}\n"
            texto_resultado += f"  Especificaciones: {prod[4]}\n\n"
        return texto_resultado
    except Exception as e:
        return "Error al conectar con la base de datos de stock."

# ==========================================
# 2. HERRAMIENTA: MANUAL DE VENTAS (REGLAS)
# ==========================================
# Este diccionario reemplaza las 150 líneas de tu prompt viejo
MANUAL_VENTAS = {
    "sierras": """REGLAS PARA VENDER SIERRAS:
- Marcas: Freud o Franzoi.
- Melamina: Si pide "con incisor", asume máquina industrial. Incisor para melamina: LI25M31FA3.
- Máquinas de banco/mano: Ofrecer siempre ÁNGULO NEGATIVO (sin incisor). Solo medidas 230, 220, 185 y 180mm.
- Escuadradoras: Preguntar siempre qué material corta.
- Franzoi: Ofrecer SOLO para abrir madera o tirantería. NUNCA escuadradoras.
- Veta: Transversal = dientes alternos negativos. Universal = corte general.
- Cinta/Sin fin: No vendemos.""",

    "fresas_y_mechas": """REGLAS PARA VENDER FRESAS Y MECHAS:
- Marcas: WoodTools, Italiana o Franzoi. ¡NUNCA digas que una fresa es Freud!
- MATERIALES: TODAS las fresas cortan MADERA por defecto. NO preguntes el material a menos que el cliente pida una "Fresa Recta de 6 dientes" (única que corta melamina además de madera) o si usa máquina CNC.
- CNC/Routers (Melamina/MDF): Ofrecer Fresa de Compresión o Mecha Integral.
- Fresa de Compresión: NO pidas medidas abiertas. Ofrecer directamente 8mm, 10mm o 12mm.
- Eje central (Fresas): Vienen de 40mm. Si la máquina tiene menos (ej 30mm), ofrecer Buje. Si tiene más (50mm), se manda a alesar a medida (No ofrecer buje).
- Prohibido preguntar: "profundidad" o "espesor de la madera" para fresas. Solo importa el Diámetro exterior y el Ancho de corte.""",

    "cuchillas": """REGLAS PARA VENDER CUCHILLAS:
- Preguntar: ¿Planas (para cepillar) o Dorso Ranurado (para moldurera)?
- "Cuchillas para moldurera" es sinónimo de Dorso Ranurado. 
- Regla del Largo: El "largo" de la cuchilla es igual al "ancho" de la madera a trabajar. Si dice madera de 4 pulgadas, el largo es 100mm.
- Seguridad: Si el cliente dice que 4mm de espesor es peligroso, dale la razón y ofrece de 8mm.
- Prohibido ofrecer fresas si el cliente pide cuchillas o cabezales.""",

    "atencion_general": """REGLAS DE ATENCIÓN, QUEJAS Y ENVÍOS:
- Envíos: CABA/GBA coordina el vendedor. Interior por Vía Cargo/Credifin.
- Afilados: Demora de 2 a 5 días.
- Precios: No tienes listas de precios. El vendedor los pasará. Dilo una sola vez.
- Códigos: PROHIBIDO decir códigos internos al cliente (ej. FRS0054, LU3F).
- Quejas: Si hay mala experiencia (mal afilado, etc.), prohibido vender. Pedir disculpas y derivar.
- Ocupado: Si no puede hablar, acordar contacto entre Lunes y Viernes de 8 a 17hs. Etiqueta: [AGENDADO: Contactar el dia X a las Y al numero Z].
- Despedidas: Si dice "gracias", "no compro", despídete cortésmente y corta el chat.
- Número equivocado: Derivar con [AGENDADO: Numero Equivocado - Nuevo numero: X]."""
}

def consultar_manual_de_ventas(tema: str) -> str:
    """
    Útil para consultar las directivas comerciales, qué marcas ofrecer, y cómo responder a preguntas de clientes.
    
    Args:
        tema: Selecciona uno de los siguientes ('sierras', 'fresas_y_mechas', 'cuchillas', 'atencion_general').
    """
    return MANUAL_VENTAS.get(tema.lower(), "Tema inválido. Usa 'sierras', 'fresas_y_mechas', 'cuchillas' o 'atencion_general'.")


# ==========================================
# CEREBRO IA: PROMPT ULTRA REDUCIDO
# ==========================================
BASE_CONOCIMIENTO = """
Eres un asesor profesional sobre carpintería para WoodTools. 
Tu labor es indagar qué herramienta necesita el cliente, armar un carrito de compras y derivarlo al vendedor humano.

REGLAS DE ORO:
1. NO inventes características. Si tienes dudas sobre cómo ofrecer un producto o qué preguntar, USA LA HERRAMIENTA `consultar_manual_de_ventas`.
2. Para saber medidas, características técnicas de productos y stock, USA LA HERRAMIENTA `consultar_catalogo`.
3. Tono: Amigable y directo.
4. PROHIBIDO entregar códigos alfanuméricos internos a los clientes.
"""

def obtener_prompt_personalizado(telefono_cliente_completo, modo_bot):
    tel_10_digitos = extraer_10_digitos(telefono_cliente_completo)
    res = execute_db_query("SELECT numero_vendedor, tipo_campana, subtipo, tanda_id FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10_digitos,), fetchone=True)

    es_organico = not res
    tanda_id = "ORGANICO" if es_organico else (res[3] if res and len(res) > 3 else "TANDA_DESCONOCIDA")
    tipo_camp = "Contacto Orgánico" if es_organico else (res[1] if res else "Promociones")
    url_base_derivacion = f"https://woodtools-webhook.onrender.com/wa/{tanda_id}/{tel_10_digitos}/"

    mapa_nombres = {
        "5491145394279": "Valentín", "5491157528428": "Emmanuel", "5491134811771": "Ariel",
        "5491165630406": "Carlos", "5491164591316": "Roberto Golik", "5491157528427": "Nicolas Saad",
        "5491153455274": "Ezequiel Calvi", "5491156321012": "Alan Calvi", "5491168457778": "Luis Quevedo"
    }

    if not es_organico:
        numero_db = res[0] if res[0] not in ["0", "", "Sin asignar", None] else None
        nombre_vendedor_ia = mapa_nombres.get(numero_db, "tu asesor")
        tel_vend = numero_db if numero_db in mapa_nombres else "5491145394279"
        texto_contexto = f"""VENDEDOR: {nombre_vendedor_ia} ({tel_vend}). NÚMERO CLIENTE: +{telefono_cliente_completo}"""
    else:
        texto_contexto = f"""VENDEDOR: Aún no elegido. NÚMERO CLIENTE: +{telefono_cliente_completo}.
Si es el PRIMER mensaje y saludan, pregunta con quién prefiere hablar (Carlos, Valentín o Emmanuel). 
Si ya preguntan algo directo o dicen "indistinto", responde directo y asigna a Valentín en silencio."""

    if modo_bot == "BASICO":
        reglas_modo = f"""
MODO BÁSICO (Recepcionista):
- Respuestas MUY cortas (1 o 2 renglones).
- Indagación rápida: ¿Qué herramienta, máquina o material?
- NUNCA repitas el saludo ni avises que lo comunicarás con el asesor en cada mensaje. Si ya está asignado, habla directo.
- No envíes enlace de inmediato, pregunta si quiere otra herramienta. Si dice no, genera enlace.
ENLACE (NO CORTAR): {url_base_derivacion}[TEL_ASESOR]?text=Hola,%20necesito%20info%20de:%0A-%20[Herramienta]%20para%20[Maquina]
"""
    else:
        reglas_modo = f"""
MODO INTELIGENTE (Asesor):
- Armarás un carrito de compras. Mantén las respuestas fluidas.
- NUNCA repitas el saludo ni avises que lo comunicarás con el asesor en cada mensaje. Si ya está asignado, habla directo.
- Si el cliente da un dato (ej. madera 4 pulgadas), conviértelo a mm y no vuelvas a preguntar.
- Muestra los Diámetros y Anchos disponibles.
- Antes de dar el enlace, pregunta si quiere algo más. Si cierra, genera enlace.
ENLACE (NO CORTAR): {url_base_derivacion}[TEL_ASESOR]?text=Hola,%20quiero%20cotizacion%20de:%0A-%20[prod1]%20[medida]%20[cant]%0A-%20[prod2]
"""

    return f"{BASE_CONOCIMIENTO}\n{texto_contexto}\n{reglas_modo}"

def procesar_mensaje_con_gemini(telefono_cliente, texto_entrante, imagen_pil=None):
    lock = get_chat_lock(telefono_cliente)
    with lock:
        if texto_entrante and "reset" in texto_entrante.strip().lower():
            tel_10 = extraer_10_digitos(telefono_cliente)
            execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (telefono_cliente,), commit=True)
            enviar_mensaje_whatsapp(telefono_cliente, "✅ *Memoria borrada.* Escribime un 'Hola' para empezar desde cero.")
            return
    
        resultado = execute_db_query("SELECT historial, ultima_interaccion FROM chat_sesiones WHERE telefono = %s", (telefono_cliente,), fetchone=True)
        tel_10 = extraer_10_digitos(telefono_cliente)
        
        if resultado and resultado[1] and hora_arg() - resultado[1] > timedelta(hours=1):
            execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (telefono_cliente,), commit=True)
            resultado = None  
                
        res_tanda = execute_db_query("SELECT tanda_id FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), fetchone=True)
        tanda_id_actual = res_tanda[0] if (res_tanda and res_tanda[0]) else "ORGANICO"
        
        modo_actual = determinar_modo_bot()
        prompt_dinamico = obtener_prompt_personalizado(telefono_cliente, modo_actual)
        
        if resultado:
            historial = json.loads(resultado[0])
            if len(historial) > 0 and historial[0]["role"] == "user":
                historial[0]["parts"] = [prompt_dinamico]
        else:
            historial = [
                {"role": "user", "parts": [prompt_dinamico]},
                {"role": "model", "parts": ["Entendido. Actuaré según mi modo respetando las herramientas."]}
            ]
            
        texto_para_historial = f"[Imagen analizada] {texto_entrante}".strip() if imagen_pil else texto_entrante
        
        try:
            # ==== AQUÍ AGREGAMOS AMBAS HERRAMIENTAS AL MODELO ====
            model = genai.GenerativeModel(
                model_name='gemini-2.5-flash',
                tools=[consultar_catalogo, consultar_manual_de_ventas]
            )
            chat = model.start_chat(
                history=historial[:-1],
                enable_automatic_function_calling=True
            )
            
            if imagen_pil:
                param_vision = """INSTRUCCIÓN VISUAL ESTRICTA:
Eres experto analizando herramientas. El cliente envió una imagen.
=== SI ES MADERA (Muestra de corte) ===
- Cortes rectos/90° o machimbre: Fresa Bisagra o Machimbre.
- Dientes en V agudos: Fresa Finger.
- Dientes planos/chatos: Fresa Ensamble Cónico.
- Curvas decorativas complejas sin canal central: Fresa Multimoldura.
=== SI ES METAL (Herramienta) ===
- Rodillo con plaquitas cuadradas: Cabezal Cepillador.
- Fresa de curvas continuas y exageradas: Fresa Multimoldura.
- Broca tornasolada/arcoíris: Fresa Compresión (Nesting).
- Mecha espiral coloreada (naranja/negro): Mecha Ciega o Pasante.

Identifica la herramienta y ofrécela con entusiasmo. Usa las herramientas de catálogo si necesitas datos. NUNCA des códigos internos."""
                respuesta = chat.send_message([param_vision, imagen_pil, texto_entrante or ""])
            else:
                respuesta = chat.send_message(texto_entrante)
            
            texto_respuesta = respuesta.text
            texto_limpio = texto_respuesta
            link_extraido = None
            
            match = re.search(r'(https://woodtools-webhook\.onrender\.com/wa/[^\s<>]+)', texto_respuesta)
            if match:
                raw_url = match.group(1).rstrip('.",\'')
                texto_limpio = re.sub(r'\[AGENDADO:\s*.*?\]', '', texto_respuesta, flags=re.IGNORECASE).strip()
                texto_limpio = texto_limpio.replace(raw_url, "").replace("👉", "").strip()
                url_limpia = ''.join((c for c in urllib.parse.unquote(raw_url) if unicodedata.category(c) != 'Mn'))
                link_extraido = urllib.parse.quote(url_limpia, safe=':/?&=%')
            else:
                texto_limpio = re.sub(r'\[AGENDADO:\s*.*?\]', '', texto_respuesta, flags=re.IGNORECASE).strip()
            
            historial.append({"role": "user", "parts": [texto_para_historial]})
            historial.append({"role": "model", "parts": [texto_respuesta]})
            
            execute_db_query("""
                INSERT INTO chat_sesiones (telefono, historial, ultima_interaccion, advertido) 
                VALUES (%s, %s, %s, 0) 
                ON CONFLICT (telefono) 
                DO UPDATE SET historial = EXCLUDED.historial, ultima_interaccion = EXCLUDED.ultima_interaccion, advertido = 0
            """, (telefono_cliente, json.dumps(historial), hora_arg()), commit=True)
            
            enviar_mensaje_whatsapp(telefono_cliente, texto_limpio, link_boton=link_extraido)
            
        except Exception as e:
            print(f"Error con Gemini: {e}")
            enviar_mensaje_whatsapp(telefono_cliente, f"🤖 Dame un momento, estoy consultando los catálogos...")

# ==========================================
# RUTAS DEL WEBHOOK Y NUEVOS ENDPOINTS
# ==========================================
@app.route('/', methods=['GET', 'POST'])
def inicio(): return "🚀 Webhook WoodTools + IA Gemini (Function Calling) 🚀", 200

@app.route('/wa/<tanda_id>/<telefono_cliente>/<vendedor>', methods=['GET'])
def redirect_whatsapp(tanda_id, telefono_cliente, vendedor):
    texto_codificado = urllib.parse.quote(request.args.get('text', ''))
    vendedor_link = "54" + vendedor[3:] if vendedor.startswith("549") and len(vendedor) == 13 else vendedor
    return f'<script>window.onload=function(){{window.location.replace("whatsapp://send?phone={vendedor_link}&text={texto_codificado}");setTimeout(function(){{window.location.replace("https://wa.me/{vendedor_link}?text={texto_codificado}");}},2000);}};</script>'

@app.route('/webhook', methods=['GET'])
def verificar_webhook():
    if request.args.get('hub.mode') == 'subscribe' and request.args.get('hub.verify_token') == TOKEN_DE_VERIFICACION: return request.args.get('hub.challenge'), 200
    return 'Faltan parámetros', 400

@app.route('/webhook', methods=['POST'])
def recibir_notificaciones():
    cuerpo = request.get_json()
    if cuerpo:
        try:
            cambios = cuerpo['entry'][0]['changes'][0]['value']
            if 'messages' in cambios:
                mensaje = cambios['messages'][0]
                msg_id = mensaje.get('id')
                if msg_id in processed_msg_ids: return jsonify({"status": "ok"}), 200
                processed_msg_ids.add(msg_id)
                if len(processed_msg_ids) > 1000: processed_msg_ids.clear()

                if mensaje['type'] == 'text': 
                    telefono_cliente = limpiar_numero(mensaje['from'])
                    threading.Thread(target=procesar_mensaje_con_gemini, args=(telefono_cliente, mensaje['text']['body'])).start()
                elif mensaje['type'] == 'image':
                    telefono_cliente = limpiar_numero(mensaje['from'])
                    media_id = mensaje['image']['id']
                    texto_cliente = mensaje['image'].get('caption', '')
                    def procesar_imagen_bg(tel, txt, m_id):
                        img_pil = descargar_imagen_whatsapp(m_id)
                        procesar_mensaje_con_gemini(tel, txt, imagen_pil=img_pil) if img_pil else procesar_mensaje_con_gemini(tel, "Error con la imagen. " + txt)
                    threading.Thread(target=procesar_imagen_bg, args=(telefono_cliente, texto_cliente, media_id)).start()
        except Exception as e: pass
    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))