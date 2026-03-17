# import os
# import sqlite3
# import urllib.parse
# from datetime import datetime, timedelta
# from flask import Flask, request, jsonify
# import requests
# import gspread
# from apscheduler.schedulers.background import BackgroundScheduler
# import google.generativeai as genai
# import json

# app = Flask(__name__)

# # ==========================================
# # CONFIGURACIÓN Y CREDENCIALES
# # ==========================================
# TOKEN_DE_VERIFICACION = "madera_tools_secreto_2026"
# CLOUD_API_TOKEN = "EAAUkLctR4q0BQ8mcvr7YtqEacloCMCDHq1AY8VE0gc0ZBIIZBboTSCSEIEOQQKbNtfD7i0HwqiJvnd9FZCdH27rlBVsOXer1Qmlx3N5GAMhO6FmRNmYwOuxCKcJAgqo9Xy8IwtiQcZCFcuJ2fIMQnO7mPvBjEYrAgCDs7eMyn1lZAT7aDaJ8SKG5I1cp7yAZDZD"
# PHONE_NUMBER_ID = "1041050652417644"

# # 🔑 ¡AGREGÁ TU API KEY DE GEMINI ACÁ!
# GEMINI_API_KEY = "TU_API_KEY_DE_GEMINI_AQUI"
# genai.configure(api_key=GEMINI_API_KEY)

# RUTA_CREDENCIALES = "/etc/secrets/credenciales.json" 
# NOMBRE_HOJA = "Base de datos wt"

# # ==========================================
# # BASE DE DATOS LOCAL (Agregamos Memoria de Chat)
# # ==========================================
# def init_db():
#     conn = sqlite3.connect('memoria_mensajes.db')
#     c = conn.cursor()
#     c.execute('''CREATE TABLE IF NOT EXISTS mensajes (id TEXT PRIMARY KEY, telefono TEXT, estado TEXT, fecha TIMESTAMP)''')
#     c.execute('''CREATE TABLE IF NOT EXISTS metricas_diarias (fecha TEXT PRIMARY KEY, enviados INTEGER, entregados INTEGER, leidos INTEGER, respondidos INTEGER)''')
#     # NUEVA TABLA PARA LA MEMORIA DE GEMINI
#     c.execute('''CREATE TABLE IF NOT EXISTS chat_sesiones (telefono TEXT PRIMARY KEY, historial TEXT, ultima_interaccion TIMESTAMP, advertido INTEGER DEFAULT 0)''')
#     conn.commit()
#     conn.close()

# init_db()

# # ... [Tus funciones registrar_metrica y bloquear_numero_en_sheets quedan IGUAL] ...
# def registrar_metrica(evento):
#     try:
#         fecha_hoy = datetime.now().strftime('%Y-%m-%d')
#         conn = sqlite3.connect('memoria_mensajes.db')
#         c = conn.cursor()
#         c.execute("INSERT OR IGNORE INTO metricas_diarias (fecha, enviados, entregados, leidos, respondidos) VALUES (?, 0, 0, 0, 0)", (fecha_hoy,))
#         if evento == 'sent': c.execute("UPDATE metricas_diarias SET enviados = enviados + 1 WHERE fecha = ?", (fecha_hoy,))
#         elif evento == 'delivered': c.execute("UPDATE metricas_diarias SET entregados = entregados + 1 WHERE fecha = ?", (fecha_hoy,))
#         elif evento == 'read': c.execute("UPDATE metricas_diarias SET leidos = leidos + 1 WHERE fecha = ?", (fecha_hoy,))
#         elif evento == 'responded': c.execute("UPDATE metricas_diarias SET respondidos = respondidos + 1 WHERE fecha = ?", (fecha_hoy,))
#         conn.commit(); conn.close()
#     except Exception as e: print(f"Error guardando métrica: {e}", flush=True)

# def bloquear_numero_en_sheets(telefono):
#     try:
#         gc = gspread.service_account(filename=RUTA_CREDENCIALES)
#         sh = gc.open(NOMBRE_HOJA)
#         for ws in sh.worksheets():
#             try:
#                 celda = ws.find(telefono)
#                 if celda:
#                     ws.update_cell(celda.row, celda.col, f"0000{telefono}")
#                     return True
#             except gspread.exceptions.CellNotFound: continue
#     except Exception as e: print(f"❌ Error conectando a Sheets: {e}", flush=True)

