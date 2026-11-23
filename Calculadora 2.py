"""Analizador de circuitos resistivos (serie / paralelo / mixto) como página web.

Instalar:  pip install flask
Ejecutar:  python analizador_circuitos.py   ->  abrir http://127.0.0.1:5000

Sintaxis de la configuración:   +  serie     ||  paralelo     ( )  agrupan
Ejemplo:  R1 + (R2 || (R3 + R4)) + R5
(|| tiene prioridad sobre +, igual que en la notación habitual de circuitos)
"""
import json
import re
import unicodedata
from html import escape
from flask import Flask, jsonify, request

app = Flask(__name__)


class ErrorCircuito(Exception):
    pass


# ───────────────────────── Código de colores ─────────────────────────
DIGITO = {"negro": 0, "marron": 1, "rojo": 2, "naranja": 3, "amarillo": 4,
          "verde": 5, "azul": 6, "violeta": 7, "gris": 8, "blanco": 9}
MULT = {**{c: 10 ** d for c, d in DIGITO.items()}, "dorado": 0.1, "plateado": 0.01}
TOL = {"marron": 1, "rojo": 2, "verde": 0.5, "azul": 0.25, "violeta": 0.1,
       "gris": 0.05, "dorado": 5, "plateado": 10}
TEMPCO = {"negro": 250, "marron": 100, "rojo": 50, "naranja": 15,
          "amarillo": 25, "azul": 10, "violeta": 5}
HEX = {"negro": "#111111", "marron": "#7b4a21", "rojo": "#d62828", "naranja": "#f77f00",
       "amarillo": "#fcd116", "verde": "#2e9e44", "azul": "#1d4ed8", "violeta": "#7e3fbf",
       "gris": "#8d99a6", "blanco": "#ffffff", "dorado": "#cfa32b", "plateado": "#c0c7cf"}
ALIAS = {"cafe": "marron", "morado": "violeta", "purpura": "violeta", "oro": "dorado", "plata": "plateado"}
ETIQUETA = {"marron": "marrón"}


