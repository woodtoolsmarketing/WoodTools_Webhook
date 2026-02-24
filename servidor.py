from flask import Flask, request, jsonify
import os

app = Flask(__name__)

# Este es un token inventado por nosotros. Es como una contraseña
# para que Meta sepa que está hablando con tu servidor y no con un impostor.
TOKEN_DE_VERIFICACION = "madera_tools_secreto_2026"

@app.route('/', methods=['GET'])
def inicio():
    return "🚀 El Webhook de WoodTools está funcionando perfectamente 🚀", 200

# 1. RUTA PARA QUE META VERIFIQUE EL SERVIDOR (Obligatorio)
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

# 2. RUTA PARA RECIBIR LAS NOTIFICACIONES (Leído, Entregado, etc.)
@app.route('/webhook', methods=['POST'])
def recibir_notificaciones():
    cuerpo = request.get_json()

    if cuerpo:
        try:
            # Aquí "desarmamos" el paquete de datos que nos manda Meta
            cambios = cuerpo['entry'][0]['changes'][0]['value']
            
            # Revisamos si es una notificación de estado (status)
            if 'statuses' in cambios:
                estado = cambios['statuses'][0]['status'] # Puede ser: sent, delivered, read, failed
                telefono = cambios['statuses'][0]['recipient_id']
                id_mensaje = cambios['statuses'][0]['id']
                
                print(f"✅ ESTADO ACTUALIZADO: El teléfono {telefono} está en estado: {estado.upper()}")
                
                # MÁS ADELANTE: Aquí pondremos el código para guardar esto en tu base de datos
                
        except Exception as e:
            # Si Meta manda un mensaje raro que no es de estado, lo ignoramos
            pass

        return jsonify({"status": "ok"}), 200
    
    return "Sin datos", 400

if __name__ == '__main__':
    # Render asigna el puerto automáticamente, por defecto usamos el 5000
    puerto = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=puerto)