# # ==========================================
# # TAREAS EN SEGUNDO PLANO (Cron)
# # ==========================================
# def revisar_rutinas_de_tiempo():
#     conn = sqlite3.connect('memoria_mensajes.db')
#     c = conn.cursor()
#     ahora = datetime.now()
    
#     # 1. Rutina original: Bloquear si no recibe en 48hs
#     hace_48_horas = ahora - timedelta(hours=48)
#     c.execute("SELECT id, telefono FROM mensajes WHERE estado='sent' AND fecha < ?", (hace_48_horas,))
#     for msg_id, telefono in c.fetchall():
#         bloquear_numero_en_sheets(telefono)
#         c.execute("DELETE FROM mensajes WHERE id=?", (msg_id,))
        
#     # 2. Rutina nueva: Avisar a las 2h 50m que se borra la charla y borrar a las 3hs
#     hace_2h50m = ahora - timedelta(hours=2, minutes=50)
#     hace_3_horas = ahora - timedelta(hours=3)
    
#     # Avisar
#     c.execute("SELECT telefono FROM chat_sesiones WHERE ultima_interaccion < ? AND advertido = 0", (hace_2h50m,))
#     for (telefono,) in c.fetchall():
#         enviar_mensaje_whatsapp(telefono, "⚠️ Hola! Por cuestiones de seguridad, en 10 minutos se cerrará nuestra sesión de chat y perderé el hilo de nuestra conversación. ¿Te quedó alguna duda pendiente o te paso con un asesor?")
#         c.execute("UPDATE chat_sesiones SET advertido = 1 WHERE telefono = ?", (telefono,))
        
#     # Borrar memoria
#     c.execute("DELETE FROM chat_sesiones WHERE ultima_interaccion < ?", (hace_3_horas,))
    
#     conn.commit(); conn.close()

# scheduler = BackgroundScheduler()
# scheduler.add_job(func=revisar_rutinas_de_tiempo, trigger="interval", minutes=10) # Revisa cada 10 mins
# scheduler.start()

# # ==========================================
# # CEREBRO IA: INTEGRACIÓN CON GEMINI
# # ==========================================
# PROMPT_SISTEMA = """
# Eres un asistente virtual de ventas experto, empático y humano de WoodTools.
# Tu objetivo es perfilar al cliente antes de pasarlo a un vendedor humano. 
# Habla en español argentino (usa 'vos', pero de forma profesional y amable).
# Usa formato de WhatsApp (*negritas* y emojis), NUNCA uses markdown de Markdown (**).

# REGLAS ESTRICTAS:
# 1. NUNCA, BAJO NINGUNA CIRCUNSTANCIA, DES PRECIOS. Si te piden precio, di que esa info la tiene el vendedor y procede a enviarle el link del vendedor.
# 2. Tu misión es averiguar: a) Qué herramienta busca, b) Qué material va a cortar, c) Si necesita cortar algo más.
# 3. Si el cliente tiene dudas técnicas, respóndelas, PERO siempre indaga un poco más para perfilarlo (Ej: Si pregunta por cantidad de dientes, explícale brevemente y pregúntale "¿De cuántos dientes estabas buscando vos?").
# 4. Cuando el cliente diga que NO tiene más dudas, o pregunte por el PRECIO, pregúntale si tiene algún vendedor de preferencia (Carlos, Valentín, Emmanuel o Ariel).
# 5. Cuando elija vendedor (o si le da igual), DESPÍDETE Y ENVIALE EL LINK.
   
# FORMATO DEL LINK (MUY IMPORTANTE):
# Arma un link de WhatsApp con el resumen de lo que hablaron. Reemplaza los espacios del resumen por '%20'.
# Numeros de vendedores:
# - Carlos: 5491165630406
# - Valentín: 5491145394279
# - Emmanuel: 5491157528428
# - Ariel: 5491100000000 (PONER NUMERO REAL)
# Ejemplo de salida final: "Perfecto, te paso el link de Carlos para que te cotice: https://wa.me/5491165630406?text=Hola%20Carlos%20busco%20sierras%20para%20cortar%20melamina"
# """

# def procesar_mensaje_con_gemini(telefono_cliente, texto_entrante):
#     conn = sqlite3.connect('memoria_mensajes.db')
#     c = conn.cursor()
#     c.execute("SELECT historial FROM chat_sesiones WHERE telefono = ?", (telefono_cliente,))
#     resultado = c.fetchone()
    
