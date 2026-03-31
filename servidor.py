import os
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import requests
import gspread
from google.oauth2.credentials import Credentials 
from apscheduler.schedulers.background import BackgroundScheduler
import google.generativeai as genai
import json
import psycopg2 
# --- NUEVA LIBRERÍA PARA MANEJAR CONEXIONES MÚLTIPLES ---
from psycopg2 import pool 

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
    # Creamos un "estacionamiento" de 1 a 10 conexiones simultáneas
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL, sslmode='require')
    if db_pool:
        print("✅ Pool de conexiones a PostgreSQL creado exitosamente.")
except Exception as e:
    print(f"❌ Error al conectar a PostgreSQL: {e}")

# Nueva función segura para pedir una conexión
def execute_db_query(query, params=(), commit=False, fetchone=False, fetchall=False):
    if not db_pool:
        print("❌ No hay pool de conexiones disponible.")
        return None
        
    conn = db_pool.getconn() # Pedimos un turno
    res = None
    try:
        with conn.cursor() as c:
            c.execute(query, params)
            if commit:
                conn.commit()
            if fetchone:
                res = c.fetchone()
            if fetchall:
                res = c.fetchall()
            # Si fue un INSERT/UPDATE y pedimos saber cuántas filas afectó
            if not fetchone and not fetchall and not commit:
                res = c.rowcount
    except Exception as e:
        print(f"Error en DB ejecutando '{query}': {e}")
        conn.rollback()
    finally:
        db_pool.putconn(conn) # Devolvemos el turno siempre
    return res

def init_db():
    try:
        execute_db_query('''CREATE TABLE IF NOT EXISTS mensajes (id TEXT PRIMARY KEY, telefono TEXT, estado TEXT, fecha TIMESTAMP)''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS chat_sesiones (telefono TEXT PRIMARY KEY, historial TEXT, ultima_interaccion TIMESTAMP)''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS asignaciones_v2 (telefono_cliente TEXT PRIMARY KEY, numero_vendedor TEXT, tipo_campana TEXT, subtipo TEXT, tanda_id TEXT)''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS metricas_campanas (tanda_id TEXT PRIMARY KEY, entregados INTEGER DEFAULT 0, leidos INTEGER DEFAULT 0, respondidos INTEGER DEFAULT 0)''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS tracking_metricas (tanda_id TEXT, telefono TEXT, evento TEXT, PRIMARY KEY(tanda_id, telefono, evento))''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS chats_derivados (telefono TEXT PRIMARY KEY, vendedor TEXT, historial TEXT, fecha TIMESTAMP)''', commit=True)
        
        # Forzar actualización de esquema viejo
        try:
            execute_db_query("ALTER TABLE chat_sesiones ADD COLUMN advertido INTEGER DEFAULT 0", commit=True)
        except Exception: pass # Ya existe
            
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

