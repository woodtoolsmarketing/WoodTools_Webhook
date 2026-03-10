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
CLOUD_API_TOKEN = "EAAUkLctR4q0BQ8mcvr7YtqEacloCMCDHq1AY8VE0gc0ZBIIZBboTSCSEIEOQQKbNtfD7i0HwqiJvnd9FZCdH27rlBVsOXer1Qmlx3N5GAMhO6FmRNmYwOuxCKcJAgqo9Xy8IwtiQcZCFcuJ2fIMQnO7mPvBjEYrAgCDs7eMyn1lZAT7aDaJ8SKG5I1cp7yAZDZD"
PHONE_NUMBER_ID = "1041050652417644"

RUTA_CREDENCIALES = "/etc/secrets/credenciales.json" 
NOMBRE_HOJA = "Base de datos wt"

# ==========================================
# BASE DE DATOS LOCAL (Memoria y Métricas)
# ==========================================
def init_db():
    conn = sqlite3.connect('memoria_mensajes.db')
    c = conn.cursor()
    # Tabla para bloqueos de 48hs
    c.execute('''CREATE TABLE IF NOT EXISTS mensajes 
                 (id TEXT PRIMARY KEY, telefono TEXT, estado TEXT, fecha TIMESTAMP)''')
    # NUEVA TABLA PARA MÉTRICAS GLOBALES
    c.execute('''CREATE TABLE IF NOT EXISTS metricas_diarias
                 (fecha TEXT PRIMARY KEY, enviados INTEGER, entregados INTEGER, leidos INTEGER, respondidos INTEGER)''')
    conn.commit()
    conn.close()

init_db()

def registrar_metrica(evento):
    """Suma +1 al evento del día (enviados, entregados, leidos, respondidos)"""
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
    except Exception as e:
        print(f"Error guardando métrica: {e}")

# ==========================================
# CONEXIÓN A GOOGLE SHEETS Y BLOQUEO
# ==========================================
def bloquear_numero_en_sheets(telefono):
    try:
        gc = gspread.service_account(filename=RUTA_CREDENCIALES)
        sh = gc.open(NOMBRE_HOJA)
        for ws in sh.worksheets():
            try:
                celda = ws.find(telefono)
                if celda:
                    ws.update_cell(celda.row, celda.col, f"0000{telefono}")
                    print(f"🚫 BLOQUEADO EN SHEETS: {telefono} (Pestaña: {ws.title})")
                    return True
            except gspread.exceptions.CellNotFound: continue
    except Exception as e: print(f"❌ Error conectando a Sheets: {e}")

# ==========================================
# LÓGICA DEL TEMPORIZADOR (48 HORAS)
# ==========================================
def revisar_mensajes_vencidos():
    print("🔍 Revisando mensajes de hace 48 horas...")
    conn = sqlite3.connect('memoria_mensajes.db')
    c = conn.cursor()
    hace_48_horas = datetime.now() - timedelta(hours=48)
    
    c.execute("SELECT id, telefono FROM mensajes WHERE estado='sent' AND fecha < ?", (hace_48_horas,))
    vencidos = c.fetchall()
    
    for msg_id, telefono in vencidos:
        print(f"⏰ TIEMPO AGOTADO: El número {telefono} nunca recibió el mensaje. Bloqueando...")
        bloquear_numero_en_sheets(telefono)
        c.execute("DELETE FROM mensajes WHERE id=?", (msg_id,))
        
    conn.commit(); conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(func=revisar_mensajes_vencidos, trigger="interval", hours=1)
scheduler.start()

# ==========================================
# RUTAS DEL WEBHOOK Y MÉTRICAS
# ==========================================
@app.route('/', methods=['GET'])
def inicio():
    return "🚀 El Webhook de WoodTools + CRM Automático está funcionando 🚀", 200

