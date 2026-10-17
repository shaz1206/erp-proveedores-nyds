"""Productos que un cliente compra seguido, para armar su pedido con un toque.

Lee los pedidos del CRM (sin cancelados ni devueltos) y agrupa por producto y
presentacion. No crea tablas ni cambia reglas de pedidos.
"""
import re

from flask import jsonify, session

if __package__:
    from .crm import ESTADOS_SIN_VENTA
else:
    from crm import ESTADOS_SIN_VENTA

PRESENTACION_EN_NOTAS = re.compile(r"^Presentaci[oó]n:\s*(.+)$", re.IGNORECASE)
MAX_HABITUALES = 24


def presentacion_de(notas):
    coincide = PRESENTACION_EN_NOTAS.match((notas or "").strip())
    return coincide.group(1).strip() if coincide else ""


def registrar_habituales(app, db, PedidoCRM, PedidoLineaCRM):
    @app.route("/api/crm/clientes/<int:cliente_id>/habituales")
    def api_cliente_habituales(cliente_id):
        if not (session.get("admin_logueado") or session.get("crm_logueado")):
            return jsonify({"error": "No autorizado"}), 403
        pedidos = (PedidoCRM.query.filter(PedidoCRM.cliente_id == cliente_id, PedidoCRM.estado.notin_(list(ESTADOS_SIN_VENTA)))
                   .order_by(PedidoCRM.fecha.desc(), PedidoCRM.id.desc()).all())
        grupos = {}
        for pedido in pedidos:  # del mas reciente al mas antiguo
            vistos = set()
            for linea in pedido.lineas:
                presentacion = presentacion_de(linea.notas)
                clave = (linea.producto_id, presentacion)
                g = grupos.setdefault(clave, {
                    "producto_id": linea.producto_id, "nombre": linea.producto.nombre if linea.producto else "Producto",
                    "presentacion": presentacion, "veces": 0, "cantidad_total": 0.0,
                    "ultima_cantidad": float(linea.cantidad), "ultimo_precio": float(linea.precio_unitario),
                    "ultima_fecha": pedido.fecha.date().isoformat()})
                g["cantidad_total"] += float(linea.cantidad)
                if clave not in vistos:
                    g["veces"] += 1
                    vistos.add(clave)
        # Primero los que mas veces pidio; a igualdad, los mas recientes (orden estable en dos pasadas).
        productos = sorted(grupos.values(), key=lambda g: g["ultima_fecha"], reverse=True)
        productos.sort(key=lambda g: g["veces"], reverse=True)
        ultimo = None
        if pedidos:
            p = pedidos[0]
            ultimo = {"folio": p.folio, "fecha": p.fecha.date().isoformat(), "lineas": [{
                "producto_id": l.producto_id, "presentacion": presentacion_de(l.notas),
                "cantidad": float(l.cantidad), "precio": float(l.precio_unitario)} for l in p.lineas]}
        return jsonify({"pedidos": len(pedidos), "productos": productos[:MAX_HABITUALES], "ultimo_pedido": ultimo})

    return api_cliente_habituales
