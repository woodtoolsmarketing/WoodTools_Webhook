// Extrae baseDatosProductos de producto.js y lo aplana a filas de variantes
// con specs parseadas (diametro, dientes, espesor, eje). Determinístico.
const fs = require('fs');

const SRC = "C:/Users/WoodTools-02/Desktop/vscode/pagina-wt/ruta-productos/JS/producto.js";
const OUT = "C:/Users/WoodTools-02/Desktop/vscode/WoodTools_Webhook/catalogo_variantes.json";

const src = fs.readFileSync(SRC, 'utf8');

// --- Aislar el objeto literal (de "{" tras el nombre hasta el "};" previo a la seccion 2) ---
const marker = src.indexOf('2. CONFIGURACIÓN');
const objStart = src.indexOf('{', src.indexOf('const baseDatosProductos'));
const objEndSemi = src.lastIndexOf('};', marker);
const objSrc = src.slice(objStart, objEndSemi + 1);
const base = eval('(' + objSrc + ')');

// --- Familias "agregadoras" que duplican variantes ya presentes en familias finas ---
const SKIP = new Set(['MCD-MCI', 'MB', 'MI', 'MAM-PINZA', '824', '925', '927', '929', '932']);

function intAfter(re, s) {
    const m = s.match(re);
    return m ? parseInt(m[1], 10) : null;
}

