# Flujo de conversación del bot — WoodTools

Cómo el bot detecta la **familia**, baja al **grupo/elemento** y cierra, emulando
a un asesor humano (sin repetir preguntas).

## Arquitectura (importante)

El flujo **NO vive en el prompt** (eso causaba *lost-in-the-middle*). Vive en
**Supabase SQL** y el bot lo recupera *just-in-time* solo para la familia que
detecta. Tres piezas:

1. **`variantes` (671 filas)** — el catálogo COMPLETO a nivel ítem, y la única
   tabla que leen las tools. Columnas filtrables:
   - `familia` · `grupo` · `subtipo` (`individual`/`combo`, solo fresas moldura) ·
     `material_corte` (`hss`/`widia`, solo cuchillas planas y dorso ranurado)
   - `subgrupo` (solo Sierras y Diamante, clasificación fina)
   - `lado` (`derecha`/`izquierda`, mechas y sierras trituradoras)
   - specs: `diametro_mm`, `dientes_z`, `largo_mm`, `ancho_mm`, `espesor_mm`, `eje_mm`
   - Se genera desde la web con `node parse_catalogo.js && python cargar_variantes.py`
     (TRUNCATE + recarga completa). **No se edita a mano.**
   > La tabla **`productos` (82 filas) está DEPRECADA** y ninguna tool la usa.
2. **`flujo_familia`** — una fila por familia con sus reglas duras (`nota_familia`).
3. **`flujo_pregunta`** — qué preguntar, en qué orden, con qué opciones y cuándo.

El prompt estático es corto (rol + reglas duras + anti-repetición).
Tools que usa el bot (Gemini function-calling):
- `consultar_flujo(familia)` → nota + preguntas de **una** familia + correcciones aprendidas.
- `consultar_catalogo(familia, grupo, subtipo, material_corte, lado)` → **máximo 2**
  productos + cuántos hay en total.
- `consultar_medidas(familia, diametro_mm, dientes, palabra_clave, subgrupo, largo_mm)`
  → specs exactas. Si la medida pedida no existe, devuelve **las 3 más cercanas que sí**.
- `buscar_specs_otra_marca(marca, producto)` → specs técnicas de otra marca, sin precios.

> **Para cambiar el flujo NO se toca código ni el prompt.** Se hace un `UPDATE`
> en `flujo_familia` / `flujo_pregunta`. Ver "Cómo mantenerlo".

---

## 0. Regla madre: anti-repetición (en el prompt)

1. Una sola pregunta por mensaje.
2. Mirá el historial antes de preguntar; si el dato ya está, no lo repitas.
3. Nunca repitas la misma pregunta con las mismas palabras.
4. **Máximo 2 intentos por dato**: intento 1 normal; intento 2 reformula con 2
   opciones concretas; después asumí la opción más común o derivá. Prohibido
   pedir el mismo dato 3+ veces.
5. No saludes de nuevo en cada mensaje.
6. Si el cliente contesta otra cosa, dalo por respondido igual y avanzá.

---

## 1. Detección de FAMILIA

| El cliente dice…                                            | Familia    |
|-------------------------------------------------------------|------------|
| sierra, disco, hoja, cortar placas/tableros                 | Sierras    |
| fresa, router, tupí, moldura, cepillar madera, CNC          | Fresas     |
| mecha, broca, perforar, agujero, bisagra                    | Mechas     |
| cuchilla, cepillo, moldurera, chipera                       | Cuchillas  |
| cabezal, portacuchillas                                     | Cabezales  |
| **diamante, PCD** (gana sobre las demás)                    | Diamante   |
| envíos, afilado, horarios, dirección, precio, pago, factura | atencion   |

Si es ambiguo (solo "Hola"): una pregunta corta y abierta, sin listar familias.

> **Diamante y Cabezales SÍ existen** y tienen su propia fila en `flujo_familia`
> (33 productos entre las dos). Ojo con la ambigüedad: la familia **`Cabezales`**
> son los portacuchillas Freud; el grupo **`cabezales`** dentro de **`Cuchillas`**
> son los portacuchillas Ilma + sus repuestos. Son cosas distintas.

---

## 2. Grupos reales por familia (lo que hay cargado en `variantes`)

### SIERRAS (86) — marcas: **Freud**; **Franzoi** solo en `multiple`
`multiple` (26) · `madera` (15) · `melamina` (15) · `incisor` (10) ·
`aluminio` (8) · `triturador` (8) · `ranurar` (2) · `seccionadora` (2)

- Melamina: la sierra principal va de **185 a 350mm** →
  185=Z60 · 220=Z64 · 250=Z80 · 300=Z96 · 350=Z108.
- El **incisor** (100/120/125mm) es un **complemento**, nunca la sierra principal.
  No tenemos cargados sus dientes: no inventarlos.
- Preguntar por el incisor es **opcional y no filtra**: primero se ofrece la sierra.
- Las trituradoras vienen con **giro** derecho/izquierdo (`lado`).

