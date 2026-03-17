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

# 🔑 ¡AGREGÁ TU API KEY DE GEMINI ACÁ! (Por seguridad quité la anterior)
GEMINI_API_KEY = "AIzaSyAksvQQznQ0JWZd5ZqWmLVAagsmhvNcq8U"
genai.configure(api_key=GEMINI_API_KEY)

RUTA_CREDENCIALES = "/etc/secrets/credenciales.json" 
NOMBRE_HOJA = "Base de datos wt"

# DICCIONARIO INVERSO PARA SABER QUIÉN ES EL VENDEDOR SEGÚN SU NÚMERO
VENDEDORES_POR_NUMERO = {
    "5491145394279": "Valentín",
    "5491165630406": "Carlos",
    "5491157528428": "Emmanuel",
    "5491100000000": "Ariel" # REEMPLAZAR POR EL DE ARIEL
}

# ==========================================
# BASE DE DATOS LOCAL
# ==========================================
def init_db():
    conn = sqlite3.connect('memoria_mensajes.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS mensajes (id TEXT PRIMARY KEY, telefono TEXT, estado TEXT, fecha TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS metricas_diarias (fecha TEXT PRIMARY KEY, enviados INTEGER, entregados INTEGER, leidos INTEGER, respondidos INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_sesiones (telefono TEXT PRIMARY KEY, historial TEXT, ultima_interaccion TIMESTAMP, advertido INTEGER DEFAULT 0)''')
    # NUEVA TABLA: Para recordar quién es el vendedor de cada cliente
    c.execute('''CREATE TABLE IF NOT EXISTS asignaciones (telefono_cliente TEXT PRIMARY KEY, numero_vendedor TEXT)''')
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

# ==========================================
# TAREAS EN SEGUNDO PLANO (Cron)
# ==========================================
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
# CEREBRO IA: GENERADOR DE PROMPTS DINÁMICOS
# ==========================================
def obtener_prompt_personalizado(telefono_cliente):
    conn = sqlite3.connect('memoria_mensajes.db')
    c = conn.cursor()
    c.execute("SELECT numero_vendedor FROM asignaciones WHERE telefono_cliente = ?", (telefono_cliente,))
    res = c.fetchone()
    conn.close()

    # Si encontramos el vendedor asignado, lo usamos. Si no, mandamos uno por defecto (ej: Valentín)
    tel_vend = res[0] if res else "5491145394279"
    nombre_vend = VENDEDORES_POR_NUMERO.get(tel_vend, "un asesor")
    
    link_base = f"https://wa.me/{tel_vend}?text="
    
    return f"""
Eres un asistente virtual de recepción rápida para WoodTools. 
Habla en español argentino (usa 'vos', pero de forma profesional y amable).
Usa formato de WhatsApp (*negritas* y emojis), NUNCA uses markdown de asteriscos dobles (**).

REGLAS ESTRICTAS Y LIMITADAS:
1. Tu ÚNICO y exclusivo objetivo es averiguar dos cosas del cliente: a) Qué tipo de herramienta busca, y b) Qué material desea cortar.
2. NO respondas preguntas técnicas, NO des precios, NO des información de stock ni entres en conversaciones largas.
3. El vendedor asignado a este cliente en particular es **{nombre_vend}**. NO preguntes con quién quiere hablar, porque ya lo sabemos.
4. Si el cliente te hace CUALQUIER OTRA PREGUNTA (ej: "¿Cuánto cuesta?", "¿Hacen envíos?"), interrumpe amablemente, dile que {nombre_vend} lo ayudará con esa consulta y pasa directamente a la regla 5.
5. Una vez que tengas el tipo de herramienta y el material, O cuando el cliente haga una pregunta fuera de libreto, DESPÍDETE Y ENVIALE EL LINK DIRECTO DE {nombre_vend.upper()}.

FORMATO DEL LINK (MUY IMPORTANTE):
Arma un link con el resumen de la información recolectada. Reemplaza los espacios por '%20'.
El link EXACTO que debes usar como base es este: {link_base}
Ejemplo de salida: "Perfecto, te paso directamente con {nombre_vend} para que te cotice: {link_base}Hola%20{nombre_vend}%20busco%20sierras%20para%20cortar%20melamina"
"""

def procesar_mensaje_con_gemini(telefono_cliente, texto_entrante):
    conn = sqlite3.connect('memoria_mensajes.db')
    c = conn.cursor()
    c.execute("SELECT historial FROM chat_sesiones WHERE telefono = ?", (telefono_cliente,))
    resultado = c.fetchone()
    
    if resultado:
        historial = json.loads(resultado[0])
    else:
        # Generamos el prompt dinámico con el vendedor que le toca a este cliente
        prompt_dinamico = obtener_prompt_personalizado(telefono_cliente)
        historial = [
            {"role": "user", "parts": [prompt_dinamico]},
            {"role": "model", "parts": ["Entendido. Soy el asistente de recepción. Solo preguntaré por herramienta y material. Ante cualquier otra duda o al terminar, derivaré al cliente usando su link personalizado."]}
        ]
        
    historial.append({"role": "user", "parts": [texto_entrante]})
    
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
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
        # ACA ESTÁ EL TRUCO: Te manda el error directamente a WhatsApp
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
    return "🚀 Webhook WoodTools + IA Gemini (Vendedor Automático) 🚀", 200

# NUEVA RUTA: El "puente" para recibir avisos de la PC
@app.route('/asignar_vendedor', methods=['POST'])
def asignar_vendedor():
    data = request.json
    telefono_cliente = data.get('cliente')
    numero_vendedor = data.get('vendedor_tel')
    
    if telefono_cliente and numero_vendedor:
        conn = sqlite3.connect('memoria_mensajes.db')
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO asignaciones (telefono_cliente, numero_vendedor) VALUES (?, ?)", (telefono_cliente, numero_vendedor))
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
                    telefono_cliente = mensaje['from']
                    texto_cliente = mensaje['text']['body']
                    
                    print(f"📩 MENSAJE de {telefono_cliente}: {texto_cliente}", flush=True)
                    registrar_metrica('responded') 
                    procesar_mensaje_con_gemini(telefono_cliente, texto_cliente)
                
            elif 'statuses' in cambios:
                estado = cambios['statuses'][0]['status'] 
                telefono = cambios['statuses'][0]['recipient_id']
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