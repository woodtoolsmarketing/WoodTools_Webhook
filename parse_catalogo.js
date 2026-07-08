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
// diametro: D:/D= en mm. Para mechas, el diametro de corte es Y=
function parseDiametro(s, familia) {
    let m = s.match(/D\s*[:=]\s*([0-9]+)/i);
    if (m) return parseInt(m[1], 10);
    if (familia === 'Mechas') {
        m = s.match(/[YØ]\s*[:=]\s*([0-9]+)/i);
        if (m) return parseInt(m[1], 10);
    }
    return null;
}
function parseEspesor(s) {
    const m = s.match(/B\s*[:=]\s*([0-9]+(?:[.,][0-9]+)?)/i);
    return m ? parseFloat(m[1].replace(',', '.')) : null;
}
function parseEje(s) {
    const m = s.match(/\bd\s*[:=]\s*([0-9]+(?:[.,][0-9]+)?)/);
    return m ? parseFloat(m[1].replace(',', '.')) : null;
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
        if (/cepillador/.test(n)) grupo = 'cepillado';
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
    }
    return { grupo, subtipo, material_corte };
}

const filas = [];
const vistos = new Set();
const statsFam = {};

for (const key of Object.keys(base)) {
    if (SKIP.has(key)) continue;
    const fam = base[key];
    const familia = fam.categoriaImg || 'Otros';
    const titulo = (fam.titulo || '').split('(')[0].trim();
    const marca = fam.marca || 'Consultar';
    const cb = fam.caracteristicasBasicas || {};
    const uso = cb['Uso'] || '';
    const material = cb['Material'] || '';
    for (const v of (fam.variantes || [])) {
        const codigo = (v.id || '').trim();
        if (!codigo || vistos.has(codigo)) continue;
        vistos.add(codigo);
        const spec = (v.nombre || '').trim();
        const blob = spec + ' ' + titulo;
        const sub = subgrupoDe(familia, titulo, uso, codigo);
        const gf = grupoFlujo(familia, titulo, uso, sub);
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
            diametro_mm: parseDiametro(blob, familia),
            espesor_mm: parseEspesor(spec),
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
