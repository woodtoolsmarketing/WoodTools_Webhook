from flask import Flask, request, jsonify
import urllib.parse
import requests
import os

app = Flask(__name__)

# ==========================================
# CREDENCIALES
# ==========================================
TOKEN_DE_VERIFICACION = "madera_tools_secreto_2026"
CLOUD_API_TOKEN = "TU_TOKEN_AQUI" # Reemplazar con el token real
PHONE_NUMBER_ID = "TU_ID_TELEFONO_AQUI" # Reemplazar con el ID real

@app.route('/', methods=['GET'])
def inicio():
    return "🚀 El Webhook de WoodTools está funcionando perfectamente 🚀", 200

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
            
            # 1. SI RECIBIMOS UN MENSAJE DE TEXTO (El cliente nos respondió)
            if 'messages' in cambios:
                mensaje_entrante = cambios['messages'][0]
                telefono_cliente = mensaje_entrante['from']
                
                print(f"📩 NUEVO MENSAJE de {telefono_cliente}. Enviando auto-respuesta...")
                enviar_respuesta_automatica(telefono_cliente)
                
            # 2. SI RECIBIMOS UN CAMBIO DE ESTADO (Entregado, Leído, etc.)
            elif 'statuses' in cambios:
                estado = cambios['statuses'][0]['status'] 
                telefono = cambios['statuses'][0]['recipient_id']
                print(f"✅ ESTADO: El teléfono {telefono} está en estado: {estado.upper()}")
                
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