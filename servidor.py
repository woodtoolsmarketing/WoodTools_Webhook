import os
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import requests
import gspread
from apscheduler.schedulers.background import BackgroundScheduler
import google.generativeai as genai
import json

app = Flask(__name__)

# ==========================================
# CONFIGURACIÓN SEGURA (Carga desde tokens.json)
# ==========================================
ruta_tokens = "/etc/secrets/tokens.json" if os.path.exists("/etc/secrets/tokens.json") else "tokens.json"

try:
    with open(ruta_tokens, 'r') as f:
        credenciales_api = json.load(f)
    TOKEN_DE_VERIFICACION = credenciales_api.get("TOKEN_DE_VERIFICACION", "")
    CLOUD_API_TOKEN = credenciales_api.get("CLOUD_API_TOKEN", "")
    PHONE_NUMBER_ID = credenciales_api.get("PHONE_NUMBER_ID", "")
    GEMINI_API_KEY = credenciales_api.get("GEMINI_API_KEY", "")
except Exception as e:
    print(f"⚠️ ATENCIÓN: No se pudo cargar tokens.json. Asegurate de haberlo subido a Secret Files en Render. Error: {e}")
    TOKEN_DE_VERIFICACION = CLOUD_API_TOKEN = PHONE_NUMBER_ID = GEMINI_API_KEY = ""

genai.configure(api_key=GEMINI_API_KEY)

RUTA_CREDENCIALES = "/etc/secrets/credenciales.json" 
NOMBRE_HOJA = "Base de datos wt"

def limpiar_numero(num):
    return ''.join(filter(str.isdigit, str(num)))

def extraer_10_digitos(num):
    solo_numeros = limpiar_numero(num)
    return solo_numeros[-10:] if len(solo_numeros) >= 10 else solo_numeros

# ==========================================
# BASE DE DATOS LOCAL
# ==========================================
def init_db():
    conn = sqlite3.connect('memoria_mensajes.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS mensajes (id TEXT PRIMARY KEY, telefono TEXT, estado TEXT, fecha TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_sesiones (telefono TEXT PRIMARY KEY, historial TEXT, ultima_interaccion TIMESTAMP, advertido INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS asignaciones_v2 (telefono_cliente TEXT PRIMARY KEY, numero_vendedor TEXT, tipo_campana TEXT, subtipo TEXT, tanda_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS metricas_campanas (tanda_id TEXT PRIMARY KEY, entregados INTEGER DEFAULT 0, leidos INTEGER DEFAULT 0, respondidos INTEGER DEFAULT 0)''')
    
    # --- NUEVA TABLA: Seguimiento estricto de eventos únicos por cliente ---
    c.execute('''CREATE TABLE IF NOT EXISTS tracking_metricas (tanda_id TEXT, telefono TEXT, evento TEXT, PRIMARY KEY(tanda_id, telefono, evento))''')
    
    conn.commit()
    conn.close()

init_db()

def registrar_metrica(evento, telefono):
    try:
        tel_10 = extraer_10_digitos(telefono)
        conn = sqlite3.connect('memoria_mensajes.db')
        c = conn.cursor()
        
        c.execute("SELECT tanda_id FROM asignaciones_v2 WHERE telefono_cliente = ?", (tel_10,))
        res = c.fetchone()
        
        if res and res[0]:
            t_id = res[0]
            # Solo permite anotar un evento de un tipo (ej. 'delivered') UNA VEZ por persona, por campaña
            c.execute("INSERT OR IGNORE INTO tracking_metricas (tanda_id, telefono, evento) VALUES (?, ?, ?)", (t_id, tel_10, evento))
            
            # Si el registro fue exitoso (es decir, es la primera vez que pasa), recién ahí suma a la estadística global
            if c.rowcount > 0:
                if evento == 'delivered':
                    c.execute("UPDATE metricas_campanas SET entregados = entregados + 1 WHERE tanda_id = ?", (t_id,))
                elif evento == 'read':
                    c.execute("UPDATE metricas_campanas SET leidos = leidos + 1 WHERE tanda_id = ?", (t_id,))
                elif evento == 'responded':
                    c.execute("UPDATE metricas_campanas SET respondidos = respondidos + 1 WHERE tanda_id = ?", (t_id,))
                
        conn.commit(); conn.close()
    except Exception as e: print(f"Error métricas: {e}")