### FRESAS (242) — marca única: **WoodTools**. Todas cortan madera, no se pregunta material
`canales` (113) · `moldura` (84, subtipo `individual`/`combo`) · `machimbre` (28) ·
`cepillado` (8) · `finger` (8) · `accesorio` (1)

- Regla: eje 40 mm (menor = buje, mayor = alesar). Nunca se pregunta profundidad.
- Las regulables y las medidas en pulgadas (1/2, 3/4, 1 1/4) **no tienen mm cargados**:
  se dice el rango tal cual, no se convierte.

### MECHAS (166) — marca única: **Nordutensili**. Perforan madera
`ciega` (53) · `pasante` (32) · `bisagra` (27) · `integral_cnc` (26) ·
`accesorio` (22) · `barreno` (5) · `router_especial` (1)

- **`lado`** (derecha/izquierda) aplica a `pasante`, `ciega` y `bisagra`.
  Si el cliente dice "ambas" o no sabe → **no se filtra** y se ofrecen las dos.
- `integral_cnc`, `barreno` y accesorios no tienen giro.

### CUCHILLAS (144) — marca única: **Ilma**
`planas` (69, cepillar) · `dorso_ranurado` (58, moldurera) · `cabezales` (15) · `chipera` (2)

- **El largo es el dato clave** (= ancho de madera que cepilla) → `largo_mm`,
  **no** `diametro_mm`. Planas 100–1080mm · dorso ranurado 25–650mm.
- `material_corte` (`hss`/`widia`) **solo** en `planas` y `dorso_ranurado`.
  En `chipera` y `cabezales` no se pregunta: no vienen en esos materiales.

### CABEZALES (25) — marca: **Freud**. Portacuchillas, 120–126mm
`multiperfil` (11) · `cepillado` (8, helicoidal = bajo ruido) · `ranurar` (3) · `finger` (3)

### DIAMANTE (8) — marcas: **Nordutensili** y **Schiavon**. Filo PCD
`mecha` (4, perforaciones de precisión) · `incisor` (3) · `disco` (1)

---

## 3. Cierre (todas las familias)

1. Mostrar **máximo 1-2 opciones**; si hay más, pedir 1 dato para afinar.
2. **Nunca** decir el código interno.
3. Preguntar si necesita algo más → generar el enlace al asesor.
4. Envíos: CABA/GBA lo coordina el vendedor; interior por Vía Cargo / Credifin.
   Afilados: 2 a 5 días, con Carlos o Valentín. *(En `flujo_familia('atencion')`.)*

### Lo que el bot NO sabe y NO debe inventar
**Precio, stock, formas de pago, factura, garantía y plazos de entrega** no están
en ninguna tabla. Es la **única excepción** a la regla de "nunca digas que no tenés
el dato": el bot debe decir que eso lo confirma el vendedor y pasar el enlace.
*(Si algún día se quiere que responda esto, hay que cargarlo en `flujo_familia('atencion')`.)*

---

## 4. Ejemplo (asesor real, no robot)

> Bot: ¿Qué vas a cortar, melamina o madera?
> Cliente: ¿tenés stock?
> Bot: El stock te lo confirma Valentín 👍 ¿es para melamina o madera maciza?
> Cliente: melamina, de 260
> Bot: De 260 no tengo, pero sí de 250 y de 300. La de 300 lleva 96 dientes.
>      ¿Con cuál vas? *(ofrece la más cercana real, no dice "no me figura")*

---

## 5. Cómo mantenerlo

- **Agregar/editar un producto:** se carga en la web (`producto.js`) y se resincroniza
  con `node parse_catalogo.js && python cargar_variantes.py`. La clasificación
  (`grupo`, `subgrupo`, `lado`, specs) la hace el parser. Si un producto nuevo queda
  mal clasificado, se ajusta la regla en `parse_catalogo.js` (`grupoFlujo` /
  `subgrupoDe` / `parseDiametro`), **no** con un UPDATE a mano: el próximo reload lo pisa.
- **Cambiar qué pregunta el bot:** `UPDATE`/`INSERT` en `flujo_pregunta`
  (`orden`, `slot`, `pregunta`, `opciones`, `condicion`).
  ⚠️ El `slot` tiene que ser un parámetro real de alguna tool, y cada valor de
  `opciones` tiene que existir en `variantes`, o el bot pregunta algo que después
  no puede buscar.
- **Cambiar las reglas de una familia:** `UPDATE flujo_familia SET nota_familia=…`.
  ⚠️ Si nombra marcas, tienen que coincidir con `SELECT DISTINCT marca FROM variantes`.
- **Corregir una conducta:** `POST /aprender` (queda en `aprendizajes`). Tope de 15
  lecciones activas por ámbito: pasado eso se pierden las más viejas.

Reflejo en el código: `servidor.py` → tools `consultar_flujo`, `consultar_catalogo`,
`consultar_medidas`; prompt corto en `BASE_CONOCIMIENTO`.

⚠️ **Render NO auto-despliega** este servicio: los cambios de `servidor.py` requieren
Manual Deploy. Los cambios en SQL (flujo, aprendizajes, catálogo) toman efecto al toque.
