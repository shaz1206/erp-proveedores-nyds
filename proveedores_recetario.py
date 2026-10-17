"""Proveedores que menciona el Recetario NYDS.

Lee el recetario ya extraido (data/recetario/Recetario_NYDS.json), junta los
proveedores citados en los ingredientes y, con un clic, los da de alta como
Borrador. Solo lleva el nombre y las materias primas que el recetario les
atribuye. El documento no trae telefono, RFC ni domicilio; esos datos son opcionales y
se capturan cuando se tengan.
"""
import json
import re
import unicodedata
from pathlib import Path

from flask import jsonify

RUTA_RECETARIO = Path(__file__).resolve().parent / "data" / "recetario" / "Recetario_NYDS.json"

# Nombre tal como aparece en el recetario (sin acentos ni mayusculas) -> nombre limpio.
NOMBRES = {
    "la formula": "La Fórmula",
    "clean chemical": "Clean Chemical",
    "dcc esencias": "DCC Esencias",
    "departamental": "Departamental",
    "power clean": "Power Clean",
    "mercado libre": "Mercado Libre",
    "pochteca": "Pochteca",
}
# Textos que NO son proveedores: se hace en casa o es una subreceta.
INTERNOS = ("propio", "subreceta")


def _clave(texto):
    t = unicodedata.normalize("NFKD", (texto or "").casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip()


def proveedores_del_recetario(datos=None):
    """Devuelve {nombre: {"materias": [..], "notas": [..]}} con lo que cita el recetario."""
    if datos is None:
        datos = json.loads(RUTA_RECETARIO.read_text(encoding="utf-8"))
    encontrados = {}
    for receta in datos.get("recetas", []):
        for comp in receta.get("componentes", []):
            texto = (comp.get("proveedor") or "").strip()
            if not texto or any(_clave(texto).startswith(i) for i in INTERNOS):
                continue
            partes = [p.strip() for p in texto.split("/") if p.strip()]
            for parte in partes:
                clave = _clave(parte)
                nombre = NOMBRES.get(clave)
                if nombre is None:
                    # Parte sin nombre conocido (p. ej. "Fresno" junto a Pochteca): no se da de alta sola.
                    if len(partes) > 1:
                        base = NOMBRES.get(_clave(partes[0]))
                        if base:
                            info = encontrados.setdefault(base, {"materias": {}, "notas": set()})
                            info["notas"].add(f"El recetario cita «{texto}» para {comp['nombre']}; confirmar qué es «{parte}».")
                        continue
                    nombre = parte.strip().title()
                info = encontrados.setdefault(nombre, {"materias": {}, "notas": set()})
                info["materias"].setdefault(_clave(comp["nombre"]), comp["nombre"])
    return {
        n: {"materias": sorted(i["materias"].values(), key=_clave), "notas": sorted(i["notas"])}
        for n, i in sorted(encontrados.items(), key=lambda kv: _clave(kv[0]))
    }


def registrar_proveedores_recetario(app, db, Proveedor, Bitacora, admin_required_json, usuario_actual, siguiente_codigo, rfc_marcador):
    def pendientes():
        existentes = {_clave(p.razon_social) for p in Proveedor.query.all()}
        existentes |= {_clave(p.nombre_comercial) for p in Proveedor.query.all() if p.nombre_comercial}
        todos = proveedores_del_recetario()
        return {n: i for n, i in todos.items() if _clave(n) not in existentes}, len(todos)

    @app.route("/api/proveedores/del-recetario", methods=["GET", "POST"])
    @admin_required_json
    def proveedores_desde_recetario():
        from flask import request

        try:
            faltan, total = pendientes()
        except (OSError, ValueError):
            return jsonify({"pendientes": [], "total": 0}), 200
        if request.method == "GET":
            return jsonify({
                "total": total,
                "pendientes": [{"nombre": n, "materias": len(i["materias"])} for n, i in faltan.items()],
            }), 200
        usuario = usuario_actual()
        creados = []
        for nombre, info in faltan.items():
            codigo = siguiente_codigo()
            notas = ["Creado desde el Recetario NYDS. Teléfono, RFC, domicilio y condiciones son opcionales; se capturan cuando se tengan."]
            if info["materias"]:
                notas.append("Materias primas que el recetario le atribuye: " + ", ".join(info["materias"]) + ".")
            notas.extend(info["notas"])
            proveedor = Proveedor(
                codigo_proveedor=codigo,
                razon_social=nombre,
                rfc=rfc_marcador(codigo),  # marcador interno: el RFC es opcional y no viene en el recetario
                tipo_proveedor="Materia prima",
                estatus="Borrador",
                calle_numero="",
                colonia="",
                codigo_postal="",
                condicion_pago="",
                tiempo_entrega_normal=0,
                notas_generales="\n".join(notas),
                creado_por=usuario,
                actualizado_por=usuario,
            )
            db.session.add(proveedor)
            db.session.flush()
            db.session.add(Bitacora(
                id_proveedor=proveedor.id_proveedor,
                accion="Alta",
                descripcion="Creado desde el Recetario NYDS (Borrador)",
                usuario=usuario,
            ))
            creados.append(nombre)
        db.session.commit()
        return jsonify({"creados": creados}), 201