def bloquear_numero_en_sheets(telefono):
    try:
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

def revisar_rutinas_de_tiempo():
    conn = sqlite3.connect('memoria_mensajes.db')
    c = conn.cursor()
    ahora = datetime.now()
    
    # Manejo de fallos en 48hs
    hace_48_horas = ahora - timedelta(hours=48)
    c.execute("SELECT id, telefono FROM mensajes WHERE estado='sent' AND fecha < ?", (hace_48_horas,))
    for msg_id, telefono in c.fetchall():
        bloquear_numero_en_sheets(telefono)
        c.execute("DELETE FROM mensajes WHERE id=?", (msg_id,))
        
    # --- NUEVOS TIEMPOS DE ADVERTENCIA (50 MINUTOS Y 1 HORA) ---
    hace_50_minutos = ahora - timedelta(minutes=50)
    hace_1_hora = ahora - timedelta(hours=1)
    
    # 1. ADVERTENCIA A LOS 50 MINUTOS
    c.execute("SELECT telefono FROM chat_sesiones WHERE ultima_interaccion < ? AND advertido = 0", (hace_50_minutos,))
    for (telefono,) in c.fetchall():
        mensaje_advertencia = "⚠️ ¡Hola! Por cuestiones de seguridad, en 10 minutos se cerrará esta conversación automática. Si no me respondés, te derivaré directamente con tu asesor asignado para que te contacte y continúe atendiéndote. ¿Pudiste revisar lo que te comenté?"
        enviar_mensaje_whatsapp(telefono, mensaje_advertencia)
        c.execute("UPDATE chat_sesiones SET advertido = 1 WHERE telefono = ?", (telefono,))
        
    # 2. CIERRE Y DERIVACIÓN A LA HORA (60 MINUTOS)
    c.execute("SELECT telefono, historial FROM chat_sesiones WHERE ultima_interaccion < ?", (hace_1_hora,))
    for telefono, historial_str in c.fetchall():
        # Buscamos a qué vendedor le tocaba este cliente
        tel_10 = extraer_10_digitos(telefono)
        c.execute("SELECT numero_vendedor, tipo_campana FROM asignaciones_v2 WHERE telefono_cliente = ?", (tel_10,))
        datos_asignacion = c.fetchone()
        
        if datos_asignacion:
            vendedor_tel = datos_asignacion[0]
            campana = datos_asignacion[1]
            
            # Rescatamos el último mensaje real que mandó el cliente
            historial = json.loads(historial_str)
            ultimo_msg_cliente = "Sin mensajes recientes."
            for msg in reversed(historial):
                if msg.get("role") == "user" and "CONTEXTO DE LA CAMPAÑA" not in msg.get("parts", [""])[0]:
                    ultimo_msg_cliente = msg["parts"][0]
                    break

            # Armamos el aviso para el Asesor Humano
            aviso_asesor = (
                f"🤖 *AVISO DEL BOT AUTOMÁTICO*\n\n"
                f"El cliente con número +{telefono} ingresó por la campaña *{campana}*, pero el chat expiró por inactividad.\n\n"
                f"💬 *Último mensaje del cliente:*\n\"{ultimo_msg_cliente}\"\n\n"
                f"👉 *Acción requerida:* Por favor, contactalo directamente para no perder la venta."
            )
            
            # Le mandamos el aviso al vendedor (si hay un número asignado)
            if vendedor_tel and vendedor_tel != "5491145394279": # Validación por las dudas
                enviar_mensaje_whatsapp(vendedor_tel, aviso_asesor)
            else:
                # Fallback al número de Valentín o general si no hay vendedor_tel
                enviar_mensaje_whatsapp("5491145394279", aviso_asesor)

        # Finalmente, borramos la sesión del cliente
        c.execute("DELETE FROM chat_sesiones WHERE telefono = ?", (telefono,))
        
    conn.commit(); conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(func=revisar_rutinas_de_tiempo, trigger="interval", minutes=5) # Lo bajé a 5 min para que sea más preciso al detectar la hora exacta
