import os
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import requests
import gspread
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# ==========================================
# CONFIGURACIÓN Y CREDENCIALES
# ==========================================
TOKEN_DE_VERIFICACION = "madera_tools_secreto_2026"
CLOUD_API_TOKEN = "EAAUkLctR4q0BQ8mcvr7YtqEacloCMCDHq1AY8VE0gc0ZBIIZBboTSCSEIEOQQKbNtfD7i0HwqiJvnd9FZCdH27rlBVsOXer1Qmlx3N5GAMhO6FmRNmYwOuxCKcJAgqo9Xy8IwtiQcZCFcuJ2fIMQnO7mPvBjEYrAgCDs7eMyn1lZAT7aDaJ8SKG5I1cp7yAZDZD" # Reemplazar con el token real
PHONE_NUMBER_ID = "1041050652417644" # Reemplazar con el ID real

# Ruta al archivo JSON que subiremos a Render a través de "Secret Files"
RUTA_CREDENCIALES = "/etc/secrets/credenciales.json" 
NOMBRE_HOJA = "Base de datos wt"

# ==========================================
# BASE DE DATOS LOCAL (Para la memoria de 48hs)
# ==========================================
def init_db():
    conn = sqlite3.connect('memoria_mensajes.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS mensajes 
                 (id TEXT PRIMARY KEY, telefono TEXT, estado TEXT, fecha TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()

# ==========================================
# CONEXIÓN A GOOGLE SHEETS Y BLOQUEO
# ==========================================
def bloquear_numero_en_sheets(telefono):
    try:
        # 1. Nos conectamos usando la llave del robot
        gc = gspread.service_account(filename=RUTA_CREDENCIALES)
        sh = gc.open(NOMBRE_HOJA)
        
        # 2. Buscamos en todas las pestañas de tu Excel
        for ws in sh.worksheets():
            try:
                # Buscamos la celda exacta que tiene este teléfono
                celda = ws.find(telefono)
                if celda:
                    nuevo_valor = f"0000{telefono}"
                    ws.update_cell(celda.row, celda.col, nuevo_valor)
                    print(f"🚫 NÚMERO BLOQUEADO EN SHEETS: {telefono} (Pestaña: {ws.title})")
                    return True
            except gspread.exceptions.CellNotFound:
                continue # No lo encontró en esta pestaña, sigue buscando
        print(f"⚠️ No se encontró el teléfono {telefono} en la planilla para bloquearlo.")
    except Exception as e:
        print(f"❌ Error conectando a Sheets: {e}")

# ==========================================
# LÓGICA DEL TEMPORIZADOR (48 HORAS)
# ==========================================
def revisar_mensajes_vencidos():
    print("🔍 Revisando mensajes de hace 48 horas...")
    conn = sqlite3.connect('memoria_mensajes.db')
    c = conn.cursor()
    
    hace_48_horas = datetime.now() - timedelta(hours=48)
    
    # Buscamos los que siguen en "sent" y ya pasaron 48hs
    c.execute("SELECT id, telefono FROM mensajes WHERE estado='sent' AND fecha < ?", (hace_48_horas,))
    vencidos = c.fetchall()
    
    for msg_id, telefono in vencidos:
        print(f"⏰ TIEMPO AGOTADO: El número {telefono} nunca recibió el mensaje. Bloqueando...")
        bloquear_numero_en_sheets(telefono)
        # Lo borramos de la memoria
        c.execute("DELETE FROM mensajes WHERE id=?", (msg_id,))
        
    conn.commit()
    conn.close()

# Iniciamos el reloj que revisa silenciosamente cada 1 hora
scheduler = BackgroundScheduler()
scheduler.add_job(func=revisar_mensajes_vencidos, trigger="interval", hours=1)
scheduler.start()

# ==========================================
# RUTAS DEL WEBHOOK
# ==========================================
@app.route('/', methods=['GET'])
def inicio():
    return "🚀 El Webhook de WoodTools + CRM Automático está funcionando 🚀", 200

@app.route('/webhook', methods=['GET'])
def verificar_webhook():
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')

    if mode and token:
        if mode == 'subscribe' and token == TOKEN_DE_VERIFICACION:
            print("¡Meta ha verificado nuestro webhook con éxito!")
            return challenge, 200
        else:
            return 'Token de verificación incorrecto', 403
    return 'Faltan parámetros', 400

@app.route('/webhook', methods=['POST'])
def recibir_notificaciones():
    cuerpo = request.get_json()

    if cuerpo:
        try:
            cambios = cuerpo['entry'][0]['changes'][0]['value']
            
            # 1. SI RECIBIMOS UN MENSAJE DE TEXTO (Respuestas / Clics en el link)
            if 'messages' in cambios:
                mensaje_entrante = cambios['messages'][0]
                telefono_cliente = mensaje_entrante['from']
                
                print(f"📩 NUEVO MENSAJE de {telefono_cliente}. Enviando auto-respuesta...")
                enviar_respuesta_automatica(telefono_cliente)
                
            # 2. SI RECIBIMOS UN CAMBIO DE ESTADO (Sent, Delivered, Read, Failed)
            elif 'statuses' in cambios:
                estado = cambios['statuses'][0]['status'] 
                telefono = cambios['statuses'][0]['recipient_id']
                msg_id = cambios['statuses'][0]['id']
                
                print(f"✅ ESTADO: {telefono} -> {estado.upper()}")
                
                conn = sqlite3.connect('memoria_mensajes.db')
                c = conn.cursor()
                
                if estado == 'sent':
                    # Lo anotamos en el cuaderno con la hora actual
                    c.execute("INSERT OR REPLACE INTO mensajes (id, telefono, estado, fecha) VALUES (?, ?, ?, ?)", 
                              (msg_id, telefono, estado, datetime.now()))
                
                elif estado in ['delivered', 'read']:
                    # ¡Llegó bien! Lo borramos de la lista de peligro
                    c.execute("DELETE FROM mensajes WHERE id=?", (msg_id,))
                    
                elif estado == 'failed':
                    # ¡Fallo instantáneo! (Sin WhatsApp) -> Bloquear ya mismo
                    print(f"💀 FALLO INMEDIATO: El número {telefono} rebotó. Bloqueando...")
                    bloquear_numero_en_sheets(telefono)
                    c.execute("DELETE FROM mensajes WHERE id=?", (msg_id,))
                    
                conn.commit()
                conn.close()
                
        except Exception as e:
            # Si Meta manda un formato raro, lo ignoramos para que no colapse
            pass

        return jsonify({"status": "ok"}), 200
    
    return "Sin datos", 400

# ==========================================
# FUNCIÓN DE AUTO-RESPUESTA CON LINK
# ==========================================
def enviar_respuesta_automatica(telefono_destino):
    # Generamos el link de wa.me genérico para el asesor principal
    telefono_asesor = "5491145394279" # Teléfono general de recepción (ej. Valentín)
    texto_prearmado = "Hola, me contacto desde la notificación para realizar una consulta."
    link_codificado = urllib.parse.quote(texto_prearmado)
    link_wa = f"https://wa.me/{telefono_asesor}?text={link_codificado}"
    
    # Armamos el mensaje final
    mensaje_texto = f"Este medio es únicamente para enviarte la notificación. Para hablar con un asesor y obtener mayor información te pido que entres al link 👉 {link_wa}"
    
    # Enviamos el mensaje a Meta
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {CLOUD_API_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": telefono_destino,
        "type": "text",
        "text": {"body": mensaje_texto}
    }
    
    try:
        requests.post(url, headers=headers, json=data)
    except Exception as e:
        print(f"Error enviando la auto-respuesta: {e}")

if __name__ == '__main__':
    puerto = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=puerto)