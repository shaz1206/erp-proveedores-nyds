"""Dar de alta los productos terminados del Recetario NYDS, de un clic y listos para el CRM.

El recetario (data/recetario/Recetario_NYDS.json) trae los productos terminados. Al cargarlo
quedan como Borrador y "Por clasificar", y el CRM solo vende los Activos. Esta pantalla los
lista, sugiere una categoria, y con un clic los deja Activos (creando los que no existan y
enlazando su receta). Solo el nombre es necesario: precio, presentacion e imagen se
completan despues y, mientras tanto, el CRM pide el precio al armar el pedido.
"""
import json
import re
import unicodedata
from decimal import Decimal
from pathlib import Path

from flask import jsonify, redirect, render_template, request, session, url_for

RUTA_RECETARIO = Path(__file__).resolve().parent / "data" / "recetario" / "Recetario_NYDS.json"
FUENTE = "Recetario_NYDS"
SIN_CLASIFICAR = ("", "por clasificar")

# (palabras que aparecen en el nombre, categoria). La primera que coincide gana.
CATEGORIAS = (
    (("ropa", "suavizante", "textiles", "oxivendss"), "Lavandería"),
    (("lavatrastes", "cocina"), "Cocina"),
    (("jabon liquido", "manos"), "Higiene personal"),
    (("pisos", "pino"), "Pisos y superficies"),
    (("vidrios", "cristales"), "Vidrios"),
    (("vehiculos", "abrillantador", "cauchos"), "Automotriz"),
    (("cloro", "muriatico", "bio accion", "desinfectante", "banos", "sarro", "moho"), "Baños y desinfección"),
    (("multiusos", "desengrasante", "planchas", "cochambre"), "Multiusos y desengrasantes"),
)


def _clave(texto):
    t = unicodedata.normalize("NFKD", (texto or "").casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip()


def categoria_sugerida(nombre):
    n = _clave(nombre)
    for palabras, categoria in CATEGORIAS:
        if any(p in n for p in palabras):
            return categoria
    return "General"


def es_base_de_producto_final(nombre, nombres):
    """Un «... CONCENTRADO» que tiene su «... PRODUCTO FINAL» hermano es solo la base del producto final."""
    n = _clave(nombre)
    if "concentrado" not in n or "producto final" in n:
        return False
    prefijo = n.split(" concentrado")[0].strip()
    return any("producto final" in otro and otro.startswith(prefijo) for otro in nombres)


def productos_del_recetario(datos=None):
    if datos is None:
        datos = json.loads(RUTA_RECETARIO.read_text(encoding="utf-8"))
    recetas = [r for r in datos.get("recetas", []) if r.get("tipo") == "Producto terminado"]
    nombres = [_clave(r["nombre"]) for r in recetas]
    return [
        {"clave": r["clave"], "nombre": r["nombre"], "categoria": categoria_sugerida(r["nombre"]),
         "base": es_base_de_producto_final(r["nombre"], nombres)}
        for r in recetas
    ]


def registrar_alta_productos(app, db, ProductoTerminado, Receta, admin_required_json):

    def generar_sku():
        mayor = 0
        for (sku,) in db.session.query(ProductoTerminado.sku).filter(ProductoTerminado.sku.like("PT-%")):
            sufijo = sku[3:]
            if sufijo.isdigit():
                mayor = max(mayor, int(sufijo))
        return f"PT-{mayor + 1:06d}"

    def buscar(item, por_receta, por_nombre):
        receta = por_receta.get(item["clave"])
        if receta is not None and receta.producto is not None:
            return receta.producto, receta
        return por_nombre.get(_clave(item["nombre"])), receta

    def contexto():
        recetas = Receta.query.filter(Receta.clave_origen.like(FUENTE + ":%")).all()
        por_receta = {r.clave_origen.split(":", 1)[1]: r for r in recetas}
        por_nombre = {}
        for p in ProductoTerminado.query.all():
            por_nombre.setdefault(_clave(p.nombre), p)
        return por_receta, por_nombre

    @app.route("/admin/productos-terminados/alta-recetario")
    def vista_alta_productos():
        if not session.get("admin_logueado"):
            return redirect(url_for("login_admin"))
        categorias = [c[0] for c in db.session.query(ProductoTerminado.categoria).distinct().order_by(ProductoTerminado.categoria)]
        return render_template("alta_productos.html", categorias=sorted(set(categorias) | {c for _, c in CATEGORIAS} | {"General"}))

    @app.route("/api/productos-terminados/alta-recetario", methods=["GET"])
    @admin_required_json
    def api_alta_productos_lista():
        por_receta, por_nombre = contexto()
        filas = []
        for item in productos_del_recetario():
            producto, _ = buscar(item, por_receta, por_nombre)
            if producto is None:
                estado, sku, categoria = "nuevo", "", item["categoria"]
            else:
                estado = {"Activo": "activo", "Borrador": "borrador"}.get(producto.estatus, "inactivo")
                sku = producto.sku
                categoria = producto.categoria if _clave(producto.categoria) not in SIN_CLASIFICAR else item["categoria"]
            filas.append({**item, "estado": estado, "sku": sku, "categoria": categoria})
        pendientes = sum(1 for f in filas if f["estado"] != "activo")
        return jsonify({"productos": filas, "pendientes": pendientes})

    @app.route("/api/productos-terminados/alta-recetario", methods=["POST"])
    @admin_required_json
    def api_alta_productos_guardar():
        datos = request.get_json(silent=True) or {}
        elegidos = datos.get("productos")
        if not isinstance(elegidos, list) or not elegidos:
            return jsonify({"error": "Marca al menos un producto."}), 400
        catalogo = {i["clave"]: i for i in productos_del_recetario()}
        por_receta, por_nombre = contexto()
        creados = activados = ya_activos = 0
        try:
            for e in elegidos:
                item = catalogo.get(str((e or {}).get("clave", "")))
                if item is None:
                    continue
                categoria = str((e or {}).get("categoria") or item["categoria"]).strip()[:100] or item["categoria"]
                producto, receta = buscar(item, por_receta, por_nombre)
                if producto is None:
                    producto = ProductoTerminado(sku=generar_sku(), nombre=item["nombre"][:150], categoria=categoria,
                                                 estatus="Activo", stock_minimo=Decimal(0), puntos_venta=[])
                    db.session.add(producto)
                    db.session.flush()
                    por_nombre[_clave(item["nombre"])] = producto
                    creados += 1
                else:
                    if producto.estatus == "Activo":
                        ya_activos += 1
                    else:
                        producto.estatus = "Activo"
                        activados += 1
                    if _clave(producto.categoria) in SIN_CLASIFICAR:
                        producto.categoria = categoria
                if receta is not None and receta.producto_id is None:
                    receta.producto_id = producto.id
            db.session.commit()
        except Exception:
            db.session.rollback()
            return jsonify({"error": "No se pudo guardar; no se cambió nada."}), 400
        return jsonify({"creados": creados, "activados": activados, "ya_activos": ya_activos}), 201