scheduler.start()

# ==========================================
# CEREBRO IA: LÓGICA CONDICIONAL DE NOMBRES
# ==========================================
def obtener_prompt_personalizado(telefono_cliente_completo):
    tel_10_digitos = extraer_10_digitos(telefono_cliente_completo)
    
    conn = sqlite3.connect('memoria_mensajes.db')
    c = conn.cursor()
    c.execute("SELECT numero_vendedor, tipo_campana, subtipo FROM asignaciones_v2 WHERE telefono_cliente = ?", (tel_10_digitos,))
    res = c.fetchone()
    conn.close()

    tel_vend = res[0] if res else "5491145394279"
    tipo_camp = res[1] if res else "Promociones"
    subtipo = res[2] if res else ""
    
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

REGLA DE IDENTIFICACIÓN DEL VENDEDOR (¡MUY IMPORTANTE!):
El número de WhatsApp del vendedor asignado a este cliente es exactamente: {tel_vend}
Aplica ESTRICTAMENTE la siguiente regla para saber cómo se llama y referirte a él:
- Si el número es 5491145394279 el vendedor es "Valentín"
- Si el número es 5491157528428 el vendedor es "Emmanuel"
- Si el número es 5491134811771 el vendedor es "Ariel"
- Si el número es 5491165630406 el vendedor es "Carlos"
- Si el número es 5491164591316 el vendedor es "Roberto Golik"
- Si el número es 5491157528427 el vendedor es "Nicolas Saad"
- Si el número es 5491153455274 el vendedor es "Ezequiel Calvi"
- Si el número es 5491156321012 el vendedor es "Alan Calvi"
- Si el número es 5491168457778 el vendedor es "Luis Quevedo"

TUS REGLAS DE CHARLA (¡ESTRICTAS!):
1. Saluda cordialmente (ESTÁ PROHIBIDO USAR LA PALABRA "CAMPAÑA").
2. Tu OBJETIVO ÚNICO es obtener la información necesaria para armar esta frase: "{frase_link}".
   - Si la frase tiene [herramienta] y [material], pregúntale ambas cosas al cliente de forma sutil y directa.
   - Si la frase NO tiene corchetes o solo pide [Vendedor], NO preguntes por herramientas.
3. REGLA DE HIERRO: NO respondes NADA que salga de tu objetivo. NO das precios, NO hablas de envíos, NO das información de stock, NO haces asesoría técnica.
4. ¿QUÉ HACER SI PREGUNTAN OTRA COSA?: Si el cliente hace CUALQUIER pregunta técnica o comercial fuera de decirte qué herramienta/material usa, CORTAS EL CHAT respondiendo EXACTAMENTE esto:
   "Esa es una gran pregunta técnica. Te voy a derivar con [Nombre del Vendedor], tu asesor comercial, para que te brinde esa información precisa. Hacé clic en este enlace para hablar con él 👉 https://wa.me/{tel_vend}?text=Hola,%20tengo%20una%20consulta"
   (No respondas nada más, solo esa derivación).
5. CIERRE NORMAL: Una vez que el cliente te dé el material y la herramienta (o los datos que pediste), dile que [Nombre del Vendedor] lo va a ayudar, despídete y mándale el link con la frase completa.

