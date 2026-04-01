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
            print(f"⚠️ Conexión caída detectada (Intento {attempt + 1}). Reconectando... Detalle: {e}")
            if conn:
                db_pool.putconn(conn, close=True)
            if attempt == retries:
                print("❌ Falló tras reintentar conectarse.")
                return None
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
            filas_afectadas = execute_db_query("""
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
            
        # --- BLOQUE 2: CIERRE EXACTO A LOS 60 MINUTOS ---
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
    
    res = execute_db_query("SELECT numero_vendedor, tipo_campana, subtipo, tanda_id FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10_digitos,), fetchone=True)

    numero_db = res[0] if res else None
    if numero_db in ["0", "", "Sin asignar", None]:
        numero_db = None

    tipo_camp = res[1] if res else "Promociones"
    subtipo = res[2] if res else ""
    tanda_id = res[3] if res and len(res) > 3 else "TANDA_DESCONOCIDA"

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
    
    if numero_db and numero_db in mapa_nombres:
        nombre_vendedor_ia = mapa_nombres[numero_db]
        tel_vend = numero_db
    else:
        nombre_vendedor_ia = "tu asesor"
        tel_vend = "5491145394279" 
    
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
(MUY IMPORTANTE: Si el nombre es "tu asesor", referite a él genéricamente de esa forma en todo momento. NUNCA inventes ni supongas nombres propios. Por ejemplo, debes decir "Así le avisamos a tu asesor" y JAMÁS "Así le avisamos a Valentín").

TUS REGLAS DE CHARLA (¡ESTRICTAS!):
1. Saluda cordialmente (ESTÁ PROHIBIDO USAR LA PALABRA "CAMPAÑA").
2. Tu OBJETIVO ÚNICO es obtener la información necesaria para armar esta frase: "{frase_link}".
   - Si la frase tiene [herramienta] y [material], pregúntale ambas cosas al cliente de forma sutil y directa.
   - Si la frase NO tiene corchetes o solo pide [Vendedor], NO preguntes por herramientas.
3. REGLA DE HIERRO: NO respondes NADA que salga de tu objetivo. NO das precios, NO hablas de envíos, NO das información de stock, NO haces asesoría técnica.
4. ¿QUÉ HACER SI PREGUNTAN OTRA COSA?: Si el cliente hace CUALQUIER pregunta técnica o comercial fuera de decirte qué herramienta/material usa, CORTAS EL CHAT respondiendo EXACTAMENTE esto:
   "Esa es una gran pregunta técnica. Te voy a derivar con {nombre_vendedor_ia} para que te brinde esa información precisa. Hacé clic en este enlace para hablar con él 👉 https://woodtools-webhook.onrender.com/go/{tanda_id}/{tel_10_digitos}/{tel_vend}?text=Hola,%20tengo%20una%20consulta"
   (No respondas nada más, solo esa derivación).
5. CIERRE NORMAL: Una vez que el cliente te dé el material y la herramienta (o los datos que pediste), dile que {nombre_vendedor_ia} lo va a ayudar, despídete y mándale el link con la frase completa.

FORMATO DEL LINK A WHATSAPP PARA EL CIERRE:
- Reemplaza [herramienta] y [material] con lo que te pidió.
- Si la frase incluye [Vendedor], reemplázalo con {nombre_vendedor_ia}.
- Codifica los espacios con '%20'.
El link EXACTO debe construirse así (respeta la estructura de la URL):
https://woodtools-webhook.onrender.com/go/{tanda_id}/{tel_10_digitos}/{tel_vend}?text=[FRASE_COMPLETADA_Y_CODIFICADA]
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
        
        # SI LA IA DERIVA AL CLIENTE (PASA CUALQUIER LINK), CERRAMOS LA SESIÓN
        if "Te voy a derivar con" in texto_respuesta or "/go/" in texto_respuesta or "wa.me" in texto_respuesta:
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

# --- ESTA ES LA RUTA QUE RASTREA EL CLIC Y ABRE LA APP DIRECTO ---
@app.route('/go/<tanda_id>/<telefono_cliente>/<vendedor>', methods=['GET'])
def redirect_whatsapp(tanda_id, telefono_cliente, vendedor):
    texto = request.args.get('text', '')
    try:
        res = execute_db_query("""
            INSERT INTO tracking_metricas (tanda_id, telefono, evento) 
            VALUES (%s, %s, %s) 
            ON CONFLICT (tanda_id, telefono, evento) DO NOTHING
        """, (tanda_id, telefono_cliente, 'clicked_link'), commit=True)
        
        if res and res > 0:
            execute_db_query("UPDATE metricas_campanas SET derivados = derivados + 1 WHERE tanda_id = %s", (tanda_id,), commit=True)
    except Exception as e:
        print(f"Error tracking click: {e}")
        
    texto_codificado = urllib.parse.quote(texto)
    
    # Redirección inteligente: Intenta abrir la app de WhatsApp directo, si falla usa Web
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
                // 1. Intenta abrir la app directamente usando el Deep Link
                window.location.replace("whatsapp://send?phone={vendedor}&text={texto_codificado}");
                
                // 2. Plan de respaldo: Si a los 2 segundos la app no se abrió, va a la web
                setTimeout(function() {{
                    window.location.replace("https://wa.me/{vendedor}?text={texto_codificado}");
                }}, 2000);
            }};
        </script>
    </head>
    <body>
        <h2>Conectando con tu asesor...</h2>
        <div class="loader"></div>
        <p>Si la aplicación no se abre automáticamente,<br><br><a href="https://wa.me/{vendedor}?text={texto_codificado}">Haz clic aquí para chatear</a>.</p>
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