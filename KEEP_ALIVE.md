# Keep-alive del webhook (evitar que Render duerma)

**Problema:** en Render free-tier el servicio se **duerme tras ~15 min sin tráfico**. Mientras
duerme (y durante el arranque en frío de 30–60 s) **pierde los webhooks de WhatsApp** que manda
Meta (delivered/read/respuestas), porque Meta reintenta por poco tiempo y después desiste. Eso
subcuenta las entregas/lecturas.

**Solución:** pinguear el servicio desde AFUERA cada pocos minutos para que no llegue a los 15 min
de inactividad. Tiene que ser externo: un self-ping no sirve (el proceso dormido no puede
pingearse a sí mismo).

URL a pinguear (endpoint liviano, sin base de datos):

    https://woodtools-webhook.onrender.com/

Con cualquiera de las opciones de abajo **alcanza UNA sola** (no hace falta combinarlas).

---

## Opción A — GitHub Action (ya activa, no requiere nada)

Ya está en el repo: [.github/workflows/keep-alive.yml](.github/workflows/keep-alive.yml). Corre en
la infra de GitHub cada ~10 min. Verla/dispararla a mano: repo en GitHub → pestaña **Actions** →
**keep-alive** → botón **Run workflow**.

Contra: el cron de GitHub **no es puntual** (puede atrasarse varios minutos o saltear una corrida
con mucha carga), así que cada tanto puede quedar una ventana > 15 min y dormirse igual. Para algo
más confiable, usar la Opción B o C.

---

## Opción B — cron-job.org  (RECOMENDADO: gratis y puntual)

1. Entrar a https://cron-job.org y crear una cuenta gratis.
2. **Create cronjob** y completar:
   - **Title:** `WoodTools keep-alive`
   - **URL:** `https://woodtools-webhook.onrender.com/`
   - **Schedule:** cada **5 minutos** (elegir "Every 5 minutes", o custom `*/5 * * * *`)
   - **Request method:** `GET`
   - **Save responses / notificaciones:** opcional (podés activar aviso por si falla)
3. **Create** → queda corriendo cada 5 min. Listo.

5 minutos está muy por debajo del umbral de 15 → lo mantiene despierto de forma confiable.

---

## Opción C — UptimeRobot  (gratis + te avisa si se cae)

1. Entrar a https://uptimerobot.com y crear una cuenta gratis.
2. **+ Add New Monitor** y completar:
   - **Monitor Type:** `HTTP(s)`
   - **Friendly Name:** `WoodTools webhook`
   - **URL (or IP):** `https://woodtools-webhook.onrender.com/`
   - **Monitoring Interval:** `5 minutes` (mínimo del plan gratis)
3. **Create Monitor**.

Bonus: además de mantenerlo despierto, te manda alerta (mail) si el servicio se cae.

---

## Importante: el costo oculto del keep-alive

Mantenerlo despierto 24/7 consume **~730 de las ~750 horas/mes gratis** de Render (casi todo el
cupo de la cuenta). Si tenés **otros servicios free** en la misma cuenta, te podés quedar sin horas
y Render **suspende el servicio hasta el mes siguiente**.

Dado que el keep-alive ya gasta casi todo el cupo gratis para simular un servicio siempre encendido,
**lo más sólido es pasar al plan Starter de Render (~US$7/mes)**: elimina el sleep Y el tope de
horas, y no depende de ningún pinger externo. Si el tracking de la campaña importa para el negocio,
es la opción recomendada; el keep-alive es el parche gratis.

---

## Cómo saber que funciona

Abrir `https://woodtools-webhook.onrender.com/` y que responda **al instante** (si tarda 30–60 s en
cargar, estaba dormido). Debe mostrar algo como:

    🚀 Webhook WoodTools + IA Gemini 🚀 | commit:XXXXXXX | conocimiento:YYYY-MM-DD
