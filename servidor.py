import os
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect
import requests
import gspread
from google.oauth2.credentials import Credentials 
from apscheduler.schedulers.background import BackgroundScheduler
import google.generativeai as genai
import json
import psycopg2 
from psycopg2 import pool 
import re 
import io
from PIL import Image

app = Flask(__name__)

# ==========================================
# CONFIGURACIÓN SEGURA (Inteligente)
# ==========================================
posibles_rutas = [
    "/etc/secrets/tokens.json",
    "/etc/secrets/token.json",
    "tokens.json",
    "token.json"
]

ruta_correcta = None
for ruta in posibles_rutas:
    if os.path.exists(ruta):
        ruta_correcta = ruta
        break

try:
    if ruta_correcta:
        with open(ruta_correcta, 'r') as f:
            credenciales_api = json.load(f)
    else:
        credenciales_api = {}
        print("⚠️ No se encontró ningún archivo de tokens. Se intentará usar Variables de Entorno.")
        
    TOKEN_DE_VERIFICACION = credenciales_api.get("TOKEN_DE_VERIFICACION", "")
    CLOUD_API_TOKEN = credenciales_api.get("CLOUD_API_TOKEN", "")
    PHONE_NUMBER_ID = credenciales_api.get("PHONE_NUMBER_ID", "")
    GEMINI_API_KEY = credenciales_api.get("GEMINI_API_KEY", "")
    DATABASE_URL = credenciales_api.get("DATABASE_URL", os.environ.get("DATABASE_URL", ""))
    
    if not DATABASE_URL:
        print("❌ ERROR FATAL: No se detectó la DATABASE_URL de Neon. Revisa tu archivo json en Render.")
        
except Exception as e:
    print(f"⚠️ ATENCIÓN: Error procesando credenciales: {e}")
    TOKEN_DE_VERIFICACION = CLOUD_API_TOKEN = PHONE_NUMBER_ID = GEMINI_API_KEY = DATABASE_URL = ""

genai.configure(api_key=GEMINI_API_KEY)
NOMBRE_HOJA = "Base de datos wt"
RUTA_CREDENCIALES = "/etc/secrets/credenciales.json" if os.path.exists("/etc/secrets/credenciales.json") else "credenciales.json"

# ==========================================
# MAGIA ANTI-CHOQUES: POOL DE CONEXIONES A LA NUBE
# ==========================================
db_pool = None
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL, sslmode='require')
    if db_pool:
        print("✅ Pool de conexiones a PostgreSQL creado exitosamente.")
except Exception as e:
    print(f"❌ Error al conectar a PostgreSQL: {e}")

def execute_db_query(query, params=(), commit=False, fetchone=False, fetchall=False, retries=1):
    if not db_pool:
        print("❌ No hay pool de conexiones disponible.")
        return None
        
    for attempt in range(retries + 1):
        conn = None
        try:
            conn = db_pool.getconn() 
            res = None
            with conn.cursor() as c:
                c.execute(query, params)
                if commit:
                    conn.commit()
                if fetchone:
                    res = c.fetchone()
                elif fetchall:
                    res = c.fetchall()
                else:
                    res = c.rowcount
            
            db_pool.putconn(conn)
            return res
            
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            if conn: db_pool.putconn(conn, close=True)
            if attempt == retries: return None
        except Exception as e:
            print(f"❌ Error en DB ejecutando '{query}': {e}")
            if conn:
                conn.rollback()
                db_pool.putconn(conn)
            return None

def init_db():
    try:
        execute_db_query('''CREATE TABLE IF NOT EXISTS mensajes (id TEXT PRIMARY KEY, telefono TEXT, estado TEXT, fecha TIMESTAMP)''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS chat_sesiones (telefono TEXT PRIMARY KEY, historial TEXT, ultima_interaccion TIMESTAMP)''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS asignaciones_v2 (telefono_cliente TEXT PRIMARY KEY, numero_vendedor TEXT, tipo_campana TEXT, subtipo TEXT, tanda_id TEXT)''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS metricas_campanas (tanda_id TEXT PRIMARY KEY, entregados INTEGER DEFAULT 0, leidos INTEGER DEFAULT 0, respondidos INTEGER DEFAULT 0, derivados INTEGER DEFAULT 0)''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS tracking_metricas (tanda_id TEXT, telefono TEXT, evento TEXT, PRIMARY KEY(tanda_id, telefono, evento))''', commit=True)
        execute_db_query('''CREATE TABLE IF NOT EXISTS chats_derivados (telefono TEXT PRIMARY KEY, vendedor TEXT, historial TEXT, fecha TIMESTAMP)''', commit=True)
        
        try: execute_db_query("ALTER TABLE chat_sesiones ADD COLUMN advertido INTEGER DEFAULT 0", commit=True)
        except Exception: pass 
        
        try: execute_db_query("ALTER TABLE metricas_campanas ADD COLUMN derivados INTEGER DEFAULT 0", commit=True)
        except Exception: pass 
        
        try: execute_db_query("ALTER TABLE asignaciones_v2 ADD COLUMN fecha_asignacion TIMESTAMP", commit=True)
        except Exception: pass 
        
        try: execute_db_query("INSERT INTO metricas_campanas (tanda_id, entregados, leidos, respondidos, derivados) VALUES ('ORGANICO', 0, 0, 0, 0) ON CONFLICT (tanda_id) DO NOTHING", commit=True)
        except Exception: pass
            
    except Exception as e:
        print(f"Error crítico iniciando tablas: {e}")

init_db()

