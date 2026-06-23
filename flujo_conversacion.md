# Flujo de conversación del bot — WoodTools

Cómo el bot detecta la **familia**, baja al **grupo/elemento** y cierra, emulando
a un asesor humano (sin repetir preguntas).

## Arquitectura (importante)

El flujo **NO vive en el prompt** (eso causaba *lost-in-the-middle*). Vive en
**Supabase SQL** y el bot lo recupera *just-in-time* solo para la familia que
detecta. Tres piezas:

1. **`productos`** — clasificado en columnas filtrables:
   - `grupo` (ej. `melamina`, `moldura`, `pasante`, `dorso_ranurado`…)
   - `subtipo` (`individual` / `combo`, solo fresas moldura)
   - `material_corte` (`hss` / `widia`, solo cuchillas)
   - Un **trigger** (`fn_clasificar_producto`) reclasifica solo en cada
     INSERT/UPDATE → los productos nuevos nunca quedan sin grupo. **No hay que
     tocar nada a mano** cuando se cargan productos.
2. **`flujo_familia`** — una fila por familia con sus reglas duras (`nota_familia`).
3. **`flujo_pregunta`** — qué preguntar, en qué orden, con qué opciones y cuándo.

El prompt estático quedó en **~350 tokens** (rol + reglas duras + anti-repetición).
Las tools que usa el bot (Gemini function-calling):
- `consultar_flujo(familia)` → trae la nota + preguntas de **una** familia.
- `consultar_catalogo(familia, grupo, subtipo, material_corte, lado)` → devuelve
  **máximo 2** productos + señal de cuántos hay en total.

> **Para cambiar el flujo NO se toca código ni el prompt.** Se hace un `UPDATE`
> en `flujo_familia` / `flujo_pregunta` (o se editan los productos y el trigger
> reclasifica). Ver sección "Cómo mantenerlo".

---

## 0. Regla madre: anti-repetición (en el prompt)

1. Una sola pregunta por mensaje.
2. Mirá el historial antes de preguntar; si el dato ya está, no lo repitas.
3. Nunca repitas la misma pregunta con las mismas palabras.
4. **Máximo 2 intentos por dato**: intento 1 normal; intento 2 reformula con 2
   opciones concretas; después asumí la opción más común o derivá. Prohibido
   pedir el mismo dato 3+ veces.
5. No saludes de nuevo en cada mensaje.

---

## 1. Detección de FAMILIA

| El cliente dice…                                          | Familia    |
|-----------------------------------------------------------|------------|
| sierra, disco, hoja, cortar placas/tableros               | Sierras    |
| fresa, router, tupí, moldura, cepillar madera, CNC        | Fresas     |
| mecha, broca, perforar, agujero, bisagra                  | Mechas     |
| cuchilla, cepillo, moldurera, chipera, cabezal            | Cuchillas  |
| envíos, afilado, horarios                                 | atencion   |

Si es ambiguo (solo "Hola"): una pregunta corta y abierta, sin listar familias.

> **No existe "Diamante"** en el catálogo. Si lo piden, el bot deriva al asesor.
> Los **Cabezales** (1 producto) se agruparon dentro de **Cuchillas**.

---

## 2. Grupos reales por familia (lo que hay cargado en la DB)

### SIERRAS (36)
- `madera` (22) — madera maciza / tirantería. Marcas Freud o Franzoi.
- `melamina` (14) — melamina / aglomerado / MDF.
- Si es melamina, el bot pregunta **con/sin incisor** (con incisor =
  escuadradora/industrial; sin incisor = banco/mano). *Es un dato del cliente,
  no una columna: solo 3 de 36 productos lo dicen en el nombre.*

### FRESAS (32) — todas cortan madera, no se pregunta material
- `moldura` (11) → subtipo `individual` / `combo`
- `machimbre` (9)
- `canales` (6) — rectas, ranurar, rinconera, replán
- `finger` (5) — finger, encastre, ensamble
- `cepillado` (1)
- Regla: eje 40 mm (menor = buje, mayor = alesar). Nunca se pregunta profundidad.

### MECHAS (8) — perforan madera; se pregunta máquina/agujero antes que material
- `pasante`, `ciega`, `bisagra`, `integral_cnc`, `barreno`, `accesorio`,
  `router_especial` (1 cada uno aprox.)
- `lado` (derecha/izquierda) solo si la máquina lo pide.

### CUCHILLAS (6, incluye el cabezal)
- Formato (`grupo`): `planas` (cepillar) · `dorso_ranurado` (moldurera) ·
  `chipera` · `cabezales`
- Material (`material_corte`): `hss` / `widia`
- Regla: largo = ancho de la madera. Si da el ancho, no se pregunta el largo.

---

## 3. Cierre (todas las familias)

1. Mostrar **máximo 1-2 opciones**; si hay más, pedir 1 dato para afinar.
2. **Nunca** decir el código interno.
3. Preguntar si necesita algo más → generar el enlace al asesor.
4. Envíos: CABA/GBA lo coordina el vendedor; interior por Vía Cargo / Credifin.
   Afilados: 2 a 5 días. *(En `flujo_familia('atencion')`.)*

---

## 4. Ejemplo (asesor real, no robot)

> Bot: ¿Qué vas a cortar, melamina o madera?
> Cliente: ¿tenés stock?
> Bot: Sí, tengo varias 👍 ¿es para melamina o madera maciza? *(reformula 1 vez)*
> Cliente: para escuadradora
> Bot: Perfecto, escuadradora va con incisor. Tengo la Freud de 300mm ideal para
>      melamina, ¿te paso precio? *(asume melamina por contexto, avanza)*

---

## 5. Cómo mantenerlo (sin tocar código)

- **Agregar/editar un producto:** cargalo normal en `productos`. El trigger le
  pone `grupo`/`subtipo`/`material_corte` solo. Si un nombre nuevo no encaja en
  los patrones, ajustá `fn_clasificar_producto` (una función SQL) o corregí la
  fila con un `UPDATE`.
- **Cambiar qué pregunta el bot:** `UPDATE`/`INSERT` en `flujo_pregunta`
  (columnas `orden`, `slot`, `pregunta`, `opciones`, `condicion`).
- **Cambiar las reglas de una familia:** `UPDATE flujo_familia SET nota_familia=…`.

Reflejo en el código: `servidor.py` → tools `consultar_flujo` y
`consultar_catalogo`; prompt corto en `BASE_CONOCIMIENTO`.