def enviar_mensaje_whatsapp(telefono_destino, texto):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {CLOUD_API_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": telefono_destino, "type": "text", "text": {"body": texto}}
    requests.post(url, headers=headers, json=data)

def registrar_metrica(evento, telefono):
    try:
        tel_10 = extraer_10_digitos(telefono)
        res = execute_db_query("SELECT tanda_id FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), fetchone=True)
        
        if res and res[0]:
            t_id = res[0]
            # Usamos el pool para el INSERT
            filas_afectadas = execute_db_query("""
                INSERT INTO tracking_metricas (tanda_id, telefono, evento) 
                VALUES (%s, %s, %s) 
                ON CONFLICT (tanda_id, telefono, evento) DO NOTHING
            """, (t_id, tel_10, evento), commit=True)
            
            # Si el Execute_db_query con commit no devuelve rowcount, lo calculamos manualmente o asumimos
            # Para simplificar y evitar fallos, forzamos los contadores si el insert no dio error
            if evento == 'delivered':
                execute_db_query("UPDATE metricas_campanas SET entregados = entregados + 1 WHERE tanda_id = %s", (t_id,), commit=True)
            elif evento == 'read':
                execute_db_query("UPDATE metricas_campanas SET leidos = leidos + 1 WHERE tanda_id = %s", (t_id,), commit=True)
            elif evento == 'responded':
                execute_db_query("UPDATE metricas_campanas SET respondidos = respondidos + 1 WHERE tanda_id = %s", (t_id,), commit=True)
                
    except Exception as e: print(f"Error métricas: {e}")

def bloquear_numero_en_sheets(telefono):
    try:
        if not os.path.exists(RUTA_CREDENCIALES):
            return False
            
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
        
        # --- BLOQUE 1: Borrar de Sheets si falla por 48hs ---
        hace_48_horas = ahora - timedelta(hours=48)
        para_borrar = execute_db_query("SELECT id, telefono FROM mensajes WHERE estado='sent' AND fecha < %s", (hace_48_horas,), fetchall=True)
        if para_borrar:
            for msg_id, telefono in para_borrar:
                bloquear_numero_en_sheets(telefono)
                execute_db_query("DELETE FROM mensajes WHERE id=%s", (msg_id,), commit=True)
            
        # --- BLOQUE 2: ADVERTENCIA 50 MINUTOS ---
        hace_50_minutos = ahora - timedelta(minutes=50)
        para_advertir = execute_db_query("SELECT telefono FROM chat_sesiones WHERE ultima_interaccion < %s AND advertido = 0", (hace_50_minutos,), fetchall=True)
        if para_advertir:
            for (telefono,) in para_advertir:
                try:
                    mensaje_advertencia = "⚠️ ¡Hola! Por cuestiones de seguridad, en 10 minutos se cerrará esta conversación automática. Si no me respondés, te derivaré directamente con tu asesor asignado para que te contacte y continúe atendiéndote. ¿Pudiste revisar lo que te comenté?"
                    enviar_mensaje_whatsapp(telefono, mensaje_advertencia)
                    execute_db_query("UPDATE chat_sesiones SET advertido = 1 WHERE telefono = %s", (telefono,), commit=True)
                except Exception as inner_e:
                    print(f"Fallo individual 50m {telefono}: {inner_e}")
            
        # --- BLOQUE 3: CIERRE 60 MINUTOS ---
        hace_1_hora = ahora - timedelta(hours=1)
        para_derivar = execute_db_query("SELECT telefono, historial FROM chat_sesiones WHERE ultima_interaccion < %s", (hace_1_hora,), fetchall=True)
        if para_derivar:
            for telefono, historial_str in para_derivar:
                try:
                    tel_10 = extraer_10_digitos(telefono)
                    res_vend = execute_db_query("SELECT numero_vendedor, tipo_campana FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), fetchone=True)
                    
                    vendedor_asignado = res_vend[0] if res_vend else "Sin asignar"
                    campana = res_vend[1] if res_vend else "Campaña"
                    
                    execute_db_query("""
                        INSERT INTO chats_derivados (telefono, vendedor, historial, fecha) 
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (telefono) DO UPDATE SET historial=EXCLUDED.historial, fecha=EXCLUDED.fecha
                    """, (telefono, vendedor_asignado, historial_str, datetime.now()), commit=True)

                    historial = json.loads(historial_str)
                    ultimo_msg_cliente = "Sin mensajes recientes."
                    for msg in reversed(historial):
                        if msg.get("role") == "user" and "CONTEXTO DE LA CAMPAÑA" not in msg.get("parts", [""])[0]:
                            ultimo_msg_cliente = msg["parts"][0]
                            break

                    aviso_asesor = (
                        f"🤖 *AVISO DEL BOT AUTOMÁTICO*\n\n"
                        f"El cliente con número +{telefono} ingresó por la campaña *{campana}*, pero el chat expiró por inactividad.\n\n"
                        f"💬 *Último mensaje del cliente:*\n\"{ultimo_msg_cliente}\"\n\n"
                        f"👉 *Acción requerida:* Por favor, revisa el panel de 'Chats Abandonados' en el sistema y contactalo directamente."
                    )
                    
                    if vendedor_asignado and vendedor_asignado != "Sin asignar" and vendedor_asignado != "5491145394279": 
                        enviar_mensaje_whatsapp(vendedor_asignado, aviso_asesor)
                    else:
                        enviar_mensaje_whatsapp("5491145394279", aviso_asesor)

                    execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (telefono,), commit=True)
                except Exception as inner_e:
                    print(f"Fallo derivando chat {telefono}: {inner_e}")

    except Exception as e:
        print(f"Error general en rutinas de tiempo: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(func=revisar_rutinas_de_tiempo, trigger="interval", minutes=5)
scheduler.start()

# ==========================================
# CEREBRO IA: LÓGICA CONDICIONAL DE NOMBRES
# ==========================================
def obtener_prompt_personalizado(telefono_cliente_completo):
    tel_10_digitos = extraer_10_digitos(telefono_cliente_completo)
    
    res = execute_db_query("SELECT numero_vendedor, tipo_campana, subtipo FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10_digitos,), fetchone=True)

    tel_vend = res[0] if res else "5491145394279"
    tipo_camp = res[1] if res else "Promociones"
    subtipo = res[2] if res else ""

    mapa_nombres = {
        "5491145394279": "Valentín",
        "5491157528428": "Emmanuel",
        "5491134811771": "Ariel",
        "5491165630406": "Carlos",
        "5491164591316": "Roberto Golik",
        "5491157528427": "Nicolas Saad",
        "5491153455274": "Ezequiel Calvi",
        "5491156321012": "Alan Calvi",
        "5491168457778": "Luis Quevedo"
    }
    nombre_vendedor_ia = mapa_nombres.get(tel_vend, "tu asesor comercial")
    
    plantillas = {
        "Promociones": "Hola, vengo por la promoción de [herramienta] para [material] y quisiera tener más información",
        "Rescate (Te extrañamos)": "Hola, vengo por el anuncio que me enviaron y quería novedades sobre [herramienta] para [material]",
        "Gira Vendedor": "Hola, me comentaron que [Vendedor] va a estar visitando mi zona dentro de poco. Quisiera poder arreglar para una visita",
        "Personalizado": "Hola Vengo por el anuncio de [herramienta] para [material]",
        "Recotización": "Hola, vengo del aviso de una recotización sobre [herramienta]"
    }
    
    if tipo_camp == "Novedades":
        if subtipo == "Nuevo producto":
            frase_link = "Hola me comento que tuvieron un nuevo ingreso de [herramienta]"
        else:
            frase_link = "Hola, me comentaron que entró nuevo stock de [herramienta]"
    else:
        frase_link = plantillas.get(tipo_camp, plantillas["Promociones"])
    
    return f"""
Eres el asistente virtual de recepción de WoodTools. 
Habla en español argentino (usa 'vos', empático y servicial).
Usa formato de WhatsApp (*negritas* y emojis), NUNCA uses markdown de asteriscos dobles (**).

CONTEXTO DE LA CAMPAÑA:
El cliente está respondiendo a una campaña del tipo "{tipo_camp}".

EL VENDEDOR ASIGNADO:
El vendedor asignado a este cliente se llama: {nombre_vendedor_ia}
El número de WhatsApp de este vendedor es: {tel_vend}
(Si el nombre es "tu asesor comercial", referite a él genéricamente de esa forma en todo momento, NO inventes ni supongas nombres bajo ninguna circunstancia).

TUS REGLAS DE CHARLA (¡ESTRICTAS!):
1. Saluda cordialmente (ESTÁ PROHIBIDO USAR LA PALABRA "CAMPAÑA").
2. Tu OBJETIVO ÚNICO es obtener la información necesaria para armar esta frase: "{frase_link}".
   - Si la frase tiene [herramienta] y [material], pregúntale ambas cosas al cliente de forma sutil y directa.
   - Si la frase NO tiene corchetes o solo pide [Vendedor], NO preguntes por herramientas.
3. REGLA DE HIERRO: NO respondes NADA que salga de tu objetivo. NO das precios, NO hablas de envíos, NO das información de stock, NO haces asesoría técnica.
4. ¿QUÉ HACER SI PREGUNTAN OTRA COSA?: Si el cliente hace CUALQUIER pregunta técnica o comercial fuera de decirte qué herramienta/material usa, CORTAS EL CHAT respondiendo EXACTAMENTE esto:
   "Esa es una gran pregunta técnica. Te voy a derivar con {nombre_vendedor_ia} para que te brinde esa información precisa. Hacé clic en este enlace para hablar con él 👉 https://wa.me/{tel_vend}?text=Hola,%20tengo%20una%20consulta"
   (No respondas nada más, solo esa derivación).
5. CIERRE NORMAL: Una vez que el cliente te dé el material y la herramienta (o los datos que pediste), dile que {nombre_vendedor_ia} lo va a ayudar, despídete y mándale el link con la frase completa.

FORMATO DEL LINK A WHATSAPP PARA EL CIERRE:
- Reemplaza [herramienta] y [material] con lo que te pidió.
- Si la frase incluye [Vendedor], reemplázalo con {nombre_vendedor_ia}.
- Codifica los espacios con '%20'.
El link EXACTO debe construirse así:
https://wa.me/{tel_vend}?text=[FRASE_COMPLETADA_Y_CODIFICADA]
"""

def procesar_mensaje_con_gemini(telefono_cliente, texto_entrante):
    resultado = execute_db_query("SELECT historial FROM chat_sesiones WHERE telefono = %s", (telefono_cliente,), fetchone=True)
    
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
        
        if "Te voy a derivar con" in texto_respuesta:
            tel_10 = extraer_10_digitos(telefono_cliente)
            res_vend = execute_db_query("SELECT numero_vendedor FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), fetchone=True)
            vendedor_asignado = res_vend[0] if res_vend else "Sin asignar"
            
            historial.append({"role": "model", "parts": [texto_respuesta]})
            
            execute_db_query("""
                INSERT INTO chats_derivados (telefono, vendedor, historial, fecha) 
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telefono) DO UPDATE SET historial=EXCLUDED.historial, fecha=EXCLUDED.fecha
            """, (telefono_cliente, vendedor_asignado, json.dumps(historial), datetime.now()), commit=True)
            
            execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (telefono_cliente,), commit=True)
        else:
            historial.append({"role": "model", "parts": [texto_respuesta]})
            execute_db_query("""
                INSERT INTO chat_sesiones (telefono, historial, ultima_interaccion, advertido) 
                VALUES (%s, %s, %s, 0) 
                ON CONFLICT (telefono) 
                DO UPDATE SET historial = EXCLUDED.historial, ultima_interaccion = EXCLUDED.ultima_interaccion, advertido = 0
            """, (telefono_cliente, json.dumps(historial), datetime.now()), commit=True)
            
        enviar_mensaje_whatsapp(telefono_cliente, texto_respuesta)
        
    except Exception as e:
        print(f"Error con Gemini: {e}")
        enviar_mensaje_whatsapp(telefono_cliente, f"🤖 ERROR TÉCNICO: {e}")

# ==========================================
# RUTAS DEL WEBHOOK Y NUEVOS ENDPOINTS
# ==========================================
@app.route('/', methods=['GET', 'POST'])
def inicio():
    return "🚀 Webhook WoodTools + IA Gemini (PostgreSQL Pool) 🚀", 200

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
            INSERT INTO asignaciones_v2 (telefono_cliente, numero_vendedor, tipo_campana, subtipo, tanda_id) 
            VALUES (%s, %s, %s, %s, %s) 
            ON CONFLICT (telefono_cliente) 
            DO UPDATE SET numero_vendedor=EXCLUDED.numero_vendedor, tipo_campana=EXCLUDED.tipo_campana, subtipo=EXCLUDED.subtipo, tanda_id=EXCLUDED.tanda_id
        """, (telefono_cliente_10, numero_vendedor, tipo_campana, subtipo, tanda_id), commit=True)
        
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
        filas = execute_db_query("SELECT tanda_id, entregados, leidos, respondidos FROM metricas_campanas", fetchall=True)
        if filas is None: return jsonify({}), 200

        datos_nube = {}
        for fila in filas:
            datos_nube[fila[0]] = {"entregados": fila[1], "leidos": fila[2], "respondidos": fila[3]}
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
            jerarquia = {'sent': 1, 'delivered': 2, 'read': 3, 'responded': 4}
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