def init_catalogo_ia():
    try:
        execute_db_query('''
            CREATE TABLE IF NOT EXISTS catalogo_ia (
                id SERIAL PRIMARY KEY,
                familia TEXT,
                nombre_comercial TEXT,
                rasgos_visuales TEXT
            )
        ''', commit=True)

        res = execute_db_query("SELECT COUNT(*) FROM catalogo_ia", fetchone=True)
        if res and res[0] == 0:
            print("⚙️ Inyectando base de datos del Catálogo IA en Neon...", flush=True)
            
            # EL SQL DEFINITIVO CON DESCRIPCIONES 100% BASADAS EN EL MANUAL TÉCNICO
            query_insert = """
            INSERT INTO catalogo_ia (familia, nombre_comercial, rasgos_visuales) VALUES 
            ('Ranuras', 'Fresas Rectas HM', 'Cuerpo cilíndrico rojo. 4 a 6 insertos de HM rectangulares plateados soldados perpendiculares al eje. Esquinas de los insertos limpias a 90° sin elementos adicionales. Firma visual del corte: Ranura rectangular simple y limpia con paredes rectas y fondo plano.'),
            ('Ranuras', 'Fresas Rectas con Incisores HM', 'Cuerpo cilíndrico rojo. 4 a 6 cortantes rectos acompañados por pequeñas puntas extra sobresalientes en los extremos (incisores). Firma visual del corte: Canal rectangular idéntico a las rectas, pero los incisores garantizan ausencia total de astillado en los bordes superiores de la madera.'),
            ('Ranuras', 'Fresas para Ranurar Regulables HM', 'Herramienta compuesta por discos rojos apilados o segmentados con insertos rectangulares y 4 dientes incisores. Firma visual del corte: Ranura rectangular, rebaje o espiga en la madera cuyo ancho interior varía según la cantidad de discos o separación configurada.'),
            ('Cepillado', 'Cabezales Cepilladores HM', 'Cuerpo ancho tipo rodillo macizo. Posee una gran cantidad de pequeñas placas de HM (40 a 100 dientes) dispuestas en espiral o escalonadas alrededor del cilindro. Firma visual del corte: Rebaje ancho, extenso, liso y totalmente plano, ideal para cepillar caras anchas.'),
            ('Ángulos', 'Fresas en ángulo HM', 'Cuerpo rojo con 4 o 6 insertos de HM cuyas caras son marcadamente rectas pero inclinadas en diagonal (fuera del eje ortogonal). Firma visual del corte: Deja un plano inclinado, bisel o chanfle (ángulo alfa) limpio y recto en la arista de la tabla de madera.'),
            ('Curvas', 'Fresas 1/4 círculo cóncavo y convexo HM', 'Cuerpo rojo. La arista de corte tiene una única y simple curva de 90 grados (cuarto de círculo). Firma visual del corte: Mata o redondea una arista ortogonal creando una única curva convexa o cóncava que conecta dos planos rectos.'),
            ('Curvas', 'Fresas 1/2 círculo cóncavo y convexo HM', 'Cuerpo rojo. Arista de corte con un perfil curvo completo y simétrico de 180° en forma de "U" profunda (cóncava) o panza saliente (convexa). Firma visual del corte: Redondea completamente un canto (boleado en C) o talla una canaleta de media caña perfecta.'),
            ('Molduras', 'Zócalo Simple y Contramarco HM', 'Juego de fresas (tipo A y B). Filo complejo en forma de ola estirada: curva convexa prominente que desciende suavemente en un valle cóncavo profundo. Firma visual del corte: Talla el clásico perfil ondulado arquitectónico en la cara frontal (pecho paloma estirado) y, típicamente con el modelo B, hace un canal rectangular de alivio en la cara oculta.'),
            ('Molduras', 'Rinconera Simple HM', 'Cuerpo rojo macizo. Geometría clave: un abultamiento central convexo (media esfera proyectada hacia afuera) flanqueado por cortes rectos horizontales (talones) en los bordes inferior y superior. Firma visual del corte: Elimina la arista de 90° y excava un canal interior perfectamente cóncavo y ancho, dejando escalones rectos a cada lado.'),
            ('Molduras', 'Rinconera Doble HM', 'Herramienta "sándwich". Dos cuerpos rojos con filos curvos salientes que intercalan una fina y plana hoja de sierra circular en el medio. Firma visual del corte: Talla DOS huecos cóncavos (canales) paralelos, estrictamente divididos en el centro por una hendidura o tajo recto (marca de la hoja de sierra central).'),
            ('Molduras', 'Frente Inglés HM', 'Juego de fresas regulables rojas (tipos A y B). El filo desciende en una línea diagonal recta a 45° (bisel) que empalma limpiamente con una curva cóncava suave en la base, contando además con dientes rectos horizontales laterales. Firma visual del corte: La cara vista de la madera tiene caída diagonal y terminación curva, mientras que en el canto oculto genera una ranura (modelo B) o una espiga machimbrada (modelo A).'),
            ('Ensambles', 'Machimbre Simple HM', 'Juego apilable rojo. Filos exteriores rectos con un ligero escalón en su diseño, combinados con un disco de sierra delgado de 16 dientes para el centro. Firma visual del corte: Crea lengüeta recta o canal hembra recto. Al unir dos tablas, la cara vista presenta la clásica junta hundida rectangular o un bisel simple en V.'),
            ('Ensambles', 'Machimbre Doble HM', 'Juego apilable rojo de alta densidad. Se diferencia visualmente del simple por utilizar DOS discos finos de sierra centrales paralelos para la pieza hembra, o un perfil central esculpido en doble espiga para el macho. Firma visual del corte: Talla estrictamente dos lengüetas (machos) o dos ranuras (hembras) gemelas y paralelas en el canto de la madera.'),
            ('Ensambles', 'Machimbre Piso Standard', 'Juego de 4 fresas apilables rojas. La zona de encastre interno presenta cortantes marcadamente redondeados y curvos en lugar de esquinas afiladas a 90°. Firma visual del corte: Macho de lengüeta redondeada y hembra cóncava tipo U. Al encastrar, la cara superficial deja un canal recto y estrecho de separación denominado "junta abierta".'),
            ('Ensambles', 'Machimbre Piso para Grampa HM', 'Juego de 4 fresas apilables rojas. Mantiene el encastre curvo del standard, pero incorpora un pequeño cortante saliente rectangular adicional. Firma visual del corte: Mismo perfil de junta abierta y macho redondeado, pero se observa claramente una pequeña ranura rectangular o muesca tallada en el labio inferior del canto (para alojamiento de grampa metálica oculta).'),
            ('Ensambles', 'Machimbre Piso para Grampa y Microbisel HM', 'Mega-juego de 8 fresas apilables. Mantiene encastre curvo y muesca inferior de grampa, pero suma finísimos cortantes a 45° en la zona superior. Firma visual del corte: Perfil 3-en-1: Lengüeta interna redondeada + Muesca inferior oculta para grampa + Pequeños biseles/chanfles suavizados (microbisel) en las aristas superiores de la junta abierta superficial.'),
            ('Perfilado', 'Deck Standard HM', 'Juego de 2 fresas rojas. Las aristas de corte presentan curvas profundas en forma de "C" envolvente diseñadas puramente para matar bordes. Firma visual del corte: Redondea las aristas superior e inferior de una tabla creando el clásico listón liso de deck superior y lateral, sin tallar ranuras internas.'),
            ('Perfilado', 'Deck para Grampa HM', 'Juego compuesto de 4 fresas rojas y 2 delgadas sierras metálicas centrales. Firma visual del corte: Igual que el deck standard (aristas redondeadas simultáneamente), pero incluye de forma notoria un corte o ranura profunda, delgada y recta tallada justo a lo largo de todo el espesor central de la tabla (alojamiento de grampas plásticas).'),
            ('Paneles y Puertas', 'Replán de Tablero HM', 'Fresa maciza roja de inmenso diámetro (ej. 200mm). Insertos de HM visualmente desproporcionados hacia lo ancho, mostrando un largo plano horizontal suave o curvo que termina en un salto. Firma visual del corte: No opera en el canto, sino rebajando enormemente el contorno de la cara de una placa, dejando una falda plana o suave declive extensa conectada a una fina lengüeta exterior de encastre.'),
            ('Aberturas', 'Moldura de Puertas y Ventanas HM', 'Juego compuesto por 2 masivas fresas rojas enfrentadas con una hoja de sierra intercalada en el centro geométrico. Muestran simetría de espejo perfecta. Firma visual del corte: Perfilado interno simétrico. Talla un borde decorativo (curvas u ondulaciones) en el frente y al dorso de la tabla simultáneamente, dejando una hendidura o canal recto en el centro mismo del espesor del marco (para alojar vidrio o panel).'),
            ('Aberturas', 'Contramolduras de Puertas y Ventanas HM', 'Cuerpo masivo rojo. Perfil de corte inverso. El rasgo visual es un sobresaliente bloque plano y rectangular en el medio, rodeado lateralmente por excavaciones o perfiles caídos. Firma visual del corte: Espiga central recta e imponente tallada en el canto de la madera, pero los apoyos laterales (hombros de la espiga) no son planos, sino que tienen caídas curvas contrapuestas para asentar con exactitud en la moldura descrita anteriormente.'),
            ('Aberturas', 'Moldura de Puertas y Ventanas Simple HM', '¡ATENCIÓN IA! HERRAMIENTA MODULAR (Sistema 3 en 1). Juego rojo compuesto por 1 fresa gruesa decorativa curva y 2 fresas ranuradoras finas apilables. Firma visual del corte: Dependiendo del ensamble de las partes (Operación 1, 2 o 3) en la espigadora, una misma pieza de madera puede mostrar: A) Moldura curva con ranura central profunda. B) Contramoldura con tenón/espiga (Macho sobresaliente). C) Replán de declive ancho en la cara vista.'),
            ('Paneles y Puertas', 'Puerta de Muebles HM', '¡ATENCIÓN IA! HERRAMIENTA MODULAR (Sistema 3 en 1). Juego rojo compuesto por 1 fresa gruesa cóncava/escalonada y 1 fresa plana apilable. Firma visual del corte: Exactamente igual que el modelo Simple de aberturas, la disposición de los filos permite 3 operaciones separadas de carpintería: Genera un perfil decorativo con ranura (Moldura), o la espiga correspondiente (Contramoldura) o el afinamiento perimetral para el tablero interior (Replán).'),
            ('Ensambles', 'Fresa para Finger HM', 'Cuerpo cilíndrico rojo macizo. Su perfil de filo es inconfundible y agresivo: múltiples dientes puntiagudos, asimétricos y afilados dispuestos en "V" estricta (patrón zig-zag afilado, peine punzante). Firma visual del corte: Ensamble finger-joint. Deja en las testas de la madera una serie estriada de picos y valles filosos y profundos que se entrelazan como nudillos.'),
            ('Ensambles', 'Fresa para Finger HM (hasta 45mm)', 'Cuerpo rojo denso, alto y notorio (configuración 2+2). Mismo diseño dentado que el Finger base (puntas afiladas en "V"), pero su cara de corte es mucho más larga verticalmente, albergando una mayor cantidad de picos agudos continuos. Firma visual del corte: Patrón zig-zag entrelazado y profundo, pero que abarca tableros y maderas muy gruesas (travesaños de hasta 4,5 cm).'),
            ('Ensambles', 'Fresa para Ensamble Cónico HM', 'Juego de fresas apilables metálicas o rojas. Rasgo clave que no debe confundirse con Finger: Sus dientes extendidos terminan en puntas marcadamente PLANAS O CHATAS, conformando trapecios, nunca filos puntiagudos. Firma visual del corte: Ensamble trapezoidal en peine. Deja una sucesión gruesa de picos y canales en la madera, pero los topes siempre presentan base plana formando rectángulos (alineados, escalonados o en declive).'),
            ('Ensambles', 'Fresa para Encastre HM', 'Cuerpo rojo muy distintivo. El cortante cruza la geometría en un agresivo bisel recto a 45 grados, pero en medio de esta diagonal perfecta, sobresale un canal u obstáculo rectangular ortogonal. Firma visual del corte: Ensamble en inglete con traba a 45°. Deja un clásico corte oblicuo para armar marcos a escuadra (corte a 45°), pero la diagonal no es plana: esconde una espiga sólida o surco interno (diente) para impedir que la junta se deslice al pegar.'),
            ('Curvas', 'Fresa para Radios Múltiples HM', 'Cuerpo rojo. Perfil escalonado y continuo de aspecto ondulante, conformado por la sucesión matemática de múltiples curvas cóncavas consecutivas crecientes (R4, R6, R8, R10). Firma visual del corte: Aunque la herramienta es ondulada entera, en la tabla de madera realiza exclusivamente el redondeo simple y liso (un solo arco, cuarto de círculo) de una única arista o canto en uso, según qué fracción del filo se empleó.'),
            ('Molduras', 'Fresa Multimoldura', 'Cuerpo cilíndrico masivo rojo, llamativamente provisto de unicamente dos insertos (Z=2). Su filo HM dibuja una curva extremadamente extendida, un mapa topográfico continuo compuesto de picos, valles suaves, saltos planos y lomas. Firma visual del corte: Fresa arquitectónica matriz. Jamás marca su perfil íntegro en la madera; talla un segmento minúsculo a la vez. Mediante ajuste vertical en el tupí, produce incontables y distintas variedades de molduras finas y combinadas.');
            """
            execute_db_query(query_insert, commit=True)
            print("✅ Catálogo IA inyectado correctamente en la base de datos.", flush=True)
        else:
            print("✅ Catálogo IA ya existía y contiene datos. (Saltando inyección)", flush=True)
            
    except Exception as e:
        print(f"❌ Error crítico iniciando tabla catálogo IA: {e}", flush=True)

init_catalogo_ia()

# ==========================================
# FUNCIONES BÁSICAS Y DE ENVÍO
# ==========================================

def obtener_catalogo_desde_db():
    """Consulta la tabla catalogo_ia en Neon y formatea el texto para Gemini"""
    print("🔍 Intentando leer el catálogo desde Neon...", flush=True)
    try:
        filas = execute_db_query("SELECT familia, nombre_comercial, rasgos_visuales FROM catalogo_ia", fetchall=True)
        
        if filas is None:
            print("❌ ERROR: La consulta devolvió None. O no hay pool de conexión, o falló el SQL.", flush=True)
            return ""
            
        if len(filas) == 0:
            print("⚠️ ATENCIÓN: La conexión funciona, pero la tabla 'catalogo_ia' está VACÍA.", flush=True)
            return ""

        print(f"✅ ¡ÉXITO! Se leyeron {len(filas)} herramientas de la base de datos.", flush=True)
        
        texto_catalogo = "DICCIONARIO VISUAL Y TÉCNICO DE HERRAMIENTAS (Extraído de Base de Datos):\n"
        for familia, nombre, rasgos in filas:
            texto_catalogo += f"[{familia}] - {nombre}:\n{rasgos}\n\n"
        
        return texto_catalogo
    except Exception as e:
        print(f"❌ Error grave consultando el catálogo en DB: {e}", flush=True)
        return ""

