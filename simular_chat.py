# Simula una conversacion REAL contra el bot (mismo prompt + mismas tools + misma DB
# + Gemini real), sin enviar WhatsApp. Sirve para validar anti-repeticion y specs.
import google.generativeai as genai
import psycopg2
from psycopg2 import pool
import servidor
from migrador import DATABASE_URL as URL_OK

# Apagar el scheduler de fondo que arranca al importar servidor (evita efectos)
try:
    servidor.scheduler.shutdown(wait=False)
except Exception:
    pass

# El DATABASE_URL local (tokens.json) no conecta; uso la URL que sí funciona
# para que las tools peguen al MISMO Supabase de produccion durante la prueba.
servidor.db_pool = psycopg2.pool.SimpleConnectionPool(1, 5, URL_OK, sslmode='require')
assert servidor.execute_db_query('SELECT count(*) FROM variantes', fetchone=True), "pool no conecta"

TEL = "5491100000099"  # telefono falso, sin asignacion ni sesion
servidor.execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (TEL,), commit=True)

prompt = servidor.obtener_prompt_personalizado(TEL, "BASICO")
model = genai.GenerativeModel(
    model_name='gemini-2.5-flash',
    tools=[servidor.consultar_catalogo, servidor.consultar_flujo, servidor.consultar_medidas]
)
hist = [
    {"role": "user", "parts": [prompt]},
    {"role": "model", "parts": ["Entendido. Hablo natural, sin repetir, y uso las tools para specs."]},
]
chat = model.start_chat(history=hist, enable_automatic_function_calling=True)

# Guion adversarial: replica EXACTO el chat que fallo (da dientes temprano, evade, etc.)
guiones = [
    "Hola",
    "Santiago",
    "Quiero una sierra para melamina",
    "con 96 dientes",          # da los dientes TEMPRANO (antes fallaba: re-preguntaba diametro)
    "¿tenés stock?",           # evade la pregunta (antes: repetia la misma pregunta)
    "300mm",
    "cuántos dientes tiene?",  # antes: "no tengo el dato"
    "si dale",
]

for u in guiones:
    try:
        r = chat.send_message(u)
        txt = (r.text or "").strip()
    except Exception as e:
        txt = f"[ERROR: {e}]"
    print("CLIENTE:", u)
    print("BOT:", txt)
    print("-" * 70)

servidor.execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (TEL,), commit=True)