// dientes: primer numero tras Z (soporta "Z: 96", "Z:6+6", "Z=2x4")
function parseDientes(s) {
    const m = s.match(/Z\s*[:=]\s*([0-9]+)/i);
    return m ? parseInt(m[1], 10) : null;
}
// diametro de CORTE. Prioridad: D= (mayuscula) > Y=/Ø= > #NNmm.
// OJO: la 'd' MINUSCULA es el eje/agujero, NO el diametro -> el match de D es
// case-SENSITIVE a proposito (con /i los cabezales "Y=120 d=40" quedaban en 40).
// "#NN" solo es diametro cuando NO hay Y=: en "Y=125 #50(6)" el 125 es el
// diametro y el 50 el ancho de trabajo; en "RECTA #12mm" el 12 SI es el diametro.
// Herramientas REGULABLES: el spec trae un rango ("5-10mm", "5 A 10mm",
// "D: 125-200mm"). Antes se guardaba uno de los extremos como si fuera una medida
// fija y el bot afirmaba un diametro que la herramienta no tiene.
function parseDiametroRango(s) {
    let m = s.match(/\bD\s*[:=]\s*([0-9]+)\s*[-–]\s*([0-9]+)/);
    if (!m) m = s.match(/#?\s*([0-9]+)\s*(?:mm)?\s*(?:[-–]|\s+[aA]\s+)\s*([0-9]+)\s*mm/);
    if (!m) return null;
    const a = parseInt(m[1], 10), b = parseInt(m[2], 10);
    return (b > a) ? { min: a, max: b } : null;
}
function parseDiametro(s) {
    // Un D= explicito de valor unico siempre gana, aunque el spec traiga ademas un
    // rango de otra cosa (ej "DECK REGULABLE #10 A 25mm D=150": D=150 es el disco).
    // (?![0-9]) evita que el backtracking parta el numero: sin eso "D: 125-200mm"
    // devolvia 12 (tomaba "12" porque lo seguia un "5" y no un guion).
    let m = s.match(/\bD\s*[:=]\s*([0-9]+)(?![0-9])(?!\s*[-–]\s*[0-9])/);
    if (m) return parseInt(m[1], 10);
    // Si lo unico que hay es un rango, no existe un diametro unico: va en min/max.
    if (parseDiametroRango(s)) return null;
    m = s.match(/\bD\s*[:=]\s*([0-9]+)/);
    if (m) return parseInt(m[1], 10);
    m = s.match(/[YØ](?:ext)?\s*[:=]\s*([0-9]+)/i);
    if (m) return parseInt(m[1], 10);
    m = s.match(/#\s*([0-9]+)\s*mm/i) || s.match(/#\s*([0-9]+)\s*(?:\(|\b)/);
    if (m) return parseInt(m[1], 10);
    // Ultimo recurso: "NNmm" suelto ("RECTA 100mm Z:4", "MOLDURA 10mm").
    // Se descarta si es un LARGO (L=/ALT:) o el extremo de un rango regulable
    // ("5 A 10mm"): en esos casos NO es el diametro y mentiria la medida.
    m = s.match(/(^|[^a-zA-Z0-9=:])([0-9]+)\s*mm\b/i);
    if (m && !/[LA]\s*[=:]\s*$/i.test(s.slice(0, m.index + m[1].length)) &&
        !/\b[aA]\s*$/.test(s.slice(0, m.index + m[1].length))) {
        return parseInt(m[2], 10);
    }
    return null;
}
// espesor: B=. Se descarta si viene en pulgadas o fraccion ("B=3/4 a 1\"" daba 3).
function parseEspesor(s) {
    const m = s.match(/B\s*[:=]\s*([0-9]+(?:[.,][0-9]+)?)\s*(.?)/i);
    if (!m) return null;
    if (m[2] === '/' || m[2] === '"') return null;   // fraccion o pulgadas: no es mm
    return parseFloat(m[1].replace(',', '.'));
}
function parseEje(s) {
    const m = s.match(/\bd\s*[:=]\s*([0-9]+(?:[.,][0-9]+)?)/);
    return m ? parseFloat(m[1].replace(',', '.')) : null;
}
// Cuchillas: el spec viene como LARGOxANCHOxESPESOR ("100x30x3", "650x70x8").
// El LARGO es el dato que pide el cliente (= ancho de madera que cepilla).
function parseLxAxE(s) {
    const m = s.match(/(?:^|[\s-])([0-9]+(?:[.,][0-9]+)?)\s*x\s*([0-9]+(?:[.,][0-9]+)?)\s*x\s*([0-9]+(?:[.,][0-9]+)?)/i);
    if (!m) return null;
    const n = (x) => parseFloat(x.replace(',', '.'));
    return { largo: n(m[1]), ancho: n(m[2]), espesor: n(m[3]) };
}
// Giro de la mecha. Esta en el titulo ("(Derecha)"), en la 3a letra del codigo
// (MPD/MPI, MCD/MCI, MBD/MBI, AVD/AVI) o en el spec ("DER."/"IZQ.").
// \b sobre DER/IZQ es clave: sin el, "madera" matchea DER.
function parseLado(titulo, codigo, spec) {
    const t = (titulo || '') + ' ' + (spec || '');
    if (/\bderech/i.test(t) || /\bDER\b/.test(spec || '')) return 'derecha';
    if (/\bizquierd/i.test(t) || /\bIZQ\b/.test(spec || '')) return 'izquierda';
    const m = (codigo || '').toUpperCase().match(/^(?:MP|MC|MB|AV)([DI])/);
    if (m) return m[1] === 'D' ? 'derecha' : 'izquierda';
    return null;
}
// El titulo se le muestra al cliente: la regla 3 del prompt prohibe los codigos
// internos, asi que sacamos el sufijo tipo "... HSS CHC" / "... MID" cuando ese
// token es el prefijo del codigo de variante.
function limpiarTitulo(titulo, codigoEjemplo) {
    let t = (titulo || '').trim();
    const cod = (codigoEjemplo || '').toUpperCase();
    const m = t.match(/\s+([A-Z]{2,6})$/);
    if (m && cod.startsWith(m[1])) t = t.slice(0, m.index).trim();
    return t;
}

// Subgrupo fino, sobre todo para SIERRAS: separa melamina / madera / aluminio /
// incisor / triturador / multiple para que el bot no confunda (ej. un incisor de
// 125mm NO es la sierra principal de melamina).
function subgrupoDe(familia, titulo, uso, codigo) {
    const t = (titulo + ' ' + uso).toLowerCase();
    const c = (codigo || '').toUpperCase();
    if (familia === 'Sierras') {
        if (/incisor/.test(t)) return 'incisor';
        if (/triturador/.test(t) || c.startsWith('TR') || c.startsWith('LT')) return 'triturador';
        if (/aluminio|pl[aá]stic|no ferroso/.test(t)) return 'aluminio';
        if (/melamina|aglomerado|\bmdf\b|bilaminad|placa/.test(t)) return 'melamina';
        if (/seccionadora/.test(t)) return 'seccionadora';
        if (/m[uú]ltiple/.test(t)) return 'multiple';
        if (/ranurar|ranurado/.test(t)) return 'ranurar';
        if (/madera/.test(t)) return 'madera';
        return 'otros';
    }
    if (familia === 'Diamante') {
        if (/incisor/.test(t)) return 'incisor';
        if (/disco|corte de placas/.test(t)) return 'disco';
        if (/mecha|bisagra|perforac/.test(t)) return 'mecha';
        return 'otros';
    }
    return null;
}

// Clasificacion de FLUJO (grupo/subtipo/material_corte) a nivel variante, misma
// logica que el trigger fn_clasificar_producto pero aplicada al titulo/uso de cada
// variante. Asi consultar_catalogo puede leer el CATALOGO COMPLETO (variantes) en
// vez del subset de 82 filas de 'productos'.
function grupoFlujo(familia, titulo, uso, subgrupo) {
    const n = (titulo || '').toLowerCase();
    const a = (uso || '').toLowerCase();
    let grupo = null, subtipo = null, material_corte = null;
    if (familia === 'Sierras') {
        grupo = subgrupo;   // rico: melamina/madera/aluminio/incisor/triturador/...
    } else if (familia === 'Fresas') {
        // 'limitador/arandela/tope' son accesorios: sin esto el disco limitador
        // salia como una de las 2 opciones al pedir una fresa de moldura.
        if (/limitador|arandela|\btope\b|separador/.test(n)) grupo = 'accesorio';
        else if (/cepillador/.test(n)) grupo = 'cepillado';
        else if (/machimbre|deck|frente ingl|z[oó]calo/.test(n)) grupo = 'machimbre';
        else if (/finger|encastre|ensamble/.test(n)) grupo = 'finger';
        else if (/recta|ranur|rincone|repl/.test(n)) grupo = 'canales';
        else grupo = 'moldura';
        if (grupo === 'moldura')
            subtipo = /multimoldura|y ventanas|c[oó]ncavo y convexo|radios m/.test(n) ? 'combo' : 'individual';
    } else if (familia === 'Mechas') {
        if (/accesorio/.test(a) || /mandril|pinza/.test(n)) grupo = 'accesorio';
        else if (/integral|cnc/.test(n) || /cnc/.test(a)) grupo = 'integral_cnc';
        else if (/bisagra|cazoleta/.test(n)) grupo = 'bisagra';
        else if (/barreno/.test(n)) grupo = 'barreno';
        else if (/ciega|avellan/.test(n)) grupo = 'ciega';
        else if (/pasante/.test(n)) grupo = 'pasante';
        else grupo = 'router_especial';
    } else if (familia === 'Cuchillas') {
        if (/cabezal/.test(n)) grupo = 'cabezales';
        else if (/chipera/.test(n)) grupo = 'chipera';
        else if (/dorso ranurado/.test(n)) grupo = 'dorso_ranurado';
        else grupo = 'planas';
        if (/hss/.test(n)) material_corte = 'hss';
        else if (/\bwidia\b|metal duro|\bmd\b/.test(n)) material_corte = 'widia';
    } else if (familia === 'Diamante') {
        grupo = subgrupo;
    } else if (familia === 'Cabezales') {
        // Antes quedaban los 25 con grupo NULL: cualquier filtro por grupo daba 0.
        if (/cepillador/.test(n)) grupo = 'cepillado';
        else if (/ranurad|ranurar/.test(n)) grupo = 'ranurar';
        else if (/juntar|minizinken|finger/.test(n) || /finger/.test(a)) grupo = 'finger';
        else grupo = 'multiperfil';
    }
    return { grupo, subtipo, material_corte };
}

const filas = [];
const vistos = new Set();
const codigosGlobales = new Set();
const statsFam = {};

for (const key of Object.keys(base)) {
    if (SKIP.has(key)) continue;
    const fam = base[key];
    const familia = fam.categoriaImg || 'Otros';
    // NO cortamos en "(": ahi viene informacion real ("(Derecha)", "(1/2 a 3/4)").
    // Cortarlo dejaba 12 titulos identicos y borraba el giro de las mechas.
    const tituloSrc = (fam.titulo || '').trim();
    const marca = fam.marca || 'Consultar';
    const cb = fam.caracteristicasBasicas || {};
    const uso = cb['Uso'] || '';
    const material = cb['Material'] || '';
    const primerCod = ((fam.variantes || [])[0] || {}).id || '';
    const titulo = limpiarTitulo(tituloSrc, primerCod);
    for (const v of (fam.variantes || [])) {
        const idRaw = (v.id || '').trim();
        if (!idRaw) continue;
        // El dedupe es por familia-de-catalogo + id: antes era global y los ids
        // genericos (ej "1") de familias distintas se pisaban, perdiendo productos.
        const clave = key + '|' + idRaw;
        if (vistos.has(clave)) continue;
        vistos.add(clave);
        // 'codigo' tiene UNIQUE en la DB: si el id es generico y ya se uso, lo
        // prefijamos con la familia de origen. Es interno, nunca se le dice al cliente.
        let codigo = idRaw;
        if (codigosGlobales.has(codigo)) codigo = key + '-' + idRaw;
        codigosGlobales.add(codigo);
        const spec = (v.nombre || '').trim();
        const blob = spec + ' ' + titulo;
        const sub = subgrupoDe(familia, titulo, uso, codigo);
        const gf = grupoFlujo(familia, titulo, uso, sub);
        const dims = (familia === 'Cuchillas') ? parseLxAxE(spec) : null;
        const diam = parseDiametro(blob);
        // El rango solo es un DIAMETRO regulable si no hay diametro fijo. En
        // "DECK REGULABLE #10 A 25mm D=150" el 10-25 es el ancho de ranura, no el
        // diametro: guardarlo como rango hacia que una fresa de 12mm trajera este disco.
        const rango = (diam == null) ? parseDiametroRango(blob) : null;
        filas.push({
            codigo,
            familia,
            titulo,
            marca,
            uso,
            material,
            subgrupo: sub,
            grupo: gf.grupo,
            subtipo: gf.subtipo,
            material_corte: gf.material_corte,
            // Tambien aplica a las sierras trituradoras ("Giro Derecho/Izquierdo"),
            // no solo a las mechas.
            lado: parseLado(titulo, familia === 'Mechas' ? codigo : '', spec),
            diametro_mm: diam,
            diametro_min_mm: rango ? rango.min : null,
            diametro_max_mm: rango ? rango.max : null,
            largo_mm: dims ? dims.largo : null,
            ancho_mm: dims ? dims.ancho : null,
            espesor_mm: dims ? dims.espesor : parseEspesor(spec),
            eje_mm: parseEje(spec),
            dientes_z: parseDientes(spec),
            spec_raw: spec
        });
        statsFam[familia] = (statsFam[familia] || 0) + 1;
    }
}

fs.writeFileSync(OUT, JSON.stringify(filas, null, 0), 'utf8');

// --- Stats ---
console.log('TOTAL variantes:', filas.length);
console.log('Por familia:', JSON.stringify(statsFam));
console.log('Con diametro:', filas.filter(f => f.diametro_mm != null).length);
console.log('Con dientes :', filas.filter(f => f.dientes_z != null).length);
// Subgrupos de sierras (para chequear el reconocimiento)
const subSierras = {};
filas.filter(f => f.familia === 'Sierras').forEach(f => { subSierras[f.subgrupo] = (subSierras[f.subgrupo] || 0) + 1; });
console.log('Subgrupos SIERRAS:', JSON.stringify(subSierras));
// Grupos de flujo por familia + huerfanos (grupo NULL en familias del flujo)
const flujoFams = ['Sierras', 'Fresas', 'Mechas', 'Cuchillas'];
flujoFams.forEach(fam => {
    const g = {};
    filas.filter(f => f.familia === fam).forEach(f => { g[f.grupo || 'NULL'] = (g[f.grupo || 'NULL'] || 0) + 1; });
    console.log(`Grupos ${fam}:`, JSON.stringify(g));
});
// Muestra: sierras melamina 300mm (lo que falló en el chat)
const demo = filas.filter(f => f.familia === 'Sierras' && f.subgrupo === 'melamina' && f.diametro_mm === 300);
console.log('DEMO sierras melamina 300mm:', JSON.stringify(demo.map(d => `${d.codigo} Z=${d.dientes_z}`)));
