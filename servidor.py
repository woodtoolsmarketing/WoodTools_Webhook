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
# CONFIGURACIÓN Y CREDENCIALES
# ==========================================
TOKEN_DE_VERIFICACION = "madera_tools_secreto_2026"
CLOUD_API_TOKEN = "EAAUkLctR4q0BQ8mcvr7YtqEacloCMCDHq1AY8VE0gc0ZBIIZBboTSCSEIEOQQKbNtfD7i0HwqiJvnd9FZCdH27rlBVsOXer1Qmlx3N5GAMhO6FmRNmYwOuxCKcJAgqo9Xy8IwtiQcZCFcuJ2fIMQnO7mPvBjEYrAgCDs7eMyn1lZAT7aDaJ8SKG5I1cp7yAZDZD"
PHONE_NUMBER_ID = "1041050652417644"

# 🔑 ¡AGREGÁ TU API KEY DE GEMINI ACÁ! 
GEMINI_API_KEY = "AIzaSyCZDagaYBtLiAtwd3CyUWPDWQL7hEB5EiE"
genai.configure(api_key=GEMINI_API_KEY)

RUTA_CREDENCIALES = "/etc/secrets/credenciales.json" 
NOMBRE_HOJA = "Base de datos wt"

VENDEDORES_POR_NUMERO = {
    "5491145394279": "Valentín",
    "5491165630406": "Carlos",
    "5491157528428": "Emmanuel",
    "5491100000000": "Ariel" # REEMPLAZAR POR EL DE ARIEL
}

def limpiar_numero(num):
    return ''.join(filter(str.isdigit, str(num)))

def extraer_10_digitos(num):
    solo_numeros = limpiar_numero(num)
    return solo_numeros[-10:] if len(solo_numeros) >= 10 else solo_numeros

# ==========================================
# BASE DE DATOS LOCAL (Render)
# ==========================================
def init_db():
    conn = sqlite3.connect('memoria_mensajes.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS mensajes (id TEXT PRIMARY KEY, telefono TEXT, estado TEXT, fecha TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS metricas_diarias (fecha TEXT PRIMARY KEY, enviados INTEGER, entregados INTEGER, leidos INTEGER, respondidos INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_sesiones (telefono TEXT PRIMARY KEY, historial TEXT, ultima_interaccion TIMESTAMP, advertido INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS asignaciones_v2 (telefono_cliente TEXT PRIMARY KEY, numero_vendedor TEXT, tipo_campana TEXT, subtipo TEXT)''')
    conn.commit()
    conn.close()

init_db()

def registrar_metrica(evento):
    try:
        fecha_hoy = datetime.now().strftime('%Y-%m-%d')
        conn = sqlite3.connect('memoria_mensajes.db')
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO metricas_diarias (fecha, enviados, entregados, leidos, respondidos) VALUES (?, 0, 0, 0, 0)", (fecha_hoy,))
        if evento == 'sent': c.execute("UPDATE metricas_diarias SET enviados = enviados + 1 WHERE fecha = ?", (fecha_hoy,))
        elif evento == 'delivered': c.execute("UPDATE metricas_diarias SET entregados = entregados + 1 WHERE fecha = ?", (fecha_hoy,))
        elif evento == 'read': c.execute("UPDATE metricas_diarias SET leidos = leidos + 1 WHERE fecha = ?", (fecha_hoy,))
        elif evento == 'responded': c.execute("UPDATE metricas_diarias SET respondidos = respondidos + 1 WHERE fecha = ?", (fecha_hoy,))
        conn.commit(); conn.close()
    except Exception as e: pass

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
    
    hace_48_horas = ahora - timedelta(hours=48)
    c.execute("SELECT id, telefono FROM mensajes WHERE estado='sent' AND fecha < ?", (hace_48_horas,))
    for msg_id, telefono in c.fetchall():
        bloquear_numero_en_sheets(telefono)
        c.execute("DELETE FROM mensajes WHERE id=?", (msg_id,))
        
    hace_2h50m = ahora - timedelta(hours=2, minutes=50)
    hace_3_horas = ahora - timedelta(hours=3)
    
    c.execute("SELECT telefono FROM chat_sesiones WHERE ultima_interaccion < ? AND advertido = 0", (hace_2h50m,))
    for (telefono,) in c.fetchall():
        enviar_mensaje_whatsapp(telefono, "⚠️ Hola! Por cuestiones de seguridad, en 10 minutos se cerrará nuestra sesión de chat y perderé el hilo de nuestra conversación. ¿Te paso con tu asesor?")
        c.execute("UPDATE chat_sesiones SET advertido = 1 WHERE telefono = ?", (telefono,))
        
    c.execute("DELETE FROM chat_sesiones WHERE ultima_interaccion < ?", (hace_3_horas,))
    conn.commit(); conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(func=revisar_rutinas_de_tiempo, trigger="interval", minutes=10)
scheduler.start()