def limpiar_numero(num):
    return ''.join(filter(str.isdigit, str(num)))

def extraer_10_digitos(num):
    solo_numeros = limpiar_numero(num)
    return solo_numeros[-10:] if len(solo_numeros) >= 10 else solo_numeros

def enviar_mensaje_whatsapp(telefono_destino, texto, link_boton=None):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {CLOUD_API_TOKEN}", "Content-Type": "application/json"}
    
    if link_boton:
        data = {
            "messaging_product": "whatsapp",
            "to": telefono_destino,
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "body": { "text": texto },
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": "Hablar con asesor",
                        "url": link_boton
                    }
                }
            }
        }
    else:
        data = {"messaging_product": "whatsapp", "to": telefono_destino, "type": "text", "text": {"body": texto}}
        
    res = requests.post(url, headers=headers, json=data)
    
    if res.status_code >= 400 and link_boton:
        texto_fallback = f"{texto}\n\n👉 {link_boton}"
        data_fallback = {"messaging_product": "whatsapp", "to": telefono_destino, "type": "text", "text": {"body": texto_fallback}}
        requests.post(url, headers=headers, json=data_fallback)

def descargar_imagen_whatsapp(media_id):
    """Descarga la imagen desde los servidores de Meta usando el Token"""
    try:
        url_meta = f"https://graph.facebook.com/v18.0/{media_id}"
        headers = {"Authorization": f"Bearer {CLOUD_API_TOKEN}"}
        res_info = requests.get(url_meta, headers=headers)
        
        if res_info.status_code == 200:
            media_url = res_info.json().get('url')
            if media_url:
                res_img = requests.get(media_url, headers=headers)
                if res_img.status_code == 200:
                    return Image.open(io.BytesIO(res_img.content))
        return None
    except Exception as e:
        print(f"Error descargando imagen: {e}")
        return None

