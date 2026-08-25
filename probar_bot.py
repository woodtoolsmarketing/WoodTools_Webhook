# -*- coding: utf-8 -*-
"""Corre N conversaciones guionadas contra el bot REAL (mismo prompt + tools + DB + Gemini),
en hilos, y guarda la transcripcion + las tools que se llamaron. No manda WhatsApp."""
import sys, os, json, threading, traceback, functools
sys.path.insert(0, r"C:/Users/WoodTools-02/Desktop/vscode/WoodTools_Webhook")
os.chdir(r"C:/Users/WoodTools-02/Desktop/vscode/WoodTools_Webhook")
import google.generativeai as genai
import psycopg2
from psycopg2 import pool
import servidor
from migrador import DATABASE_URL as URL_OK

try: servidor.scheduler.shutdown(wait=False)
except Exception: pass
servidor.db_pool = psycopg2.pool.SimpleConnectionPool(1, 12, URL_OK, sslmode='require')
assert servidor.execute_db_query('SELECT count(*) FROM variantes', fetchone=True)

# --- Instrumentacion: envolvemos las tools para registrar como las llama el modelo ---
TRAZA = threading.local()
def _wrap(fn):
    # functools.wraps deja __wrapped__, e inspect.signature lo sigue: asi el SDK de
    # Gemini ve la firma REAL (con defaults) y no (*a, **k), que rompia el schema.
    @functools.wraps(fn)
    def w(*a, **k):
        out = fn(*a, **k)
        try: TRAZA.log.append({"tool": fn.__name__, "args": {**{f"a{i}": v for i, v in enumerate(a)}, **k},
                               "out": (out or "")[:400]})
        except Exception: pass
        return out
    return w
TOOLS = [_wrap(servidor.consultar_catalogo), _wrap(servidor.consultar_flujo),
         _wrap(servidor.consultar_medidas), _wrap(servidor.buscar_specs_otra_marca)]

ESCENARIOS = json.load(open(sys.argv[1], encoding='utf-8'))
RES = {}
def correr(idx, esc):
    TRAZA.log = []
    tel = "54911000000%02d" % (50 + idx)
    servidor.execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (tel,), commit=True)
    prompt = servidor.obtener_prompt_personalizado(tel, "BASICO")
    model = genai.GenerativeModel(model_name='gemini-2.5-flash', tools=TOOLS)
    chat = model.start_chat(history=[{"role": "user", "parts": [prompt]},
                                     {"role": "model", "parts": ["Entendido."]}],
                            enable_automatic_function_calling=True)
    turnos = []
    for u in esc["mensajes"]:
        n0 = len(TRAZA.log)
        try:
            r = chat.send_message(u)
            txt = servidor._texto_de(r)
            if not txt:   # mismo reintento que hace produccion
                txt = servidor._texto_de(chat.send_message(
                    "(segui la conversacion y responde al cliente en una sola frase)"))
            if not txt:
                txt = "[VACIO tras reintento]"
        except Exception as e:
            txt = "[ERROR: %s]" % e
        turnos.append({"cliente": u, "bot": txt, "tools": list(TRAZA.log[n0:])})
    RES[esc["id"]] = {"titulo": esc["titulo"], "objetivo": esc["objetivo"], "turnos": turnos}
    servidor.execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (tel,), commit=True)
    print("[ok]", esc["id"], flush=True)

hilos = []
for i, e in enumerate(ESCENARIOS):
    t = threading.Thread(target=lambda i=i, e=e: (_ for _ in ()).throw(SystemExit) if False else _safe(i, e))
    hilos.append(t)
def _safe(i, e):
    try: correr(i, e)
    except Exception:
        RES[e["id"]] = {"titulo": e["titulo"], "error": traceback.format_exc()[-1500:]}
        print("[fail]", e["id"], flush=True)
for t in hilos: t.start()
for t in hilos: t.join()
json.dump(RES, open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print("LISTO ->", sys.argv[2])