# ---> NUEVA RUTA PARA QUE TU PROGRAMA DE ESCRITORIO LEA LAS ESTADÍSTICAS <---
@app.route('/metricas', methods=['GET'])
def obtener_metricas():
    try:
        conn = sqlite3.connect('memoria_mensajes.db')
        c = conn.cursor()
        c.execute("SELECT fecha, enviados, entregados, leidos, respondidos FROM metricas_diarias")
        filas = c.fetchall()
        conn.close()
        
        # Devolvemos la info en formato JSON
        data = {row[0]: {"enviados": row[1], "entregados": row[2], "leidos": row[3], "respondidos": row[4]} for row in filas}
        return jsonify(data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/webhook', methods=['GET'])
def verificar_webhook():
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    if mode and token:
        if mode == 'subscribe' and token == TOKEN_DE_VERIFICACION: return request.args.get('hub.challenge'), 200
        else: return 'Token incorrecto', 403
    return 'Faltan parámetros', 400

@app.route('/webhook', methods=['POST'])
def recibir_notificaciones():
    cuerpo = request.get_json()
    if cuerpo:
        try:
            cambios = cuerpo['entry'][0]['changes'][0]['value']
            
            # 1. SI RECIBIMOS UN MENSAJE DIRECTO AL BOT
            if 'messages' in cambios:
                mensaje_entrante = cambios['messages'][0]
                telefono_cliente = mensaje_entrante['from']
                print(f"📩 NUEVO MENSAJE de {telefono_cliente}. Enviando auto-respuesta...")
                
                # Registramos que alguien interactuó con el Bot directamente
                registrar_metrica('responded') 
                enviar_respuesta_automatica(telefono_cliente)
                
            # 2. CAMBIOS DE ESTADO (Sent, Delivered, Read, Failed)
            elif 'statuses' in cambios:
                estado = cambios['statuses'][0]['status'] 
                telefono = cambios['statuses'][0]['recipient_id']
                msg_id = cambios['statuses'][0]['id']
                
                print(f"✅ ESTADO: {telefono} -> {estado.upper()}")
                
                # ANOTAMOS LA MÉTRICA PARA EL PANEL DE ESTADÍSTICAS
                registrar_metrica(estado)
                
                # LÓGICA DE MEMORIA PARA BLOQUEO DE 48HS
                conn = sqlite3.connect('memoria_mensajes.db')
                c = conn.cursor()
                if estado == 'sent':
                    c.execute("INSERT OR REPLACE INTO mensajes (id, telefono, estado, fecha) VALUES (?, ?, ?, ?)", 
                              (msg_id, telefono, estado, datetime.now()))
                elif estado in ['delivered', 'read']:
                    c.execute("DELETE FROM mensajes WHERE id=?", (msg_id,))
                elif estado == 'failed':
                    print(f"💀 FALLO INMEDIATO: El número {telefono} rebotó. Bloqueando...")
                    bloquear_numero_en_sheets(telefono)
                    c.execute("DELETE FROM mensajes WHERE id=?", (msg_id,))
                    
                conn.commit(); conn.close()
        except Exception: pass
        return jsonify({"status": "ok"}), 200
    return "Sin datos", 400

# ==========================================
# FUNCIÓN DE AUTO-RESPUESTA
# ==========================================
def enviar_respuesta_automatica(telefono_destino):
    telefono_asesor = "5491145394279" 
    texto_prearmado = "Hola, me contacto desde la notificación para realizar una consulta."
    link_wa = f"https://wa.me/{telefono_asesor}?text={urllib.parse.quote(texto_prearmado)}"
    mensaje_texto = f"Este medio es únicamente para enviarte la notificación. Para hablar con un asesor y obtener mayor información te pido que entres al link 👉 {link_wa}"
    
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {CLOUD_API_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": telefono_destino, "type": "text", "text": {"body": mensaje_texto}}
    
    try: 
        res = requests.post(url, headers=headers, json=data)
        # Log detallado para ver si Meta bloquea el mensaje
        print(f"➡️ Auto-respuesta enviada: [{res.status_code}] {res.text}")
    except Exception as e: 
        print(f"❌ Error enviando auto-respuesta: {e}")

if __name__ == '__main__':
    puerto = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=puerto)