def normaliza(t):
    t = unicodedata.normalize("NFD", t.strip().lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def fmt(x, u=""):
    """Formato de ingeniería: 4700 -> '4.7 kΩ'."""
    if x == 0:
        return f"0 {u}".strip()
    for f, p in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs(x) >= f * 0.9999999:
            return f"{x / f:.4g} {p}{u}".strip()
    return f"{x:.3e} {u}".strip()


def pn(x):
    return f"{x:.6g}"


def decodificar_colores(texto):
    cols = [ALIAS.get(c, c) for c in map(normaliza, re.split(r"[,\s;]+", texto.strip())) if c]
    for c in cols:
        if c not in HEX:
            raise ErrorCircuito(f"color desconocido «{c}»")
    n = len(cols)
    if n not in (3, 4, 5, 6):
        raise ErrorCircuito(f"usa 3, 4, 5 o 6 bandas (recibió {n})")
    nd = 2 if n <= 4 else 3
    for c in cols[:nd]:
        if c not in DIGITO:
            raise ErrorCircuito(f"«{c}» no puede ser una banda de dígito")
    tol = 20.0
    if n >= 4:
        tol = TOL.get(cols[nd + 1])
        if tol is None:
            raise ErrorCircuito(f"«{cols[nd + 1]}» no es un color de tolerancia válido")
    tc = ""
    if n == 6:
        if cols[5] not in TEMPCO:
            raise ErrorCircuito(f"«{cols[5]}» no es un color de coef. de temperatura válido")
        tc = f", {TEMPCO[cols[5]]} ppm/°C"
    cifras = int("".join(str(DIGITO[c]) for c in cols[:nd]))
    mult = MULT[cols[nd]]
    valor = round(cifras * mult, 9)
    desc = (f"{', '.join(cols)} → cifras {cifras} × multiplicador {mult:g} "
            f"= {fmt(valor, 'Ω')} ±{tol:g}%{tc}")
    return valor, tol, cols, desc


def parse_valor(texto):
    t = re.sub(r"(?i)\s*(ohmios?|ohms?|Ω)\s*$", "", texto.strip())
    pref = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
            "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
    m = re.fullmatch(r"(\d+)([kKMG])(\d+)", t)  # notación 4k7
    if m:
        return float(f"{m[1]}.{m[3]}") * pref[m[2]]
    m = re.fullmatch(r"([0-9]*\.?[0-9]+(?:[eE][+-]?\d+)?)\s*([pnuµmkKMG]?)", t.replace(",", "."))
    if not m:
        raise ErrorCircuito(f"valor no válido «{texto}» (ejemplos: 220, 4.7k, 1M, 4k7)")
    return float(m[1]) * pref.get(m[2], 1)


class Res:
    def __init__(self, nombre, valor, tol, cols, desc):
        self.nombre, self.valor, self.tol, self.cols, self.desc = nombre, valor, tol, cols, desc


# ───────────────────────── Árbol del circuito ─────────────────────────
class Nodo:
    def __init__(self, tipo, nombre=None, hijos=None):
        self.tipo = tipo          # 'R' resistencia, 'S' serie, 'P' paralelo
        self.nombre = nombre
        self.hijos = hijos or []
        self.R = self.V = self.I = self.P = None
        self.rama = None          # número de rama (si es hijo directo de un paralelo)
        self.camino = []          # ramas que contienen al nodo


def unir(tipo, hijos):
    if len(hijos) == 1:
        return hijos[0]
    plano = []
    for h in hijos:
        plano.extend(h.hijos if h.tipo == tipo else [h])
    return Nodo(tipo, hijos=plano)


class Parser:
    def __init__(self, texto):
        texto = texto.replace("//", "||").replace("∥", "||")
        self.t = re.findall(r"\|\||\||\+|\(|\)|[A-Za-z_]\w*|\S", texto)
        self.i = 0

    def ver(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def toma(self):
        x = self.ver()
        self.i += 1
        return x

    def expr(self):
        h = [self.par()]
        while self.ver() == "+":
            self.toma()
            h.append(self.par())
        return unir("S", h)

    def par(self):
        h = [self.atomo()]
        while self.ver() in ("||", "|"):
            self.toma()
            h.append(self.atomo())
        return unir("P", h)

    def atomo(self):
        t = self.toma()
        if t is None:
            raise ErrorCircuito("la expresión está incompleta")
        if t == "(":
            n = self.expr()
            if self.toma() != ")":
                raise ErrorCircuito("falta cerrar un paréntesis")
            return n
        if re.fullmatch(r"[A-Za-z_]\w*", t):
            return Nodo("R", t)
        raise ErrorCircuito(f"símbolo inesperado «{t}»")

    def parse(self):
        n = self.expr()
        if self.ver() is not None:
            raise ErrorCircuito(f"símbolo inesperado «{self.ver()}»")
        return n


def nodos(n):
    yield n
    for h in n.hijos:
        yield from nodos(h)


def expr_str(n, raiz=True):
    if n.tipo == "R":
        return n.nombre
    s = (" + " if n.tipo == "S" else " ∥ ").join(expr_str(h, False) for h in n.hijos)
    return s if raiz else f"({s})"


def nombrar(n, cont):
    if n.tipo != "R":
        cont[n.tipo] += 1
        n.nombre = ("Serie " if n.tipo == "S" else "Paralelo ") + str(cont[n.tipo])
        for h in n.hijos:
            nombrar(h, cont)


def asignar_ramas(n, camino, cont):
    n.camino = camino
    if n.tipo == "P":
        for h in n.hijos:
            cont[0] += 1
            h.rama = cont[0]
    for h in n.hijos:
        asignar_ramas(h, camino + [h.rama] if n.tipo == "P" else camino, cont)


# ───────────────────────── Cálculos ─────────────────────────
def calcular_R(n, resist, L):
    if n.tipo == "R":
        n.R = resist[n.nombre].valor
        return
    for h in n.hijos:
        calcular_R(h, resist, L)
    nom = [h.nombre for h in n.hijos]
    L.append(f"{n.nombre} = {expr_str(n)}   ({'serie' if n.tipo == 'S' else 'paralelo'})")
    if n.tipo == "S":
        n.R = sum(h.R for h in n.hijos)
        L.append("   Req = " + " + ".join(nom) + " = " + " + ".join(pn(h.R) for h in n.hijos)
                 + f" = {pn(n.R)} Ω")
    else:
        inv = sum(1 / h.R for h in n.hijos)
        n.R = 1 / inv
        L.append("   1/Req = " + " + ".join(f"1/{x}" for x in nom) + " = "
                 + " + ".join(f"1/{pn(h.R)}" for h in n.hijos) + f" = {pn(inv)} S")
        L.append(f"   Req = 1/{pn(inv)} = {pn(n.R)} Ω")
    L.append("")


def distribuir(n, V, I, L):
    n.V, n.I, n.P = V, I, V * I
    if n.tipo == "R":
        return
    if n.tipo == "S":
        L.append(f"{n.nombre} (serie): misma corriente I = {pn(I)} A en todos sus elementos")
        vs = [I * h.R for h in n.hijos]
        for h, v in zip(n.hijos, vs):
            L.append(f"   V({h.nombre}) = I·R = {pn(I)} · {pn(h.R)} = {pn(v)} V")
        L.append(f"   Comprobación: ΣV = {pn(sum(vs))} V  (debe ser {pn(V)} V)")
        datos = [(h, v, I) for h, v in zip(n.hijos, vs)]
    else:
        L.append(f"{n.nombre} (paralelo): mismo voltaje V = {pn(V)} V en todas sus ramas")
        ii = [V / h.R for h in n.hijos]
        for h, i in zip(n.hijos, ii):
            L.append(f"   I(Rama {h.rama}: {h.nombre}) = V/R = {pn(V)} / {pn(h.R)} = {pn(i)} A")
        L.append(f"   Comprobación: ΣI = {pn(sum(ii))} A  (debe ser {pn(I)} A)")
        datos = [(h, V, i) for h, i in zip(n.hijos, ii)]
    L.append("")
    for h, v, i in datos:
        distribuir(h, v, i, L)


# ───────────────────────── Dibujo SVG ─────────────────────────
RW, RH, YM, PAD, GAP = 128, 64, 32, 28, 14
INK = "#1f2937"


def medir(n):
    for h in n.hijos:
        medir(h)
    if n.tipo == "R":
        n.w, n.h, n.ym = RW, RH, YM
    elif n.tipo == "S":
        arriba = max(h.ym for h in n.hijos)
        abajo = max(h.h - h.ym for h in n.hijos)
        n.w, n.h, n.ym = sum(h.w for h in n.hijos), arriba + abajo, arriba
    else:
        n.w = max(h.w for h in n.hijos) + 2 * PAD
        n.h = sum(h.h for h in n.hijos) + GAP * (len(n.hijos) - 1)
        y, n.ys = 0, []
        for h in n.hijos:
            n.ys.append(y + h.ym)
            y += h.h + GAP
        n.ym = (n.ys[0] + n.ys[-1]) / 2


def ln(x1, y1, x2, y2):
    return f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>'


def txt(x, y, s, size=13, bold=True, color=INK, anchor="middle"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" font-size="{size}" '
            f'font-weight="{600 if bold else 400}" fill="{color}" stroke="none">{escape(str(s))}</text>')


def dibujar(n, x, y, resist):
    wy = y + n.ym
    out = []
    if n.tipo == "R":
        r = resist[n.nombre]
        x0 = x + 36
        out += [ln(x, wy, x0, wy), ln(x0 + 56, wy, x + RW, wy),
                f'<rect x="{x0}" y="{wy - 9}" width="56" height="18" rx="3" fill="#f3e9d2"/>']
        if r.cols:
            nc = len(r.cols)
            extra = 2 if nc == 6 else (1 if nc >= 4 else 0)
            pos = [6 + 8 * i for i in range(nc - extra)] + [46 - 8 * (extra - 1 - j) for j in range(extra)]
            for c, p in zip(r.cols, pos):
                out.append(f'<rect x="{x0 + p}" y="{wy - 9}" width="4" height="18" fill="{HEX[c]}" stroke="none"/>')
        etq = fmt(r.valor, "Ω") + (f" ±{r.tol:g}%" if r.tol else "")
        out += [txt(x + RW / 2, wy - 14, n.nombre), txt(x + RW / 2, wy + 32, etq, 12, False)]
    elif n.tipo == "S":
        cx = x
        for h in n.hijos:
            out += dibujar(h, cx, y + n.ym - h.ym, resist)
            cx += h.w
    else:
        xl, xr = x + PAD, x + n.w - PAD
        ys = [y + v for v in n.ys]
        k = len(n.hijos)
        for h, yh in zip(n.hijos, ys):
            out += dibujar(h, xl, yh - h.ym, resist)
            if xl + h.w < xr:
                out.append(ln(xl + h.w, yh, xr, yh))
        out += [ln(x, wy, xl, wy), ln(xr, wy, x + n.w, wy),
                ln(xl, ys[0], xl, ys[-1]), ln(xr, ys[0], xr, ys[-1])]
        puntos = ys[1:-1] + ([wy] if k >= 3 else [])
        for yy in puntos:
            out += [f'<circle cx="{xl}" cy="{yy:.1f}" r="3" fill="{INK}"/>',
                    f'<circle cx="{xr}" cy="{yy:.1f}" r="3" fill="{INK}"/>']
        for h, yh in zip(n.hijos, ys):  # insignia con el número de rama
            out += [f'<circle cx="{xl + 12}" cy="{yh:.1f}" r="9" fill="#fff" stroke="#2563eb" stroke-width="1.5"/>',
                    txt(xl + 12, yh + 4, h.rama, 11, True, "#2563eb")]
    return out


def svg_circuito(raiz, resist, vs):
    medir(raiz)
    x0, y0, xs = 170, 50, 70
    wy = y0 + raiz.ym
    xr = x0 + raiz.w + 40
    yb = max(y0 + raiz.h + 40, wy + 90)
    cy = (wy + yb) / 2
    p = dibujar(raiz, x0, y0, resist)
    p += [ln(xs, wy, x0, wy), ln(x0 + raiz.w, wy, xr, wy), ln(xr, wy, xr, yb), ln(xr, yb, xs, yb),
          ln(xs, wy, xs, cy - 8), ln(xs, cy + 4, xs, yb),
          f'<line x1="{xs - 16}" y1="{cy - 8}" x2="{xs + 16}" y2="{cy - 8}" stroke-width="3"/>',
          f'<line x1="{xs - 9}" y1="{cy + 4}" x2="{xs + 9}" y2="{cy + 4}" stroke-width="3"/>',
          txt(xs + 24, cy - 10, "+", 14), txt(xs - 22, cy + 2, fmt(vs, "V"), 14, True, INK, "end"),
          txt((xs + x0) / 2, wy - 8, f"I = {fmt(raiz.I, 'A')} →", 12, False)]
    W, H = xr + 40, yb + 30
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" width="{W:.0f}" '
            f'height="{H:.0f}" font-family="system-ui,Segoe UI,sans-serif" fill="none" stroke="{INK}" '
            f'stroke-width="2" stroke-linecap="round">{"".join(p)}</svg>')


# ───────────────────────── Análisis completo ─────────────────────────
def analizar(d):
    try:
        vs = float(str(d.get("vs", "")).replace(",", "."))
    except ValueError:
        raise ErrorCircuito("El voltaje de la fuente debe ser un número.")
    if vs <= 0:
        raise ErrorCircuito("El voltaje de la fuente debe ser mayor que 0.")

    resist, L1 = {}, []
    for f in d.get("resistencias", []):
        nombre = str(f.get("nombre", "")).strip()
        if not nombre:
            continue
        if not re.fullmatch(r"[A-Za-z_]\w*", nombre):
            raise ErrorCircuito(f"Nombre de resistencia no válido: «{nombre}» (usa letras, números y _).")
        if nombre in resist:
            raise ErrorCircuito(f"La resistencia «{nombre}» está repetida.")
        valor_txt = str(f.get("valor", "")).strip()
        try:
            if f.get("tipo") == "colores":
                valor, tol, cols, desc = decodificar_colores(valor_txt)
            else:
                valor, tol, cols = parse_valor(valor_txt), None, None
                desc = f"valor directo = {fmt(valor, 'Ω')}"
        except ErrorCircuito as e:
            raise ErrorCircuito(f"{nombre}: {e}")
        if valor <= 0:
            raise ErrorCircuito(f"{nombre}: la resistencia debe ser mayor que 0 Ω.")
        resist[nombre] = Res(nombre, valor, tol, cols, desc)
        L1.append(f"{nombre}: {desc}")

    try:
        raiz = Parser(str(d.get("expresion", ""))).parse()
    except ErrorCircuito as e:
        raise ErrorCircuito(f"Configuración: {e}")
    usados = [n.nombre for n in nodos(raiz) if n.tipo == "R"]
    faltan = [u for u in dict.fromkeys(usados) if u not in resist]
    if faltan:
        raise ErrorCircuito("Resistencias no definidas: " + ", ".join(faltan))
    rep = sorted({u for u in usados if usados.count(u) > 1})
    if rep:
        raise ErrorCircuito("Cada resistencia solo puede aparecer una vez en la configuración: " + ", ".join(rep))
    valores = {k: fmt(v.valor, "Ω") + (f" ±{v.tol:g}%" if v.tol else "") for k, v in resist.items()}
    avisos = [f"«{k}» está definida pero no se usa en la configuración." for k in resist if k not in usados]
    resist = {k: v for k, v in resist.items() if k in usados}

    nombrar(raiz, {"S": 0, "P": 0})
    asignar_ramas(raiz, [], [0])
    L2, L4 = [f"Estructura: {expr_str(raiz)}", "(|| se resuelve antes que +; se calcula de adentro hacia afuera)", ""], []
    calcular_R(raiz, resist, L2)
    I = vs / raiz.R
    L3 = [f"Req total = {pn(raiz.R)} Ω", f"I total = V / Req = {pn(vs)} / {pn(raiz.R)} = {pn(I)} A",
          f"P total = V · I = {pn(vs)} · {pn(I)} = {pn(vs * I)} W"]
    distribuir(raiz, vs, I, L4)
    hojas = [n for n in nodos(raiz) if n.tipo == "R"]
    L5 = [f"{n.nombre}: P = V·I = {pn(n.V)} · {pn(n.I)} = {pn(n.P)} W   (también I²·R = {pn(n.I ** 2 * n.R)} W)"
          for n in hojas]
    L5.append(f"Comprobación: ΣP resistencias = {pn(sum(n.P for n in hojas))} W  (debe ser {pn(raiz.P)} W)")

    def fila(n, nombre=None):
        return {"nombre": nombre or n.nombre, "R": fmt(n.R, "Ω"), "V": fmt(n.V, "V"),
                "I": fmt(n.I, "A"), "P": fmt(n.P, "W")}

    comps = []
    for n in hojas:
        r = fila(n)
        r["rama"] = " › ".join(f"Rama {i}" for i in n.camino) or "Principal"
        comps.append(r)
    ramas = []
    for n in sorted((n for n in nodos(raiz) if n.rama), key=lambda n: n.rama):
        r = fila(n, f"Rama {n.rama}")
        r["contenido"] = expr_str(n)
        ramas.append(r)
    return {"ok": True, "svg": svg_circuito(raiz, resist, vs), "avisos": avisos,
            "total": {"R": fmt(raiz.R, "Ω"), "V": fmt(vs, "V"), "I": fmt(I, "A"), "P": fmt(raiz.P, "W")},
            "componentes": comps, "ramas": ramas, "valores": valores,
            "pasos": [{"titulo": "1. Valor de cada resistencia", "lineas": L1},
                      {"titulo": "2. Resistencia equivalente (serie / paralelo)", "lineas": L2},
                      {"titulo": "3. Valores totales (Ley de Ohm)", "lineas": L3},
                      {"titulo": "4. Reparto de voltaje y corriente", "lineas": L4},
                      {"titulo": "5. Potencia de cada componente", "lineas": L5}]}


@app.post("/api/analizar")
def api_analizar():
    try:
        return jsonify(analizar(request.get_json(force=True, silent=True) or {}))
    except ErrorCircuito as e:
        return jsonify({"ok": False, "error": str(e)})
    except Exception as e:  # noqa
        return jsonify({"ok": False, "error": f"Error inesperado: {e}"})


# ═══════════════ CONVERSIÓN A PÁGINA WEB (HTML) ═══════════════
# Tres piezas convierten el análisis en una página:
#  1) svg_circuito()  -> Python arma el dibujo como TEXTO SVG (SVG es XML/HTML vectorial).
#  2) HTML (abajo)    -> la plantilla de la página (HTML + CSS + JavaScript) guardada como texto.
#  3) index()         -> ruta de Flask que entrega esa plantilla al navegador.
# Ya en el navegador, el JavaScript envía los datos a /api/analizar (que devuelve JSON con el
# SVG y las tablas) y los inserta en la página con innerHTML. Así el HTML se "construye" aquí.
HTML = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Analizador de circuitos resistivos</title>
<style>
:root{--bg:#f4f6fb;--card:#fff;--ink:#0f172a;--mut:#64748b;--acc:#4f46e5;--acc2:#06b6d4;--bd:#e2e8f0;--soft:#eef2ff}
@media(prefers-color-scheme:dark){:root{--bg:#0b1020;--card:#141b2f;--ink:#e5e7eb;--mut:#94a3b8;--bd:#263150;--soft:#1e2850}}
*{box-sizing:border-box}
body{margin:0;font-family:Inter,system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--ink)}
header{background:linear-gradient(120deg,var(--acc),var(--acc2));color:#fff;padding:26px 24px}
header h1{margin:0;font-size:1.6rem}header p{margin:4px 0 0;opacity:.92}
.app{display:grid;grid-template-columns:430px 1fr;gap:18px;padding:18px;max-width:1500px;margin:auto}
@media(max-width:980px){.app{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:16px;margin-bottom:16px;box-shadow:0 2px 10px #0000000d}
h2{font-size:1rem;margin:0 0 12px;display:flex;align-items:center;gap:8px}
.n{background:var(--acc);color:#fff;border-radius:50%;width:24px;height:24px;display:inline-grid;place-items:center;font-size:.8rem}
input,select,button{font:inherit;color:var(--ink);background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:8px 10px}
input:focus,select:focus{outline:2px solid var(--acc);border-color:transparent}
button{cursor:pointer;transition:.15s}button:hover{background:var(--soft)}
.pri{background:var(--acc);color:#fff;border:0;width:100%;padding:10px}.pri:hover{background:#4338ca}
.fila{border:1px solid var(--bd);border-radius:12px;padding:10px;margin-bottom:10px;background:var(--bg)}
.top{display:flex;gap:8px;align-items:center}.nom{width:72px;font-weight:700}
.mini{width:66px;height:28px;border-radius:8px;background:#f3e9d2;border:2px solid #1f2937;display:flex;justify-content:space-evenly;align-items:center;padding:0 6px;color:#1f2937;font-weight:700;font-size:.85rem;flex:none}
.mini i{width:6px;height:100%}
.seg{display:flex;margin-left:auto}.seg button{border-radius:0;padding:6px 10px;font-size:.85rem}
.seg button:first-child{border-radius:10px 0 0 10px}.seg button:last-child{border-radius:0 10px 10px 0;margin-left:-1px}
.seg .on{background:var(--acc);color:#fff;border-color:var(--acc)}
.det{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}.det input{flex:1;min-width:120px}.det select{padding:6px;font-size:.85rem}
.dec{color:var(--acc);font-weight:600;font-size:.85rem;margin-top:6px;min-height:1em}
#expr{width:100%;font-family:ui-monospace,Consolas,monospace;font-size:1.15rem;padding:12px}
.mut{color:var(--mut);font-size:.85rem}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.chip{border-radius:99px;padding:4px 12px;background:var(--soft);border-color:transparent;font-weight:600;color:var(--acc)}
.err{background:#fee2e2;color:#991b1b;padding:12px 14px;border-radius:12px;margin-bottom:14px}
.warn{background:#fef9c3;color:#854d0e;padding:10px 14px;border-radius:12px;margin-bottom:14px}
.tot{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.t{border-radius:14px;padding:14px;color:#fff}.t b{display:block;font-size:1.4rem}.t span{opacity:.9;font-size:.85rem}
.dib{overflow:auto;border:1px solid var(--bd);border-radius:12px;background:#fff;padding:8px}
.bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:8px;flex-wrap:wrap}
.bar button{padding:5px 12px;font-size:.85rem}
table{border-collapse:collapse;width:100%;font-size:.92rem}
th,td{padding:8px 10px;border-bottom:1px solid var(--bd);text-align:right}
th{background:var(--soft)}th:first-child,td:first-child{text-align:left}td:first-child{font-weight:700}
tr:hover td{background:var(--soft)}
.tabs{display:flex;gap:6px;margin-bottom:10px}.tabs .on{background:var(--acc);color:#fff;border-color:var(--acc)}
details{border:1px solid var(--bd);border-radius:12px;padding:8px 12px;margin-top:8px}summary{cursor:pointer;font-weight:600}
pre{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:10px;overflow:auto;font-size:.85rem;line-height:1.5}
</style></head><body>
<header><h1>⚡ Analizador de circuitos resistivos</h1>
<p>Define tus resistencias, escribe cómo se conectan y obtén el dibujo y todos los valores al instante.</p></header>
<div class="app">
 <div>
  <div class="card"><h2><span class="n">1</span>Fuente de voltaje</h2>
   <input id="vs" type="number" step="any" value="12" style="width:110px"> <b>V</b></div>
  <div class="card"><h2><span class="n">2</span>Resistencias</h2>
   <div id="filas"></div><button class="pri" id="add">+ Agregar resistencia</button></div>
  <div class="card"><h2><span class="n">3</span>Conexión del circuito</h2>
   <input id="expr" value="R1 + (R2 || R3) + R4" spellcheck="false" autocomplete="off">
   <p class="mut" style="margin:8px 0 0">Toca para insertar en la expresión:</p>
   <div class="chips" id="ops"></div><div class="chips" id="chips"></div>
   <p class="mut" style="margin:8px 0 0">O prueba un ejemplo:</p>
   <div class="chips"><button class="chip" data-ej="serie">Serie</button><button class="chip" data-ej="paralelo">Paralelo</button><button class="chip" data-ej="mixto">Mixto</button></div></div>
 </div>
 <div>
  <div id="msg"></div>
  <div id="res" hidden>
   <div class="card"><div class="bar"><h2 style="margin:0">Circuito <span class="mut">· los círculos azules numeran las ramas en paralelo</span></h2><button id="dl">⬇ Descargar SVG</button></div>
    <div class="dib" id="svg"></div></div>
   <div class="card"><h2>Valores totales</h2><div class="tot" id="tot"></div></div>
   <div class="card"><h2>Detalle por componente</h2>
    <div class="tabs"><button class="on" data-tab="tcomp">Componentes</button><button data-tab="tramas" id="tabR">Ramas en paralelo</button></div>
    <div id="tcomp"></div><div id="tramas" hidden></div></div>
   <div class="card"><button class="pri" id="btnPasos">🔍 Ver cálculos paso a paso</button><div id="pasos" hidden></div></div>
  </div>
 </div>
</div>
<script>
const C=__COLORES__;
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const DEF={4:['marron','negro','rojo','dorado'],5:['marron','negro','negro','rojo','marron'],6:['marron','negro','negro','rojo','marron','rojo']};
const mk=(nombre,valor)=>({nombre,tipo:'valor',valor,bandas:4,cols:DEF[4].slice()});
const mkc=(nombre,cols)=>({nombre,tipo:'colores',valor:'',bandas:cols.length,cols});
const EJ={
 serie:{expr:'R1 + R2 + R3',f:()=>[mk('R1','1k'),mk('R2','2.2k'),mk('R3','470')]},
 paralelo:{expr:'R1 || R2 || R3',f:()=>[mk('R1','1k'),mk('R2','2.2k'),mk('R3','470')]},
 mixto:{expr:'R1 + (R2 || (R3 + R4)) + R5',f:()=>[mk('R1','100'),mkc('R2',['rojo','rojo','marron','dorado']),mk('R3','330'),mk('R4','4k7'),mk('R5','1k')]}
};
let filas=[mk('R1','1k'),mk('R2','2.2k'),mk('R3','3.3k'),mk('R4','470')],timer,VAL={};
const pedir=()=>{clearTimeout(timer);timer=setTimeout(analizar,250)};
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};

function sel(lista,val,fn){const s=el('select');
 lista.forEach(o=>{const op=el('option','',o.t);op.value=o.v;op.selected=o.v===val;s.appendChild(op)});
 s.onchange=()=>fn(s.value);return s}
function prev(f){const p=el('div','mini');
 if(f.tipo==='colores')f.cols.forEach(c=>{const i=el('i');i.style.background=C.hex[c];p.appendChild(i)});else p.textContent='Ω';return p}
function seg(f){const d=el('div','seg');
 [['valor','Valor'],['colores','Colores']].forEach(([v,t])=>{const b=el('button',f.tipo===v?'on':'',t);
  b.onclick=()=>{f.tipo=v;renderFilas();pedir()};d.appendChild(b)});return d}
function banda(f,i){
 const nd=f.bandas<=4?2:3,lista=i<nd?C.dig:i===nd?C.colores:i===nd+1?C.tol:C.tc;
 const s=sel(lista.map(n=>({v:n,t:C.et[n]||n})),f.cols[i],v=>{f.cols[i]=v;renderFilas();pedir()});
 s.style.borderLeft='10px solid '+C.hex[f.cols[i]];return s}
function renderFilas(){
 const cont=$('filas');cont.innerHTML='';
 filas.forEach((f,i)=>{
  const d=el('div','fila'),top=el('div','top'),det=el('div','det'),dec=el('div','dec');
  dec.dataset.n=f.nombre;
  const nom=el('input','nom');nom.value=f.nombre;nom.title='Nombre de la variable';
  nom.oninput=()=>{f.nombre=nom.value.trim();dec.dataset.n=f.nombre;chips();pedir()};
  top.append(prev(f),nom,seg(f));
  if(f.tipo==='valor'){const v=el('input');v.value=f.valor;v.placeholder='ej. 220, 4.7k, 1M, 4k7';
   v.oninput=()=>{f.valor=v.value;pedir()};det.appendChild(v)}
  else{det.appendChild(sel([4,5,6].map(n=>({v:String(n),t:n+' bandas'})),String(f.bandas),v=>{f.bandas=+v;f.cols=DEF[v].slice();renderFilas();pedir()}));
   f.cols.forEach((_,k)=>det.appendChild(banda(f,k)))}
  const x=el('button','','✕');x.title='Quitar';x.onclick=()=>{filas.splice(i,1);renderFilas();pedir()};top.appendChild(x);
  d.append(top,det,dec);cont.appendChild(d)});
 chips();mostrarValores()}
function mostrarValores(){document.querySelectorAll('.dec').forEach(e=>e.textContent=VAL[e.dataset.n]?'= '+VAL[e.dataset.n]:'')}
function insertar(t){const e=$('expr'),s=e.selectionStart??e.value.length,f=e.selectionEnd??s;
 e.value=e.value.slice(0,s)+t+e.value.slice(f);e.focus();e.setSelectionRange(s+t.length,s+t.length);pedir()}
function chips(){const c=$('chips');c.innerHTML='';
 filas.filter(f=>f.nombre).forEach(f=>{const b=el('button','chip',f.nombre);b.onclick=()=>insertar(f.nombre);c.appendChild(b)})}
[[' + ','+  Serie'],[' || ','||  Paralelo'],['(','( '],[')',' )']].forEach(([t,l])=>{const b=el('button','chip',l.trim());b.onclick=()=>insertar(t);$('ops').appendChild(b)});
document.querySelectorAll('[data-ej]').forEach(b=>b.onclick=()=>{const e=EJ[b.dataset.ej];filas=e.f();$('expr').value=e.expr;renderFilas();pedir()});
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('[data-tab]').forEach(x=>{x.classList.toggle('on',x===b);$(x.dataset.tab).hidden=x!==b})});
$('add').onclick=()=>{let n=filas.length+1;while(filas.some(f=>f.nombre==='R'+n))n++;filas.push(mk('R'+n,'1k'));renderFilas();pedir()};
$('vs').oninput=pedir;$('expr').oninput=pedir;
$('btnPasos').onclick=()=>{const p=$('pasos');p.hidden=!p.hidden;$('btnPasos').textContent=p.hidden?'🔍 Ver cálculos paso a paso':'✖ Ocultar cálculos'};
$('dl').onclick=()=>{const a=el('a');a.href=URL.createObjectURL(new Blob([$('svg').innerHTML],{type:'image/svg+xml'}));a.download='circuito.svg';a.click()};

const tabla=(cab,rows)=>'<table><tr>'+cab.map(c=>`<th>${c}</th>`).join('')+'</tr>'+rows.map(r=>'<tr>'+r.map(c=>`<td>${esc(c)}</td>`).join('')+'</tr>').join('')+'</table>';

async function analizar(){
 const body={vs:$('vs').value,expresion:$('expr').value,
  resistencias:filas.map(f=>({nombre:f.nombre,tipo:f.tipo,valor:f.tipo==='colores'?f.cols.join(','):f.valor}))};
 let d;try{d=await (await fetch('/api/analizar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json()}
 catch(e){d={ok:false,error:'No se pudo conectar con el servidor.'}}
 if(!d.ok){$('msg').innerHTML='<div class="err">⚠️ '+esc(d.error)+'</div>';$('res').hidden=true;return}
 VAL=d.valores;mostrarValores();
 $('msg').innerHTML=d.avisos.map(a=>'<div class="warn">ℹ️ '+esc(a)+'</div>').join('');
 $('res').hidden=false;$('svg').innerHTML=d.svg;   // ← aquí el SVG generado en Python entra a la página
 const T=[['Resistencia equivalente',d.total.R,'#6366f1'],['Voltaje total',d.total.V,'#0ea5e9'],['Corriente total',d.total.I,'#f59e0b'],['Potencia total',d.total.P,'#ef4444']];
 $('tot').innerHTML=T.map(([a,b,c])=>`<div class="t" style="background:${c}"><b>${esc(b)}</b><span>${a}</span></div>`).join('');
 $('tcomp').innerHTML=tabla(['Componente','Rama','Resistencia','Voltaje','Corriente','Potencia'],d.componentes.map(c=>[c.nombre,c.rama,c.R,c.V,c.I,c.P]));
 $('tramas').innerHTML=tabla(['Rama','Contenido','Resistencia','Voltaje','Corriente','Potencia'],d.ramas.map(c=>[c.nombre,c.contenido,c.R,c.V,c.I,c.P]));
 $('tabR').hidden=!d.ramas.length;if(!d.ramas.length)document.querySelector('[data-tab=tcomp]').click();
 const P=$('pasos');P.innerHTML='';
 d.pasos.forEach(p=>{const det=el('details'),s=el('summary','',p.titulo),pre=el('pre','',p.lineas.join('\n'));det.open=true;det.append(s,pre);P.appendChild(det)});
}
renderFilas();analizar();
</script></body></html>"""


@app.get("/")
def index():
    # 3) Entrega la plantilla HTML; __COLORES__ se sustituye por los datos de colores (JSON)
    #    para que el JavaScript arme las listas desplegables de bandas.
    cfg = {"colores": list(HEX), "dig": list(DIGITO), "tol": list(TOL), "tc": list(TEMPCO),
           "hex": HEX, "et": ETIQUETA}
    return HTML.replace("__COLORES__", json.dumps(cfg)), 200, {"Content-Type": "text/html; charset=utf-8"}


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)