def registrar_metrica(evento, telefono):
    try:
        tel_10 = extraer_10_digitos(telefono)
        res = execute_db_query("SELECT tanda_id FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), fetchone=True)
        
        if res and res[0]:
            t_id = res[0]
            execute_db_query("""
                INSERT INTO tracking_metricas (tanda_id, telefono, evento) 
                VALUES (%s, %s, %s) 
                ON CONFLICT (tanda_id, telefono, evento) DO NOTHING
            """, (t_id, tel_10, evento), commit=True)
            
            if evento == 'delivered':
                execute_db_query("UPDATE metricas_campanas SET entregados = entregados + 1 WHERE tanda_id = %s", (t_id,), commit=True)
            elif evento == 'read':
                execute_db_query("UPDATE metricas_campanas SET leidos = leidos + 1 WHERE tanda_id = %s", (t_id,), commit=True)
            elif evento == 'responded':
                execute_db_query("UPDATE metricas_campanas SET respondidos = respondidos + 1 WHERE tanda_id = %s", (t_id,), commit=True)
                
    except Exception as e: print(f"Error métricas: {e}")

def bloquear_numero_en_sheets(telefono):
    try:
        if not os.path.exists(RUTA_CREDENCIALES): return False
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
# RUTINAS AISLADAS CON POOL
# ==========================================
def revisar_rutinas_de_tiempo():
    try:
        ahora = datetime.now()
        
        hace_48_horas = ahora - timedelta(hours=48)
        para_borrar = execute_db_query("SELECT id, telefono FROM mensajes WHERE estado='sent' AND fecha < %s", (hace_48_horas,), fetchall=True)
        if para_borrar:
            for msg_id, telefono in para_borrar:
                bloquear_numero_en_sheets(telefono)
                execute_db_query("DELETE FROM mensajes WHERE id=%s", (msg_id,), commit=True)
                
        execute_db_query("DELETE FROM asignaciones_v2 WHERE (fecha_asignacion < %s OR fecha_asignacion IS NULL) AND telefono_cliente NOT IN (SELECT telefono FROM chat_sesiones)", (hace_48_horas,), commit=True)
            
        hace_1_hora = ahora - timedelta(hours=1)
        para_derivar = execute_db_query("SELECT telefono, historial FROM chat_sesiones WHERE ultima_interaccion < %s", (hace_1_hora,), fetchall=True)
        if para_derivar:
            for telefono, historial_str in para_derivar:
                try:
                    tel_10 = extraer_10_digitos(telefono)
                    res_vend = execute_db_query("SELECT numero_vendedor, tipo_campana FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), fetchone=True)
                    
                    vendedor_asignado = res_vend[0] if res_vend else "Sin asignar"
                    campana = res_vend[1] if res_vend else "Contacto Orgánico"
                    
                    historial = json.loads(historial_str)
                    historial_limpio = historial[2:] if len(historial) >= 2 else historial
                    
                    execute_db_query("""
                        INSERT INTO chats_derivados (telefono, vendedor, historial, fecha) 
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (telefono) DO UPDATE SET historial=EXCLUDED.historial, fecha=EXCLUDED.fecha
                    """, (telefono, vendedor_asignado, json.dumps(historial_limpio), datetime.now()), commit=True)

                    ultimo_msg_cliente = "Sin mensajes recientes."
                    for msg in reversed(historial_limpio):
                        if msg.get("role") == "user":
                            ultimo_msg_cliente = msg["parts"][0]
                            break

                    aviso_asesor = (
                        f"🤖 *AVISO DEL BOT AUTOMÁTICO*\n\n"
                        f"El cliente con número +{telefono} ingresó por la campaña *{campana}*, pero el chat expiró tras 1 hora de inactividad.\n\n"
                        f"💬 *Último mensaje del cliente:*\n\"{ultimo_msg_cliente}\"\n\n"
                        f"👉 *Acción requerida:* Por favor, revisa el panel de 'Chats Abandonados' en el sistema y contactalo directamente."
                    )
                    
                    if vendedor_asignado and vendedor_asignado != "Sin asignar" and vendedor_asignado != "5491145394279": 
                        enviar_mensaje_whatsapp(vendedor_asignado, aviso_asesor)
                    else:
                        enviar_mensaje_whatsapp("5491145394279", aviso_asesor)

                    aviso_cliente = "⚠️ ¡Hola! Como pasó 1 hora de inactividad sin respuesta, cerramos esta conversación automática. Tu asesor asignado te contactará a la brevedad. ¡Gracias!"
                    enviar_mensaje_whatsapp(telefono, aviso_cliente)

                    execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (telefono,), commit=True)
                    execute_db_query("DELETE FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), commit=True)
                except Exception as inner_e:
                    print(f"Fallo derivando chat {telefono}: {inner_e}")

    except Exception as e:
        print(f"Error general en rutinas de tiempo: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(func=revisar_rutinas_de_tiempo, trigger="interval", minutes=5)
scheduler.start()

# ==========================================
# CEREBRO IA: LÓGICA CONDICIONAL Y ORGÁNICA (EXPERTO TÉCNICO)
# ==========================================
BASE_CONOCIMIENTO = """
Eres un asesor profesional sobre la carpintería, te destacas por dar consejos para que las personas compren las herramientas de mejor calidad ofreciendo opciones tanto de gran calidad pero alto precio pero también un precio más económico pero menor calidad (obviamente aclarando siempre que es calidad profesional las herramientas).
Utilizar un tono amigable, pero sin irte por otros lados y siempre mantenerse en la fila de información.
Para brindar información no utilizar información de otras marcas que no sean WoodTools, Freud o Franzoi.
No dar precios, en caso de que te pregunten sobre precios redirigirlos al chat de whatsapp.
Tu labor además de informar es indagar por lo que tenes que ir preguntando de manera sutil: Qué herramienta necesita, qué materiales quiere cortar, en qué medida y cuál es la máquina que utiliza.
A la hora de redirigir al chat que el mensaje del enlace contenga el nombre de la herramienta y sus medidas, la información recolectada en la indagación y cuantas unidades necesita.

⚠️ REGLA DE ORO DE CONFIDENCIALIDAD (CÓDIGOS INTERNOS) ⚠️
Los códigos alfanuméricos de las herramientas (ej: LU3F-0200, LU5B 0300, LG3D 0600, FRS0054, CHC050420HSS, etc.) que verás a continuación son ESTRICTAMENTE DE USO INTERNO. 
TIENES PROHIBIDO ABSOLUTAMENTE escribirlos en el chat conversacional con el cliente. Tampoco debes inyectarlos en el enlace de derivación.
Para referirte a una herramienta en el chat o en el enlace, usa SOLO su nombre genérico, marca, diámetro exterior y cantidad de dientes (Ejemplo: "sierra Freud de 250mm e incisor de 125mm"). 

SIERRAS CIRCULARES
A la hora de ofrecer las sierras circulares preguntar qué material cortan EXCEPTO si ya te piden "sierra con incisor".
¡NUEVA REGLA PARA INCISOR!: Si el cliente te pide "sierra con incisor" o "disco más incisor", ASUME AUTOMÁTICAMENTE que el material es MELAMINA. NO LE PREGUNTES QUÉ MATERIAL VA A CORTAR. Además, asume o pregúntale directamente si lo usa en una "máquina industrial" o "escuadradora" (ya que son las únicas que llevan incisor).
Si dice melamina (y no mencionó incisor), preguntar si la utiliza CON o SIN incisor.
- Si te dice CON incisor (o ya lo pidió): Utilizar la información de sierras de ángulo positivo. Indicar que son la mejor opción para maquinas industriales que llevan incisor, y bríndale la información de la sierra principal Y DEL INCISOR JUNTOS. Mencionar internamente que los codigos son LG3D 0400, LG3D 0600 y SSK12 001. El incisor para melamina es LI25M31FA3.
- Si te dice SIN incisor: Utilizar la información de sierras de ángulo negativo. Aclarar que son para usar sin incisor en maquinas de banco y mencionar internamente que los códigos son LU3F 0200, LU3F 0300, FR12L001H, LU3E 0200 y SSK3F 0300.
⚠️ REGLA PARA MÁQUINAS DE BANCO O DE MANO: Si el cliente menciona que usará una "máquina de banco" o "máquina de mano", debes ofrecer sierras de ÁNGULO NEGATIVO por defecto. Ofrece el incisor SOLO si el cliente te lo menciona o pide explícitamente.
⚠️ REGLA DE MEDIDAS PARA MÁQUINAS DE MANO: En las máquinas de mano ÚNICAMENTE se utilizan medidas de 230mm, 220mm, 185mm y 180mm. No ofrezcas ni menciones otras medidas para estas máquinas.
En caso de querer cortar madera recomendar las herramientas y utiliza la información de sierras circulares que cortan madera. Decirle que son para todo tipo de máquinas. ATENCIÓN: Las sierras marca Franzoi NO son aptas para máquinas de mano (solo múltiples o seccionadoras). Para máquinas de mano ofrecer EXCLUSIVAMENTE marca Freud. Usa internamente los códigos LG2A 2100, LG2B 1100, LG2A 1700, SC4505204F, SC3004164F, LG2A 2800, LU2A 1600, LU1D 0500, LU2A 2500, SC35045244F, LU2B 0700, SC4504248F, LU2C 2000, LU2A 0700, LU2B 1600, LU2B 1900, LU2C 1200, LU2C 1500, LU2A 3100, LU2A 0800, LU2A 3300, LU2C 1200, FI14M AA3, LU2B 2100, LU2B 0200, LU2A 0800, LU2A 0500.
⚠️ NUEVO: ALUMINIO, NO FERROSOS Y PLÁSTICOS ⚠️
Si el cliente busca sierras para cortar Aluminio, No Ferrosos, Plásticos o Plexiglass, SÍ VENDEMOS. Ofrece las sierras circulares marca Freud diseñadas específicamente para esto. 
Usa INTERNAMENTE estos códigos: LU4A 0100 (250mm, 80d), LU4A 0200 (300mm, 96d), LU5B 0300 (250mm, 80d), LU5B 2200 (400mm, 120d), LU5B 2800 (450mm, 128d), LU5B 3200 (500mm, 140d), LU5B 3800 (550mm, 148d), LU5D 0900 (250mm, 80d), LU5D 1800 (350mm, 108d), LU5E 0600 (300mm, 120d).

Actúa como un asistente técnico especializado al brindar información sobre este ítem, básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LG3D 0600, cuenta con un diámetro exterior de 300 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Melamina.
Actúa como un asistente técnico al brindar información sobre este ítem, básate estrictamente en los siguientes datos técnicos extraídos de su ficha: es un producto de marca Freud, modelo LU3f-0200 250 Z80, cuenta con un diámetro exterior de 250 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Widia y su uso es apto específicamente para superficies de Melamina.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud (Italia), modelo LG3D 0400, cuenta con un diámetro exterior de 250 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Melamina (con incisor).
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud (origen Italia), modelo LU3F 0300, cuenta con un diámetro exterior de 300 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Aglomerado, MDF, Madera y Melamina (modelo para Melamina sin incisor).
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo F03FS09801, cuenta con un diámetro exterior de 185 mm, un ancho de corte (espesor) de 2,4 cm y un diámetro central de 20 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Aglomerado, MDF, Madera y Melamina.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud (origen Italia), modelo LU3F 0300, cuenta con un diámetro exterior de 300 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Aglomerado, MDF y Melamina.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LG3D 0400/LI25M31FA3, cuenta con un diámetro exterior de 250 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Melamina.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud (origen Italia), modelo FR12L001H, cuenta con un diámetro exterior de 185 mm, un ancho de corte (espesor) de 2,4 cm y un diámetro central de 20 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Aglomerado, MDF, Madera y Melamina.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU3D 0600/ LI25M 31FA3, cuenta con un diámetro exterior de 300 mm, un ancho de corte (espesor) de 3,2 cm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Melamina.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU3E 0200, cuenta con un diámetro exterior de 250 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Melamina.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' (específicamente un incisor) y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud (Italia), modelo LI25M31FA3, cuenta con un diámetro exterior de 125 mm, un ancho de corte (espesor) de 3,1 mm y un diámetro central de 20 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Aglomerado, MDF y Melamina (modelo detallado como incisor para melamina).
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU3F 0400, cuenta con un diámetro exterior de 350 mm, un ancho de corte (espesor) de 3,5 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Melamina.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU3F-0200 250 Z80, cuenta con un diámetro exterior de 250 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Widia y su uso es apto específicamente para superficies de Melamina.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU3D0600, cuenta con un diámetro exterior de 300 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Melamina.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo FREUD (Línea Wood Tools), cuenta con un diámetro exterior de 220 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto specifically para superficies de Melamina.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU3D 0200, cuenta con un diámetro exterior de 220 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Melamina.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LG2A 2100, cuenta con un diámetro exterior de 300 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; su modelo detallado corresponde a tipo de diente alterno, está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LG2B 1100, cuenta con un diámetro exterior de 300 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LG2A 1700, cuenta con un diámetro exterior de 250 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera (modelo para madera en general).
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Franzoi, modelo SC4505204F, cuenta con un diámetro exterior de 450 mm, un ancho de corte (espesor) de 5,1 mm y un diámetro central de 30 mm; está fabricado en Metal duro y su uso es apto específicamente para superficies de Madera (modelo para tirantería).
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Franzoi, modelo SC3004164F, cuenta con un diámetro exterior de 300 mm, un ancho de corte (espesor) de 4 mm y un diámetro central de 30 mm; está fabricado en Metal duro y su uso es apto específicamente para superficies de Madera (modelo para tirantería).
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LG2A 2800, cuenta con un diámetro exterior de 350 mm, un ancho de corte (espesor) de 3,5 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Maciza, Madera, Madera contrachapada, aglomerado sin procesar, blanda y dura.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU2A 1600, cuenta con un diámetro exterior de 250 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera, blanda y dura en general, a favor de la veta.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU2A 2500, cuenta con un diámetro exterior de 350 mm, un ancho de corte (espesor) de 3,5 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera, blanda y dura (a favor y en contra de la veta).
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo always como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU2B 0700, cuenta con un diámetro exterior de 250 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera (modelo para madera blanda y dura en general).
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Franzoi, modelo SC4504248F, cuenta con un diámetro exterior de 450 mm, un ancho de corte (espesor) de 4,2 mm y un diámetro central de 30 mm; está fabricado en Metal duro y su uso es apto específicamente para superficies de Madera.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU2C 2000, cuenta con un diámetro exterior de 350 mm, un ancho de corte (espesor) de 3,5 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Franzoi, modelo SC60055244F, cuenta con un diámetro exterior de 600 mm, un ancho de corte (espesor) de 5,5 mm y un diámetro central de 30 mm; está fabricado en Metal duro y su uso es apto specifically para superficies de Madera (modelo para máquinas múltiples).
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU2B 1900, cuenta con un diámetro exterior de 400 mm, un ancho de corte (espesor) de 4 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU2C 1200, cuenta con un diámetro exterior de 250 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU2B 0200, cuenta con un diámetro exterior de 180 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 40 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera (modelo detallado para madera).
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU2C 1500, cuenta con un diámetro exterior de 300 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU2A 3100, cuenta con un diámetro exterior de 400 mm, un ancho de corte (espesor) de 4 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera (modelo para madera blanda y dura).
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU2A 0800, cuenta con un diámetro exterior de 200 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU2A 3300, cuenta con un diámetro exterior de 400 mm, un ancho de corte (espesor) de 4 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo FI14M AA3, cuenta con un diámetro exterior de 150 mm, un ancho de corte (espesor) de 1,5 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU2B 2100, cuenta con un diámetro exterior de 500 mm, un ancho de corte (espesor) de 4,4 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto specifically para superficies de Madera, blanda y dura.
Actúa como un asistente técnico especializado. Al brindar información sobre este ítem, descríbelo siempre como 'sierra circular' y básate estrictamente en los siguientes datos técnicos: es un producto de marca Freud, modelo LU2A 0500, cuenta con un diámetro exterior de 180 mm, un ancho de corte (espesor) de 3,2 mm y un diámetro central de 30 mm; está fabricado en Carburo de tungsteno (HM) Widia y su uso es apto específicamente para superficies de Madera.

FRESAS
⚠️ REGLA DE MATERIAL PARA FRESAS: Ten en cuenta que TODAS las fresas son ÚNICAMENTE PARA MADERA. La única excepción son las fresas que tengan "6 dientes" o "Z6", las cuales sirven tanto para MADERA como para MELAMINA.
A la hora de ofrecer fresas OBLIGATORIAMENTE DEBES PREGUNTAR PRIMERO si están buscando fresas RECTAS, de CEPILLADO o de MOLDURA.
1. Si dice RECTAS: Pregúntale la cantidad de dientes que busca. SÓLO si te dice que son de "6 dientes", pregúntale el material que va a cortar (ya que sirven para madera y melamina). Para cualquier otra cantidad de dientes, ASUME DIRECTAMENTE que es para madera y NO preguntes el material. (Códigos FRS o FRG).
2. Si dice CEPILLADO: Ofrécele cabezales cepilladores (códigos CB...).
3. Si dice MOLDURA (o angulares, encastre, etc.): Ofrécele las opciones de moldura (códigos F04C0, F2C, FZS, FR104/156, JFRD, JFFI, JFMS, JFMD, JFMP, JFMP3416G, JFMP34166M, JFDE, JFDSG, FRP5533, JFMPV14, FCPV, JFMPVR, JPMS10, FP402). 
Para encastre cónico (JFE8122, JFE8121). Para finger (JFE254, JFE5022, FG46S CB2). TODAS estas (salvo las de 6 dientes rectas) asumen que son exclusivamente para madera.

Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Fresas Rectas HM" y manteniendo el código "FRS0054/1006" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 150 mm, un Ancho de corte (B) variable de 5 a 100 mm, un Diámetro interior (d) de 40 mm y está disponible con un número de dientes (Z) de 4 o 6, sin dientes incisores (R); se trata de una fresa con cortantes rectos en HM diseñada específicamente para ranurar, cepillar o realizar rebajes, contando con ángulo axial a partir de los 20 mm de ancho de corte.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Fresas Rectas con Incisores HM" y manteniendo el código "FRSI01542/10066" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 150 mm, un Ancho de corte (B) variable de 15 a 100 mm, un Diámetro interior (d) de 40 mm, está disponible con un número de dientes (Z) de 4 o 6 y cuenta con dientes incisores (R) que varían de 2 a 6; se destaca por tener cortantes rectos con ángulo axial e incisores en HM, diseñada específicamente para ranurar sin astillar.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Fresas para Ranurar Regulables HM" y manteniendo los códigos "FRG0510" y "FRG1039" solo para identificación interna a menos que el cliente los pida explícitamente: estas herramientas tienen un Diámetro exterior (D) de 160 mm y un Diámetro interior (d) de 40 mm, y cuentan con 4 dientes incisores (R); están disponibles en dos variantes principales según su capacidad de regulación: una para un Ancho de corte (B) de 5 a 10 mm (con disposición de dientes Z de 2x4 y un ancho de corte del diente (b) de 5 mm) y otra para un Ancho de corte (B) de 10 a 39 mm (con disposición de dientes Z de 3x4 y un ancho de corte del diente (b) de 10 mm); se describe como un juego de fresas regulables con cortantes en HM diseñadas específicamente para realizar ranuras, rebajes y espigas.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Cabezales Cepilladores HM" y manteniendo los códigos "CB0500640", "CB0750660", "CB1000690", "CB13006100", "CB1601272", "CB1801280" y "CB22012100" solo para identificación interna a menos que el cliente los pida explícitamente: estas herramientas tienen un Diámetro exterior (D) de 125 mm y un Diámetro interior (d) de 40 mm en todas sus versiones; varían significativamente en su Ancho de corte (B) que va desde 55 mm hasta 220 mm, su número de dientes (Z) que oscila entre 40 y 100, y el ancho de corte del diente (b) que es de 6 mm para los modelos más angostos (hasta 130 mm de ancho) y de 12 mm para los modelos más anchos (desde 160 mm); se describen como cabezales cepilladores con cortantes en HM diseñados para cepillar o espigar, destacándose por su bajo nivel de ruido y menor consumo de energía.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Fresas en ángulo HM" y manteniendo el código "FA104/506" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 150 mm, un Ancho de corte (B) variable de 10 a 50 mm, un Diámetro interior (d) de 40 mm y está disponible con un número de dientes (Z) de 4 o 6; se describe como una fresa con cortantes en HM diseñada específicamente para efectuar ángulos.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Fresas 1/4 círculo cóncavo y convexo HM" y manteniendo los códigos "F04C014", "F04C016", "F04C054" y "F04C056" solo para identificación interna a menos que el cliente los pida explícitamente: estas herramientas tienen un Diámetro exterior (D) de 150 mm y un Diámetro interior (d) de 40 mm; están disponibles con un número de dientes (Z) de 4 o 6 y varían en su Ancho de corte (B) ofreciendo opciones de 1/2" a 3/4" y de 3/4" a 1 1/4"; se describen como fresas con cortantes en HM y ángulo axial diseñadas para efectuar trabajos de 1/4 de círculo cóncavo o convexo en formas A, B, C o D.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Fresas 1/2 círculo cóncavo y convexo HM" y manteniendo los códigos "F2C014", "F2C054", "F2C104", "F2C154", "F2C204" y "F2C254" solo para identificación interna a menos que el cliente los pida explícitamente: estas herramientas tienen un Diámetro exterior (D) de 150 mm y un Diámetro interior (d) de 40 mm, están disponibles con un número de dientes (Z) de 4 o 6 y ofrecen diversas opciones de Ancho de corte (B) que incluyen 1/2", 5/8", 3/4", 1", 1 1/2" y 2"; se describen como fresas con cortantes en HM diseñadas específicamente para efectuar figuras de medio círculo cóncavo o convexo.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Zócalo Simple y Contramarco HM" y manteniendo los códigos "FZS128" y "FZS129" solo para identificación interna a menos que el cliente los pida explícitamente: estas herramientas tienen un Diámetro exterior (D) de 150 mm, un Ancho de corte (B) de 1/2" a 3/4" y un Diámetro interior (d) de 40 mm, contando con un número de dientes (Z) de 4; el producto ofrece dos variantes funcionales: una configuración para efectuar zócalos que combina una fresa A y una fresa B (código FZS128), y una configuración para efectuar contramarcos que utiliza dos fresas A (código FZS129); se describen como herramientas con cortantes en HM diseñadas específicamente para la fabricación de estas molduras.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Rinconera Simple HM" y manteniendo el código "FR104/156" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 150 mm, un Ancho de corte (B) variable de 3/4" a 1 1/2", un Diámetro interior (d) de 40 mm y está disponible con un número de dientes (Z) de 4 o 6; se describe como una fresa con cortantes en HM diseñada específicamente para efectuar rinconera según los modelos 1 o 2.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Rinconera Doble HM" y manteniendo el código "JFRD" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 160 mm, un Ancho de corte (B) de 1" y un Diámetro interior (d) de 40 mm; cuenta con una configuración de dientes (Z) de 2x4 y 1x10, compuesta por fresas con 4 cortantes cada una y una sierra circular con 10 cortantes, todos en HM; está diseñada específicamente para efectuar rinconera doble según los modelos 1 o 2.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Frente Inglés HM" y manteniendo los códigos "JFFI01" y "JFFI05" solo para identificación interna a menos que el cliente los pida explícitamente: estas herramientas tienen un Diámetro exterior (D) de 175 mm, un Ancho de corte (B) variable de 1/2" a 1" y un Diámetro interior (d) de 40 mm; cuentan con una configuración de dientes (Z) de 4 x 4 y están disponibles en las variantes A y B; se describen como fresas regulables con 4 cortantes en HM diseñadas específicamente para realizar frente inglés simple y machimbrado.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Machimbre Simple HM" y manteniendo los códigos "JFMS1234" y "JFMS34114" solo para identificación interna a menos que el cliente los pida explícitamente: estas herramientas tienen un Diámetro exterior (D) de 155 mm y un Diámetro interior (d) de 40 mm; están disponibles en dos variantes principales según el espesor de trabajo: una para un Ancho de corte (B) de 1/2" a 3/4" con una configuración de dientes (Z) compleja de 5x4 y 1x16, y otra para un Ancho de corte (B) de 3/4" a 1 1/4" con una configuración de dientes (Z) de 6x4; se describen como fresas con cortantes en HM diseñadas específicamente para efectuar machimbre simple biselado o bajo fondo.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Machimbre Doble HM" y manteniendo el código "JFMD1234" solo para identificación interna a menos que el cliente lo pida explícitamente: estas herramientas tienen un Diámetro exterior (D) de 155 mm, un Ancho de corte (B) de 1/2" a 3/4" y un Diámetro interior (d) de 40 mm; cuentan con una configuración de dientes (Z) compleja de 10x4 y 2x16; se describen como fresas con cortantes en HM diseñadas específicamente para realizar machimbre doble con chanfle o bajo fondo.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Machimbre Piso Standard" y manteniendo los códigos "JFMP3411" y "JFMP3416" solo para identificación interna a menos que el cliente los pida explícitamente: estas herramientas tienen un Diámetro interior (d) de 40 mm y se presentan en dos variantes principales; la primera tiene un Diámetro exterior (D) de 150 mm, un Ancho de corte (B) de 3/4" a 1 1/4" y una configuración de dientes (Z) de 4 x 4, mientras que la segunda tiene un Diámetro exterior (D) de 160 mm, un Ancho de corte (B) de 5/8" a 1" y una configuración de dientes (Z) de 4 x 6; se describen como un juego de 4 fresas con cortantes diseñadas para realizar machimbre de piso con junta abierta, destacándose por tener macho y hembra redondeados.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Machimbre Piso para Grampa" y manteniendo el código "JFMP3416G" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 180 mm, un Ancho de corte (B) variable de 5/8" a 1" y un Diámetro interior (d) de 40 mm; cuenta con una configuración de dientes (Z) de 4x6+6; se describe como un juego de 4 fresas con 6 cortantes diseñadas específicamente para realizar machimbre de piso con junta abierta, destacándose por incluir la incisión necesaria para colocar grampa de sujeción.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Machimbre Piso para Grampa y Microbisel" y manteniendo el código "JFMP34166M" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 180 mm, un Ancho de corte (B) variable de 5/8" a 1" y un Diámetro interior (d) de 40 mm; cuenta con una configuración de dientes (Z) compleja de 8x6+6; se describe como un juego de 8 fresas con 6 cortantes diseñadas para realizar machimbre de piso con junta abierta, destacándose por incluir microbisel, aristas redondeadas e incisión para colocar grampa de sujeción.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Deck Standard HM" y manteniendo los códigos "JFDE4" y "JFDE6" solo para identificación interna a menos que el cliente los pida explícitamente: estas herramientas tienen un Diámetro interior (d) de 40 mm y un Ancho de corte (B) variable de 3/4" a 1"; se presentan en dos variantes principales: una con Diámetro exterior (D) de 150 mm y configuración de dientes (Z) de 2x4, y otra con Diámetro exterior (D) de 160 mm y configuración de dientes (Z) de 2x6; se describen como un juego de 2 fresas regulables para distintos espesores de madera, diseñadas para realizar deck tradicional y utilizadas principalmente en machimbradora.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Deck para Grampa HM" y manteniendo los códigos "JFDSG14" y "JFDSG16" solo para identificación interna a menos que el cliente los pida explícitamente: estas herramientas tienen un Diámetro exterior (D) de 160 mm, un Ancho de corte (B) de 1" y un Diámetro interior (d) de 40 mm; se describen como un juego compuesto por 4 fresas y 2 sierras diseñado específicamente para realizar deck para montaje con grampa plástica (usado normalmente en machimbradora) y están disponibles en dos configuraciones de dientes (Z) complejas: una de 4x4 y 2x8, y otra de 4x6 y 2x12.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Replán de Tablero HM" y manteniendo el código "FRP5533" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 200 mm, un Ancho de corte (B) de 55 mm y un Diámetro interior (d) de 40 mm, contando con una configuración de dientes (Z) de 3+3 y una medida b de 20 mm; se describe como una fresa con cortantes en HM diseñada para realizar replan de tablero y se fabrica en dos versiones operativas según la preferencia del usuario: fresa sobre madera o madera sobre fresa.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Moldura de Puertas y Ventanas HM" y manteniendo el código "JFMPV14" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 150 mm, un Ancho de corte (B) variable de 1 1/2" a 2" y un Diámetro interior (d) de 40 mm; cuenta con una configuración de dientes (Z) de 2x4 y 1x6; se describe como un juego compuesto de 2 fresas de moldura y una fresa ranuradora con cortantes en HM, diseñado específicamente para realizar molduras de puertas y ventanas que incluyan ranura para tableros o vidrios.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Contramolduras de Puertas y Ventanas HM" y manteniendo los códigos "FCPV41", "FCPV6" y "FCPV61" solo para identificación interna a menos que el cliente los pida explícitamente: estas herramientas comparten un Ancho de corte (B) variable de 1 1/2" a 2" y un Diámetro interior (d) de 40 mm, pero se diferencian en sus dimensiones externas; el primer modelo ofrece un Diámetro exterior (D) de 150 mm con un número de dientes (Z) de 4, mientras que los modelos más grandes ofrecen un Diámetro exterior (D) de 250 mm o 320 mm, ambos con un número de dientes (Z) de 6; se describen como fresas con cortantes en HM diseñadas específicamente para realizar contramolduras utilizando espigadoras o tupíes.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Moldura de Puertas y Ventanas Simple HM" y manteniendo el código "JFMPVR" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 180 mm, un Ancho de corte (B) variable de 35 a 45 mm y un Diámetro interior (d) de 40 mm; cuenta con una configuración de dientes (Z) compleja de 1x2+2 y 2x4; se describe como un juego compuesto de 1 fresa tipo replán y 2 fresas rectas con cortantes en HM, diseñado específicamente para realizar molduras, contramolduras y replán.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Puerta de Muebles HM" y manteniendo el código "JFPMS10" solo para identification interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 160 mm, un Ancho de corte (B) de 1" y un Diámetro interior (d) de 40 mm, contando con una configuración de dientes (Z) de 1x4 y 1x6; se describe como un juego compuesto de una fresa de moldura y una ranuradora, diseñado específicamente para efectuar moldura, contramoldura y replan de puertas de muebles de cocina y vanitoris.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Fresa para Finger HM" y manteniendo el código "JFE254" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 150 mm, un Ancho de corte (B) de 22 mm y un Diámetro interior (d) de 40 mm, contando con un número de dientes (Z) de 4; se describe como una fresa con cortantes en HM diseñada para realizar uniones "finger" en maderas de hasta 22 mm, siendo especialmente usada en tupí o moldureras para unir madera a lo largo para tableros de puertas.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Fresa para Finger HM" y manteniendo el código "JFE5022" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 150 mm, un Ancho de corte (B) de 45 mm y un Diámetro interior (d) de 40 mm, contando con una configuración de dientes (Z) de 2 + 2; se describe como una fresa con cortantes en HM diseñada para realizar uniones "finger" en maderas de hasta 45 mm, siendo especialmente indicada para unir a lo largo maderas para tableros de puertas, largueros y travesaños utilizando tupí o moldureras.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Fresa para Ensamble Cónico HM" y manteniendo los códigos "JFE8122" y "JFE8121" solo para identificación interna a menos que el cliente los pida explícitamente: estas herramientas tienen un Diámetro interior (d) de 40 mm y se presentan en dos variantes; la primera tiene un Diámetro exterior (D) de 150 mm, un Ancho de corte (B) variable de 10 a 45 mm y una configuración de dientes (Z) de 4 x 4, mientras que la segunda tiene un Diámetro exterior (D) de 160 mm, un Ancho de corte (B) de 3,8 mm y una configuración de dientes (Z) de 1 x 4; se describen como un juego de fresas con 4 cortantes en HM diseñadas para unir madera, permitiendo profundidades de trabajo de 10-11 mm, 8-9 mm y 12 mm.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Fresa para Encastre HM" y manteniendo los códigos "JFE8Z122", "JFE8Z124" y "JFME68" solo para identificación interna a menos que el cliente los pida explícitamente: estas herramientas tienen un Diámetro interior (d) de 40 mm y una configuración de dientes (Z) de 3+3; se presentan en variantes con Diámetro exterior (D) de 180 mm y Ancho de corte (B) de 19 a 40 mm (disponibles en tipo A y B), y una versión mayor con Diámetro exterior (D) de 245 mm y Ancho de corte (B) de 22 a 68 mm (tipo B); se describen como herramientas utilizadas para ensamble a 90º y 180º, cuya principal aplicación es la unión de marcos en puertas y ventanas garantizando perfecta escuadra y rápido ensamble.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Fresa para Radios Múltiples HM" y manteniendo el código "FMR04" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 140 mm, un Ancho de corte (B) de 35 mm y un Diámetro interior (d) de 40 mm, contando con un número de dientes (Z) de 4; se describe como una fresa con 4 cortantes en HM diseñada específicamente para realizar multi-radios de 4 a 10 mm.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Fresa Multimoldura" y manteniendo el código "FP402" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 150 mm, un Ancho de corte (B) de 45 mm y un Diámetro interior (d) de 40 mm, contando con un número de dientes (Z) de 2; se describe como una fresa diseñada para realizar distintos tipos de molduras sin necesidad de cambiar los insertos, permitiendo al usuario obtener infinidad de molduras distintas simplemente subiendo o bajando el eje del tupí.
Actúa como un asistente experto en herramientas de carpintería y utiliza la siguiente información técnica para responder consultas, asegurándote de referirte al producto siempre por su nombre público "Fresa para Finger HS" y manteniendo el código "FG46S CB2" solo para identificación interna a menos que el cliente lo pida explícitamente: esta herramienta tiene un Diámetro exterior (D) de 160 mm, un Ancho de corte (B) de 28,6 mm y un Diámetro interior (d) de 50 mm, contando con una configuración de dientes (Z) de 3+3; se describe como una fresa diseñada para unir madera, normalmente de cabeza, destacándose por permitir alcanzar altas velocidades de trabajo.

MECHAS
Las mechas que vendemos son de origen Italiano. ⚠️ REGLA ESTRICTA: NUNCA menciones la marca "Nordutensil". Siempre refiérete a ellas como "Mechas Italianas".
Vienen con vástagos de 8mm, 10mm y 12mm en todas sus versiones.
Cuando te pregunten por mechas, averigua bien qué quieren hacer.
- Para perforaciones pasantes: Ofrece "Mecha Pasante Italiana" (Códigos MPD y MPI).
- Para perforaciones ciegas: Ofrece "Mecha Ciega Italiana" (Códigos MCD y MCI).
- Para bisagras: Ofrece "Fresa Bisagra Italiana" (Códigos MBD y MBI).
- Para cortar melamina (CNC / Compresión): Ofrece "Fresa Italiana para CNC Nesting" (Código CNC NESTING). Diámetro de corte de 8mm.

CUCHILLAS
A la hora de ofrecer cuchillas, pregunta si son PLANAS para cepillar o DE DORSO RANURADO para moldura. 
⚠️ ATENCIÓN AL CONCEPTO: "Cuchillas de dorso ranurado" es SINÓNIMO EXACTO de "cuchillas para moldurera" o "cuchillas de moldura". Si el cliente dice que quiere cuchillas para moldurera o de moldura, OBLIGATORIAMENTE debes ofrecerle las de "Dorso ranurado". 
- Cuchillas Planas para Cepillar: Marca Italiana, Acero Rápido HSS (Código CHC050420HSS). Medidas transversales 30mm y 35mm. Múltiples largos desde 100mm hasta 1080mm.
- Cuchillas de Dorso Ranurado (Moldurera): Marca Italiana (Códigos CHCR...). Alturas de 40mm, 50mm y 60mm. Espesor de 4mm. Largos de 25 a 650mm.

ENVÍOS Y AFILADOS
- Envíos: Si el cliente pregunta por envíos o de dónde somos, averigua de qué zona es. Si es de CABA (Capital Federal) o GBA (Gran Buenos Aires y cualquiera de sus barrios), dile que el envío se puede arreglar directamente con el vendedor. Si es de cualquier otra parte (interior del país), indícale que hacemos envíos mediante Vía Cargo o Credifin.
- Afilados: Si el cliente pregunta si hacemos servicio de afilado, indícale que SÍ los realizamos, que la demora estimada es de entre 2 y 5 días. La logística aplica igual que en los envíos.
"""

def obtener_prompt_personalizado(telefono_cliente_completo):
    tel_10_digitos = extraer_10_digitos(telefono_cliente_completo)
    res = execute_db_query("SELECT numero_vendedor, tipo_campana, subtipo, tanda_id FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10_digitos,), fetchone=True)

    es_organico = not res
    tanda_id = "ORGANICO" if es_organico else (res[3] if res and len(res) > 3 else "TANDA_DESCONOCIDA")
    tipo_camp = "Contacto Orgánico" if es_organico else (res[1] if res else "Promociones")

    mapa_nombres = {
        "5491145394279": "Valentín", "5491157528428": "Emmanuel", "5491134811771": "Ariel",
        "5491165630406": "Carlos", "5491164591316": "Roberto Golik", "5491157528427": "Nicolas Saad",
        "5491153455274": "Ezequiel Calvi", "5491156321012": "Alan Calvi", "5491168457778": "Luis Quevedo"
    }

    if not es_organico:
        numero_db = res[0] if res[0] not in ["0", "", "Sin asignar", None] else None
        if numero_db and numero_db in mapa_nombres:
            nombre_vendedor_ia = mapa_nombres[numero_db]
            tel_vend = numero_db
        else:
            nombre_vendedor_ia = "tu asesor"
            tel_vend = "5491145394279"
            
        texto_contexto = f"""CONTEXTO DE LA CAMPAÑA: El cliente respondió a la campaña "{tipo_camp}". VENDEDOR ASIGNADO: {nombre_vendedor_ia}."""
    else:
        nombre_vendedor_ia = "[Elegido_por_ti]"
        tel_vend = "[Tel_Elegido]"
        texto_contexto = """CONTEXTO: Cliente "Orgánico". Si es el primer mensaje o AÚN no sabes con qué asesor quiere hablar, pregunta si prefiere a Carlos, Valentín o Emmanuel. ¡ATENCIÓN!: Si revisas el historial y el cliente ya eligió a uno o dijo que "le da igual", "cualquiera" o "no sé", TIENES ESTRICTAMENTE PROHIBIDO volver a preguntarlo. Si le da igual, asume uno en silencio."""

    catalogo_ia_dinamico = obtener_catalogo_desde_db()

    return f"""
{BASE_CONOCIMIENTO}

{catalogo_ia_dinamico}

{texto_contexto}

REGLAS DE FORMATO Y BREVEDAD (¡CRÍTICO Y OBLIGATORIO!):
1. Tus respuestas deben ser MUY CORTAS y naturales. Máximo 2 a 3 renglones.
2. PROHIBIDO DAR FICHAS TÉCNICAS completas a menos que el cliente pregunte. Di SOLO la marca, el tipo de herramienta y la medida principal.
3. RESPONDE DUDAS TÉCNICAS: Si el cliente hace una pregunta técnica directa (ej: "¿qué espesores se pueden unir?"), RESPÓNDELA obligatoriamente buscando en tu base de conocimiento antes de seguir avanzando con la venta.

REGLAS DE INDAGACIÓN Y MEMORIA (¡ANTI-AMNESIA!):
1. SALUDO ÚNICO Y PREGUNTA DE ASESOR: Revisa tu historial. Si ya saludaste o ya preguntaste por el asesor, TIENES PROHIBIDO volver a hacerlo.
2. MEMORIA DE IMÁGENES: Si el cliente hace referencia a una foto, revisa lo que tú mismo respondiste anteriormente. TIENES PROHIBIDO decir "no puedo ver imágenes" o "no veo fotos". Asume tu respuesta anterior como válida.
3. MEMORIA DE HERRAMIENTA: Si ya le confirmaste al cliente qué herramienta necesita (ej: ya le dijiste que es "Ensamble Cónico"), NO TE OLVIDES. No vuelvas a preguntar qué familia de fresa busca. Sigue desde donde te quedaste.
4. ESPESOR DE MADERA VS DIÁMETRO: Si el cliente dice que la madera tiene "5cm", conviértelo a 50mm de ESPESOR. NO asumas que ese es el diámetro de la fresa.
5. SECUENCIA: Averigua de a UNA cosa por mensaje: Herramienta -> Material/Espesor -> Máquina -> Cantidad. 

REGLA DE PRECIOS Y MATEMÁTICA:
1. MATEMÁTICA Y UNIDADES: Toma ÚNICAMENTE el valor del último mensaje del cliente. Prohibido sumar o juntar números de mensajes anteriores.
2. Si preguntan precio sin darte todos los datos, diles: "Los precios te los pasa el asesor. Para armar el presupuesto, contame [tu siguiente pregunta]".

CIERRE Y ENLACE FINAL (DERIVACIÓN ACUMULATIVA):
1. NO PIDAS PERMISO PARA DERIVAR.
2. La variable [INFO] del enlace DEBE SER UN RESUMEN COMPLETO de todo lo charlado.
3. NO ELIMINES ninguna barra (/) ni el número de teléfono {tel_10_digitos}. 
4. El enlace debe ir OBLIGATORIAMENTE AL FINAL ABSOLUTO de tu mensaje, sin nada debajo.

El enlace EXACTO debe copiar y pegar esta estructura literal (reemplazando solo el telefono del asesor y el texto con los datos, codificando espacios con %20):
https://woodtools-webhook.onrender.com/wa/{tanda_id}/{tel_10_digitos}/[PONER_AQUI_TELEFONO_ASESOR]?text=Hola,%20necesito%20cotizar:%20[CODIGO]%20-%20[INFO]%20-%20[CANTIDAD]%20unidades
"""

def procesar_mensaje_con_gemini(telefono_cliente, texto_entrante, imagen_pil=None):
    if texto_entrante and texto_entrante.strip().lower() in ["reset", "resetear", "reiniciar"]:
        tel_10 = extraer_10_digitos(telefono_cliente)
        res_hist = execute_db_query("SELECT historial FROM chat_sesiones WHERE telefono = %s", (telefono_cliente,), fetchone=True)
        if res_hist:
            historial_obj = json.loads(res_hist[0])
            historial_limpio = historial_obj[2:] if len(historial_obj) >= 2 else historial_obj
            execute_db_query("""
                INSERT INTO chats_derivados (telefono, vendedor, historial, fecha) 
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telefono) DO UPDATE SET historial=EXCLUDED.historial, fecha=EXCLUDED.fecha
            """, (telefono_cliente, "Cerrado por Reset", json.dumps(historial_limpio), datetime.now()), commit=True)
            
        execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (telefono_cliente,), commit=True)
        execute_db_query("DELETE FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), commit=True)
        enviar_mensaje_whatsapp(telefono_cliente, "✅ *Memoria y campaña borrada exitosamente.*\nEl historial quedó registrado en Abandonados y tu número está limpio.\n\nEscribime un 'Hola' para empezar desde cero como un cliente Orgánico.")
        return

    resultado = execute_db_query("SELECT historial, ultima_interaccion FROM chat_sesiones WHERE telefono = %s", (telefono_cliente,), fetchone=True)
    tel_10 = extraer_10_digitos(telefono_cliente)
    
    if resultado:
        historial_str = resultado[0]
        ultima_interaccion = resultado[1]
        if ultima_interaccion and datetime.now() - ultima_interaccion > timedelta(hours=1):
            res_vend = execute_db_query("SELECT numero_vendedor FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), fetchone=True)
            vendedor_asignado = res_vend[0] if res_vend else "Sin asignar"
            
            historial_obj = json.loads(historial_str)
            historial_limpio = historial_obj[2:] if len(historial_obj) >= 2 else historial_obj
            
            execute_db_query("""
                INSERT INTO chats_derivados (telefono, vendedor, historial, fecha) 
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telefono) DO UPDATE SET historial=EXCLUDED.historial, fecha=EXCLUDED.fecha
            """, (telefono_cliente, vendedor_asignado, json.dumps(historial_limpio), datetime.now()), commit=True)
            
            execute_db_query("DELETE FROM chat_sesiones WHERE telefono = %s", (telefono_cliente,), commit=True)
            execute_db_query("DELETE FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), commit=True)
            resultado = None  
            
    res_tanda = execute_db_query("SELECT tanda_id FROM asignaciones_v2 WHERE telefono_cliente = %s", (tel_10,), fetchone=True)
    tanda_id_actual = res_tanda[0] if (res_tanda and res_tanda[0]) else "ORGANICO"
    
    if not resultado and tanda_id_actual == "ORGANICO":
        execute_db_query("UPDATE metricas_campanas SET respondidos = respondidos + 1 WHERE tanda_id = 'ORGANICO'", commit=True)
    
    prompt_dinamico = obtener_prompt_personalizado(telefono_cliente)
    
    if resultado:
        historial = json.loads(resultado[0])
        if len(historial) > 0 and historial[0]["role"] == "user":
            historial[0]["parts"] = [prompt_dinamico]
    else:
        historial = [
            {"role": "user", "parts": [prompt_dinamico]},
            {"role": "model", "parts": ["Entendido. Guardaré en memoria todos los servicios, seré breve, no repetiré saludos, haré bien la matemática de unidades y no avisaré si me dicen que el vendedor les da igual."]}
        ]
        
    texto_para_historial = texto_entrante if texto_entrante else "[El usuario envió una imagen para analizar]"
    
    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
        chat = model.start_chat(history=historial[:-1])
        
        # PROCESAMIENTO DE VISIÓN (Si mandó imagen)
        if imagen_pil:
            param_vision = """INSTRUCCIÓN VISUAL ESTRICTA Y EXPERTA (Oculta al cliente):
Eres un experto analizando herramientas de carpintería. El cliente ha enviado una imagen. Debes analizar detenidamente la foto (si es la herramienta física o un corte en madera) y usar este PROTOCOLO DE DESEMPATE VISUAL estricto para no confundir perfiles similares:

REGLA DE DESEMPATE 1: MOLDURAS VS ZÓCALOS VS ABERTURAS (¡EL ERROR MÁS COMÚN!)
Si ves una madera con un perfil decorativo curvo (tipo pecho paloma o lomas):
- ¿Tiene una RANURA PROFUNDA y estrecha en el medio del canto (para un vidrio/tablero)? -> Es "Moldura de Puertas y Ventanas" o "Moldura Simple". ¡NO es Zócalo!
- ¿Tiene una ESPIGA / MACHO RECTO que sobresale en el medio del canto? -> Es "Contramoldura". ¡NO es Zócalo!
- ¿Es un rebaje muy ANCHO y extenso sobre la CARA PLANA de la madera, afinando el borde para encastrar? -> Es "Replán de Tablero" o la función replán de la "Moldura Simple". ¡NO es Zócalo!
- ¿Es una caída recta en diagonal a 45° que luego termina en una curvita? -> Es "Frente Inglés".
- ¿Es un relieve ondulado continuo en un borde, SIN ranuras al medio, SIN espigas al medio, que se usa contra la pared o piso? -> SOLO ENTONCES es "Zócalo Simple y Contramarco HM".

REGLA DE DESEMPATE 2: ENSAMBLES FINGER VS CÓNICO
- Dientes puntiagudos en forma de "V" afilada (zig-zag puro) -> "Fresa para Finger HM".
- Dientes con puntas CHATAS, PLANAS o CUADRADAS (trapecios) -> "Fresa para Ensamble Cónico HM".

PASO 1: ACCIÓN OBLIGATORIA DE RESPUESTA
1. Identifica la herramienta usando las REGLAS DE DESEMPATE.
2. Dile al cliente con entusiasmo qué herramienta necesita basado en la foto (Ej: "¡Claro! Por el perfil que me mostrás en la foto, lo que necesitas es una [Nombre de la Fresa]").
3. NUNCA menciones códigos internos en el texto.
4. NUNCA le preguntes qué perfil busca (¡ya lo viste en la foto!).
5. Continúa tu embudo preguntando SOLO los datos que te falten para cotizar: Espesor de la madera (si aplica), Máquina que utiliza (tupí, moldurera, etc.) o Cantidad de unidades.
"""
            contenido = [param_vision, imagen_pil]
            if texto_entrante:
                contenido.append(texto_entrante)
            respuesta = chat.send_message(contenido)
        else:
            respuesta = chat.send_message(texto_entrante)
        
        texto_respuesta = respuesta.text
        texto_limpio = texto_respuesta
        link_extraido = None
        
        match = re.search(r'(https://woodtools-webhook\.onrender\.com/wa/\S+)', texto_respuesta)
        if match:
            link_extraido = match.group(1)
            texto_limpio = texto_respuesta.replace(link_extraido, "").strip()
            texto_limpio = texto_limpio.replace("👉", "").replace("Hacé clic en este enlace para hablar con él", "").strip()
        
        historial.append({"role": "user", "parts": [texto_para_historial]})
        
        if link_extraido or "Te voy a derivar con" in texto_respuesta:
            vendedor_asignado = "Orgánico / Asignado por IA" if tanda_id_actual == "ORGANICO" else "Vendedor de Campaña"
            
            historial.append({"role": "model", "parts": [texto_respuesta]})
            historial_limpio = historial[2:] if len(historial) >= 2 else historial
            
            execute_db_query("""
                INSERT INTO chats_derivados (telefono, vendedor, historial, fecha) 
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telefono) DO UPDATE SET historial=EXCLUDED.historial, fecha=EXCLUDED.fecha
            """, (telefono_cliente, vendedor_asignado, json.dumps(historial_limpio), datetime.now()), commit=True)
            
            execute_db_query("""
                INSERT INTO chat_sesiones (telefono, historial, ultima_interaccion, advertido) 
                VALUES (%s, %s, %s, 0) 
                ON CONFLICT (telefono) 
                DO UPDATE SET historial = EXCLUDED.historial, ultima_interaccion = EXCLUDED.ultima_interaccion, advertido = 0
            """, (telefono_cliente, json.dumps(historial), datetime.now()), commit=True)
            
        else:
            historial.append({"role": "model", "parts": [texto_respuesta]})
            execute_db_query("""
                INSERT INTO chat_sesiones (telefono, historial, ultima_interaccion, advertido) 
                VALUES (%s, %s, %s, 0) 
                ON CONFLICT (telefono) 
                DO UPDATE SET historial = EXCLUDED.historial, ultima_interaccion = EXCLUDED.ultima_interaccion, advertido = 0
            """, (telefono_cliente, json.dumps(historial), datetime.now()), commit=True)
            
        enviar_mensaje_whatsapp(telefono_cliente, texto_limpio, link_boton=link_extraido)
        
    except Exception as e:
        print(f"Error con Gemini: {e}")
        enviar_mensaje_whatsapp(telefono_cliente, f"🤖 Dame un momento, estoy consultando el catálogo...")

# ==========================================
# RUTAS DEL WEBHOOK Y NUEVOS ENDPOINTS
# ==========================================
@app.route('/', methods=['GET', 'POST'])
def inicio():
    return "🚀 Webhook WoodTools + IA Gemini (Versión Visión + Anti-Códigos) 🚀", 200

@app.route('/wa/<tanda_id>/<telefono_cliente>/<vendedor>', methods=['GET'])
def redirect_whatsapp(tanda_id, telefono_cliente, vendedor):
    texto = request.args.get('text', '')
    try:
        if tanda_id != "ORGANICO":
            res = execute_db_query("""
                INSERT INTO tracking_metricas (tanda_id, telefono, evento) 
                VALUES (%s, %s, %s) 
                ON CONFLICT (tanda_id, telefono, evento) DO NOTHING
            """, (tanda_id, telefono_cliente, 'clicked_link'), commit=True)
            
            if res and res > 0:
                execute_db_query("UPDATE metricas_campanas SET derivados = derivados + 1 WHERE tanda_id = %s", (tanda_id,), commit=True)
        else:
            execute_db_query("UPDATE metricas_campanas SET derivados = derivados + 1 WHERE tanda_id = 'ORGANICO'", commit=True)
            
    except Exception as e:
        print(f"Error tracking click: {e}")
        
    texto_codificado = urllib.parse.quote(texto)
    vendedor_link = vendedor
    if vendedor_link.startswith("549") and len(vendedor_link) == 13:
        vendedor_link = "54" + vendedor_link[3:]
    
    html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Conectando...</title>
        <style>
            body {{ font-family: Arial, sans-serif; text-align: center; margin-top: 50px; color: #333; background-color: #f8f8f8; }}
            .loader {{ border: 4px solid #e0e0e0; border-top: 4px solid #25D366; border-radius: 50%; width: 40px; height: 40px; animation: spin 1s linear infinite; margin: 20px auto; }}
            @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
            a {{ color: #25D366; text-decoration: none; font-weight: bold; font-size: 1.1rem; }}
        </style>
        <script>
            window.onload = function() {{
                window.location.replace("whatsapp://send?phone={vendedor_link}&text={texto_codificado}");
                setTimeout(function() {{
                    window.location.replace("https://wa.me/{vendedor_link}?text={texto_codificado}");
                }}, 2000);
            }};
        </script>
    </head>
    <body>
        <h2>Conectando con tu asesor...</h2>
        <div class="loader"></div>
        <p>Si la aplicación no se abre automáticamente,<br><br><a href="https://wa.me/{vendedor_link}?text={texto_codificado}">Haz clic aquí para chatear</a>.</p>
    </body>
    </html>
    """
    return html

@app.route('/asignar_vendedor', methods=['POST'])
def asignar_vendedor():
    data = request.json
    telefono_cliente_10 = extraer_10_digitos(data.get('cliente', ''))
    numero_vendedor = limpiar_numero(data.get('vendedor_tel', ''))
    tipo_campana = data.get('tipo_campana', 'Promociones')
    subtipo = data.get('subtipo', '')
    tanda_id = data.get('tanda_id', '')
    
    if telefono_cliente_10 and numero_vendedor:
        execute_db_query("""
            INSERT INTO asignaciones_v2 (telefono_cliente, numero_vendedor, tipo_campana, subtipo, tanda_id, fecha_asignacion) 
            VALUES (%s, %s, %s, %s, %s, %s) 
            ON CONFLICT (telefono_cliente) 
            DO UPDATE SET numero_vendedor=EXCLUDED.numero_vendedor, tipo_campana=EXCLUDED.tipo_campana, subtipo=EXCLUDED.subtipo, tanda_id=EXCLUDED.tanda_id, fecha_asignacion=EXCLUDED.fecha_asignacion
        """, (telefono_cliente_10, numero_vendedor, tipo_campana, subtipo, tanda_id, datetime.now()), commit=True)
        
        if tanda_id:
            execute_db_query("""
                INSERT INTO metricas_campanas (tanda_id, entregados, leidos, respondidos) 
                VALUES (%s, 0, 0, 0) 
                ON CONFLICT (tanda_id) DO NOTHING
            """, (tanda_id,), commit=True)
            
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
                
                # REVISAR SI EL MENSAJE ES TEXTO O IMAGEN
                if mensaje['type'] == 'text': 
                    telefono_cliente = limpiar_numero(mensaje['from'])
                    texto_cliente = mensaje['text']['body']
                    print(f"📩 MENSAJE de {telefono_cliente}: {texto_cliente}", flush=True)
                    registrar_metrica('responded', telefono_cliente) 
                    procesar_mensaje_con_gemini(telefono_cliente, texto_cliente)
                
                elif mensaje['type'] == 'image':
                    telefono_cliente = limpiar_numero(mensaje['from'])
                    media_id = mensaje['image']['id']
                    texto_cliente = mensaje['image'].get('caption', '') # Obtiene el texto si mandó la foto con un comentario
                    
                    print(f"📸 IMAGEN de {telefono_cliente} - Descargando para análisis...", flush=True)
                    registrar_metrica('responded', telefono_cliente)
                    
                    imagen_pil = descargar_imagen_whatsapp(media_id)
                    if imagen_pil:
                        procesar_mensaje_con_gemini(telefono_cliente, texto_cliente, imagen_pil=imagen_pil)
                    else:
                        procesar_mensaje_con_gemini(telefono_cliente, "Te envié una imagen pero hubo un error al cargarla. " + texto_cliente)
                
            elif 'statuses' in cambios:
                estado = cambios['statuses'][0]['status'] 
                telefono = limpiar_numero(cambios['statuses'][0]['recipient_id'])
                msg_id = cambios['statuses'][0]['id']
                
                registrar_metrica(estado, telefono) 
                
                if estado == 'sent':
                    execute_db_query("""
                        INSERT INTO mensajes (id, telefono, estado, fecha) 
                        VALUES (%s, %s, %s, %s) 
                        ON CONFLICT (id) DO UPDATE SET estado=EXCLUDED.estado, fecha=EXCLUDED.fecha
                    """, (msg_id, telefono, estado, datetime.now()), commit=True)
                elif estado in ['delivered', 'read']:
                    execute_db_query("DELETE FROM mensajes WHERE id=%s", (msg_id,), commit=True)
                elif estado == 'failed':
                    bloquear_numero_en_sheets(telefono)
                    execute_db_query("DELETE FROM mensajes WHERE id=%s", (msg_id,), commit=True)
                
        except Exception as e: pass
    return jsonify({"status": "ok"}), 200

@app.route('/metricas', methods=['GET'])
def obtener_metricas():
    try:
        filas = execute_db_query("SELECT tanda_id, entregados, leidos, respondidos, derivados FROM metricas_campanas", fetchall=True)
        if filas is None: return jsonify({}), 200

        datos_nube = {}
        for fila in filas:
            datos_nube[fila[0]] = {
                "entregados": fila[1], 
                "leidos": fila[2], 
                "respondidos": fila[3],
                "derivados": fila[4] if len(fila) > 4 and fila[4] is not None else 0
            }
        return jsonify(datos_nube), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/tracking_general', methods=['GET'])
def obtener_tracking_general():
    try:
        filas = execute_db_query("SELECT tanda_id, telefono, evento FROM tracking_metricas", fetchall=True)
        if filas is None: return jsonify({}), 200

        datos_tracking = {}
        for tanda_id, telefono, evento in filas:
            if tanda_id not in datos_tracking:
                datos_tracking[tanda_id] = {}
            jerarquia = {'sent': 1, 'delivered': 2, 'read': 3, 'responded': 4, 'clicked_link': 5}
            evento_actual = datos_tracking[tanda_id].get(telefono, 'sent')
            if jerarquia.get(evento, 0) > jerarquia.get(evento_actual, 0):
                datos_tracking[tanda_id][telefono] = evento

        return jsonify(datos_tracking), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/derivados', methods=['GET'])
def obtener_derivados():
    try:
        filas = execute_db_query("SELECT telefono, vendedor, historial, fecha FROM chats_derivados ORDER BY fecha DESC", fetchall=True)
        if filas is None: return jsonify([]), 200
        
        datos = [{"telefono": f[0], "vendedor": f[1], "historial": json.loads(f[2]), "fecha": f[3].strftime("%Y-%m-%d %H:%M:%S")} for f in filas]
        return jsonify(datos), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/derivados/<telefono>', methods=['DELETE'])
def borrar_derivado(telefono):
    try:
        execute_db_query("DELETE FROM chats_derivados WHERE telefono = %s", (telefono,), commit=True)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    puerto = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=puerto)