# ==========================================
# CEREBRO IA: GENERADOR DE PROMPTS Y MEMORIA
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
    nombre_vend = VENDEDORES_POR_NUMERO.get(tel_vend, "un asesor")
    
    plantillas = {
        "Promociones": "Hola, vengo por la promoción de [herramienta] para [material] y quisiera tener más información",
        "Rescate (Te extrañamos)": "Hola, vengo por el anuncio que me enviaron y quería novedades sobre [herramienta] para [material]",
        "Gira Vendedor": f"Hola, me comentaron que {nombre_vend} va a estar visitando mi zona dentro de poco. Quisiera poder arreglar para una visita",
        "Personalizado": "Hola Vengo por el anuncio de [herramienta] para [material]",
        "Recotización": "Hola, vengo del aviso de una recotización sobre [herramienta]"
    }
    
    if tipo_camp == "Novedades":
        if subtipo == "Ingresos":
            frase_link = "Hola me comento que tuvieron un nuevo ingreso de [herramienta]"
        else:
            frase_link = "Hola, me comentaron que entró nuevo stock de [herramienta]"
    else:
        frase_link = plantillas.get(tipo_camp, plantillas["Promociones"])

    link_base = f"https://wa.me/{tel_vend}?text="
    
    return f"""
Eres el asistente virtual de recepción de WoodTools. 
Habla en español argentino (usa 'vos', empático y servicial).
Usa formato de WhatsApp (*negritas* y emojis), NUNCA uses markdown de asteriscos dobles (**).

CONTEXTO:
El cliente recibió una campaña del tipo "{tipo_camp}" y está respondiendo.

TUS REGLAS ESTRICTAS:
1. Saluda cordialmente. ESTÁ PROHIBIDO USAR LA PALABRA "CAMPAÑA".
2. Tu objetivo es obtener la información necesaria para completar la siguiente frase: "{frase_link}".
   - Si la frase tiene [herramienta] y [material], pregúntale ambas cosas al cliente sutilmente.
   - Si la frase NO tiene corchetes (ej. Gira Vendedor), NO preguntes nada de herramientas ni materiales, solo despídete y pasa el link.
3. NO respondas preguntas técnicas, NO des precios, NO des información de stock.
4. El vendedor asignado es **{nombre_vend}**.
5. Una vez que tengas los datos para llenar los corchetes de la frase, interrumpe amablemente, dile que {nombre_vend} lo ayudará, y DESPÍDETE ENVIANDO EL LINK.

FORMATO DEL LINK (MUY IMPORTANTE):
DEBES usar EXACTAMENTE esta frase para el texto del link: "{frase_link}"
Reemplaza los campos [herramienta] y [material] con lo que te dijo el cliente.
Codifica los espacios con '%20'.
El link EXACTO que debes usar como base es este: {link_base}
"""

def procesar_mensaje_con_gemini(telefono_cliente, texto_entrante):
    conn = sqlite3.connect('memoria_mensajes.db')
    c = conn.cursor()
    c.execute("SELECT historial FROM chat_sesiones WHERE telefono = ?", (telefono_cliente,))
    resultado = c.fetchone()
    
    # Buscamos SIEMPRE las instrucciones de la campaña actual
    prompt_dinamico = obtener_prompt_personalizado(telefono_cliente)
    
    if resultado:
        historial = json.loads(resultado[0])
        # ESTA ES LA MAGIA ANTI-MEMORIA VIEJA: 
        # Forzamos a la IA a que se actualice con el vendedor de la nueva campaña, 
        # incluso si ya venían charlando de otra cosa antes.
        if len(historial) > 0 and historial[0]["role"] == "user":
            historial[0]["parts"] = [prompt_dinamico]
    else:
        historial = [
            {"role": "user", "parts": [prompt_dinamico]},
            {"role": "model", "parts": ["Entendido. Soy el asistente. Haré las preguntas justas para rellenar los corchetes de la frase predefinida, y luego armaré el link exactamente con esa frase codificada."]}
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
    return "🚀 Webhook WoodTools + IA Gemini (Solución Memoria) 🚀", 200

@app.route('/asignar_vendedor', methods=['POST'])
def asignar_vendedor():
    data = request.json
    telefono_cliente_10 = extraer_10_digitos(data.get('cliente', ''))
    numero_vendedor = limpiar_numero(data.get('vendedor_tel', ''))
    tipo_campana = data.get('tipo_campana', 'Promociones')
    subtipo = data.get('subtipo', '')
    
    if telefono_cliente_10 and numero_vendedor:
        conn = sqlite3.connect('memoria_mensajes.db')
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO asignaciones_v2 (telefono_cliente, numero_vendedor, tipo_campana, subtipo) VALUES (?, ?, ?, ?)", 
                  (telefono_cliente_10, numero_vendedor, tipo_campana, subtipo))
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
                    registrar_metrica('responded') 
                    procesar_mensaje_con_gemini(telefono_cliente, texto_cliente)
                
            elif 'statuses' in cambios:
                estado = cambios['statuses'][0]['status'] 
                telefono = limpiar_numero(cambios['statuses'][0]['recipient_id'])
                msg_id = cambios['statuses'][0]['id']
                
                registrar_metrica(estado)
                
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

if __name__ == '__main__':
    puerto = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=puerto)