FORMATO DEL LINK A WHATSAPP PARA EL CIERRE:
- Reemplaza [herramienta] y [material] con lo que te pidió.
- Si la frase incluye [Vendedor], reemplázalo con el nombre que dedujiste.
- Codifica los espacios con '%20'.
El link EXACTO debe construirse así:
https://wa.me/{tel_vend}?text=[FRASE_COMPLETADA_Y_CODIFICADA]
"""

def procesar_mensaje_con_gemini(telefono_cliente, texto_entrante):
    conn = sqlite3.connect('memoria_mensajes.db')
    c = conn.cursor()
    c.execute("SELECT historial FROM chat_sesiones WHERE telefono = ?", (telefono_cliente,))
    resultado = c.fetchone()
    
    prompt_dinamico = obtener_prompt_personalizado(telefono_cliente)
    
    if resultado:
        historial = json.loads(resultado[0])
        if len(historial) > 0 and historial[0]["role"] == "user":
            historial[0]["parts"] = [prompt_dinamico]
    else:
        historial = [
            {"role": "user", "parts": [prompt_dinamico]},
            {"role": "model", "parts": ["Entendido. Aplicaré las reglas de forma hiper estricta. Si me hacen preguntas fuera de mi alcance, derivaré inmediatamente al vendedor sin responder la duda técnica."]}
        ]
        
    historial.append({"role": "user", "parts": [texto_entrante]})
    
    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
        chat = model.start_chat(history=historial[:-1])
        respuesta = chat.send_message(texto_entrante)
        texto_respuesta = respuesta.text
        
        historial.append({"role": "model", "parts": [texto_respuesta]})
        c.execute("INSERT OR REPLACE INTO chat_sesiones (telefono, historial, ultima_interaccion, advertido) VALUES (?, ?, ?, 0)", 
                  (telefono_cliente, json.dumps(historial), datetime.now()))
        conn.commit()
        
        enviar_mensaje_whatsapp(telefono_cliente, texto_respuesta)
        
    except Exception as e:
        print(f"Error con Gemini: {e}")
        enviar_mensaje_whatsapp(telefono_cliente, f"🤖 ERROR TÉCNICO: {e}")
    finally:
        conn.close()

def enviar_mensaje_whatsapp(telefono_destino, texto):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {CLOUD_API_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": telefono_destino, "type": "text", "text": {"body": texto}}
    requests.post(url, headers=headers, json=data)

# ==========================================
# RUTAS DEL WEBHOOK Y NUEVOS ENDPOINTS
# ==========================================
@app.route('/', methods=['GET', 'POST'])
def inicio():
    return "🚀 Webhook WoodTools + IA Gemini (Métricas Exactas) 🚀", 200

@app.route('/asignar_vendedor', methods=['POST'])
def asignar_vendedor():
    data = request.json
    telefono_cliente_10 = extraer_10_digitos(data.get('cliente', ''))
    numero_vendedor = limpiar_numero(data.get('vendedor_tel', ''))
    tipo_campana = data.get('tipo_campana', 'Promociones')
    subtipo = data.get('subtipo', '')
    tanda_id = data.get('tanda_id', '')
    
    if telefono_cliente_10 and numero_vendedor:
        conn = sqlite3.connect('memoria_mensajes.db')
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO asignaciones_v2 (telefono_cliente, numero_vendedor, tipo_campana, subtipo, tanda_id) VALUES (?, ?, ?, ?, ?)", 
                  (telefono_cliente_10, numero_vendedor, tipo_campana, subtipo, tanda_id))
        
        if tanda_id:
            c.execute("INSERT OR IGNORE INTO metricas_campanas (tanda_id, entregados, leidos, respondidos) VALUES (?, 0, 0, 0)", (tanda_id,))
            
        conn.commit(); conn.close()
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
                
                conn = sqlite3.connect('memoria_mensajes.db')
                c = conn.cursor()
                if estado == 'sent':
                    c.execute("INSERT OR REPLACE INTO mensajes (id, telefono, estado, fecha) VALUES (?, ?, ?, ?)", (msg_id, telefono, estado, datetime.now()))
                elif estado in ['delivered', 'read']:
                    c.execute("DELETE FROM mensajes WHERE id=?", (msg_id,))
                elif estado == 'failed':
                    bloquear_numero_en_sheets(telefono)
                    c.execute("DELETE FROM mensajes WHERE id=?", (msg_id,))
                conn.commit(); conn.close()
                
        except Exception as e: pass
    return jsonify({"status": "ok"}), 200

@app.route('/metricas', methods=['GET'])
def obtener_metricas():
    try:
        conn = sqlite3.connect('memoria_mensajes.db')
        c = conn.cursor()
        c.execute("SELECT tanda_id, entregados, leidos, respondidos FROM metricas_campanas")
        filas = c.fetchall()
        conn.close()

        datos_nube = {}
        for fila in filas:
            tanda_id, entregados, leidos, respondidos = fila
            datos_nube[tanda_id] = {
                "entregados": entregados,
                "leidos": leidos,
                "respondidos": respondidos
            }
        return jsonify(datos_nube), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    puerto = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=puerto)