#     # Cargar historial o empezar de cero
#     if resultado:
#         historial = json.loads(resultado[0])
#     else:
#         historial = [
#             {"role": "user", "parts": [PROMPT_SISTEMA]},
#             {"role": "model", "parts": ["Entendido. Soy el asistente de WoodTools. Respetaré las reglas, el formato de WhatsApp y nunca daré precios."]}
#         ]
        
#     historial.append({"role": "user", "parts": [texto_entrante]})
    
#     # Llamar a Gemini
#     try:
#         model = genai.GenerativeModel('gemini-1.5-flash')
#         chat = model.start_chat(history=historial[:-1]) # Cargamos historia
#         respuesta = chat.send_message(texto_entrante)
#         texto_respuesta = respuesta.text
        
#         # Guardar nueva memoria
#         historial.append({"role": "model", "parts": [texto_respuesta]})
#         c.execute("INSERT OR REPLACE INTO chat_sesiones (telefono, historial, ultima_interaccion, advertido) VALUES (?, ?, ?, 0)", 
#                   (telefono_cliente, json.dumps(historial), datetime.now()))
#         conn.commit()
        
#         # Mandar WhatsApp
#         enviar_mensaje_whatsapp(telefono_cliente, texto_respuesta)
        
#     except Exception as e:
#         print(f"Error con Gemini: {e}")
#         enviar_mensaje_whatsapp(telefono_cliente, "Dame un segundito que estoy revisando el catálogo...")
#     finally:
#         conn.close()

# def enviar_mensaje_whatsapp(telefono_destino, texto):
#     url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
#     headers = {"Authorization": f"Bearer {CLOUD_API_TOKEN}", "Content-Type": "application/json"}
#     data = {"messaging_product": "whatsapp", "to": telefono_destino, "type": "text", "text": {"body": texto}}
#     requests.post(url, headers=headers, json=data)

# # ==========================================
# # RUTAS DEL WEBHOOK
# # ==========================================
# @app.route('/', methods=['GET', 'POST'])
# def inicio():
#     return "🚀 Webhook WoodTools + IA Gemini 🚀", 200

# @app.route('/webhook', methods=['GET'])
# def verificar_webhook():
#     mode = request.args.get('hub.mode')
#     token = request.args.get('hub.verify_token')
#     if mode == 'subscribe' and token == TOKEN_DE_VERIFICACION: 
#         return request.args.get('hub.challenge'), 200
#     return 'Faltan parámetros', 400

# @app.route('/webhook', methods=['POST'])
# def recibir_notificaciones():
#     cuerpo = request.get_json()
#     if cuerpo:
#         try:
#             cambios = cuerpo['entry'][0]['changes'][0]['value']
            
#             # SI ES UN MENSAJE ENTRANTE (TEXTO)
#             if 'messages' in cambios:
#                 mensaje = cambios['messages'][0]
#                 if mensaje['type'] == 'text': # Validamos que nos manden texto y no audios/fotos
#                     telefono_cliente = mensaje['from']
#                     texto_cliente = mensaje['text']['body']
                    
#                     print(f"📩 MENSAJE de {telefono_cliente}: {texto_cliente}", flush=True)
#                     registrar_metrica('responded') 
#                     procesar_mensaje_con_gemini(telefono_cliente, texto_cliente)
                
#             # SI ES UN CAMBIO DE ESTADO
#             elif 'statuses' in cambios:
#                 estado = cambios['statuses'][0]['status'] 
#                 telefono = cambios['statuses'][0]['recipient_id']
#                 msg_id = cambios['statuses'][0]['id']
                
#                 registrar_metrica(estado)
                
#                 conn = sqlite3.connect('memoria_mensajes.db')
#                 c = conn.cursor()
#                 if estado == 'sent':
#                     c.execute("INSERT OR REPLACE INTO mensajes (id, telefono, estado, fecha) VALUES (?, ?, ?, ?)", (msg_id, telefono, estado, datetime.now()))
#                 elif estado in ['delivered', 'read']:
#                     c.execute("DELETE FROM mensajes WHERE id=?", (msg_id,))
#                 elif estado == 'failed':
#                     bloquear_numero_en_sheets(telefono)
#                     c.execute("DELETE FROM mensajes WHERE id=?", (msg_id,))
#                 conn.commit(); conn.close()
                
#         except Exception as e: pass
#     return jsonify({"status": "ok"}), 200

# if __name__ == '__main__':
#     puerto = int(os.environ.get('PORT', 5000))
#     app.run(host='0.0.0.0', port=puerto)