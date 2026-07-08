# Prueba la identificación de 2 pasos contra el endpoint en vivo.
from PIL import Image
import requests, io

B = 'https://woodtools-webhook.onrender.com'
SC = r'C:\Users\WOODTO~1\AppData\Local\Temp\claude\C--Users-WoodTools-02-Desktop-vscode-WoodTools-Webhook\4b4a28d3-ff78-4a99-838f-c99c6f967c84\scratchpad'

def probar(nombre, pag, box):
    crop = Image.open(SC + '\\' + pag).crop(box)
    buf = io.BytesIO(); crop.save(buf, 'PNG'); buf.seek(0)
    r = requests.post(f'{B}/identificar_corte', files={'foto': ('c.png', buf, 'image/png')}, timeout=120)
    print(f'=== {nombre} (HTTP {r.status_code}) ===')
    print(r.json().get('resultado', r.text)[:700])
    print()

# machimbre piso (tiene foto de referencia -> confirmación visual de 2 pasos)
probar('Machimbre de Piso [con ref]', 'pag_09.png', (430, 830, 830, 1120))
# una moldura 1/4 círculo (sin ref -> paso 1 por texto)
probar('1/4 círculo [sin ref]', 'pag_06.png', (830, 870, 1180, 1120))
