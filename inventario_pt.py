"""Inventario de producto terminado en el ERP.

Usa los mismos movimientos (MovimientoPT) que genera el CRM al llenar, sacar a
ruta y entregar pedidos, y permite registrar desde el ERP el saldo inicial, la
produccion y los ajustes de almacen. La existencia de un producto es la suma
de sus movimientos por ubicacion; lo reservado son las reservas activas de
pedidos del CRM."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
import secrets

from flask import abort, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError

if __package__:
    from .reportes_pdf import celda, generar_pdf, respuesta_pdf
else:
    from reportes_pdf import celda, generar_pdf, respuesta_pdf

UBICACION_PT = "Almacen PT"
UBICACION_TRANSITO = "En transito"
TIPOS_ENTRADA = ["Saldo inicial", "Produccion", "Ajuste de entrada"]
TIPOS_MOVIMIENTO = TIPOS_ENTRADA + ["Ajuste de salida"]


def registrar_inventario_pt(app, db, PT, MovimientoPT, ReservaERP, usuario_actual):
    def protegido(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not session.get("admin_logueado"):
                return redirect("/login")
            session.setdefault("inventario_pt_csrf", secrets.token_hex(24))
            if request.method == "POST" and not secrets.compare_digest(request.form.get("csrf", ""), session["inventario_pt_csrf"]):
                abort(400)
            return fn(*a, **kw)

        return wrapper

    def saldos_por_producto():
        """producto_id -> {almacen, transito, reservado}"""
        saldos = {}
        filas = (
            db.session.query(MovimientoPT.producto_id, MovimientoPT.ubicacion, db.func.sum(MovimientoPT.cantidad))
            .group_by(MovimientoPT.producto_id, MovimientoPT.ubicacion)
            .all()
        )
        for producto_id, ubicacion, total in filas:
            s = saldos.setdefault(producto_id, {"almacen": Decimal(0), "transito": Decimal(0), "reservado": Decimal(0)})
            if ubicacion == UBICACION_PT:
                s["almacen"] += Decimal(str(total or 0))
            elif ubicacion == UBICACION_TRANSITO:
                s["transito"] += Decimal(str(total or 0))
        reservas = (
            db.session.query(ReservaERP.producto_id, db.func.sum(ReservaERP.cantidad))
            .filter(ReservaERP.tipo == "Producto terminado", ReservaERP.estado == "Activa")
            .group_by(ReservaERP.producto_id)
            .all()
        )
        for producto_id, total in reservas:
            s = saldos.setdefault(producto_id, {"almacen": Decimal(0), "transito": Decimal(0), "reservado": Decimal(0)})
            s["reservado"] += Decimal(str(total or 0))
        return saldos

    def resumen():
        saldos = saldos_por_producto()
        productos = PT.query.order_by(PT.nombre).all()
        filas = []
        for p in productos:
            s = saldos.get(p.id)
            if p.estatus != "Activo" and not s:
                continue
            s = s or {"almacen": Decimal(0), "transito": Decimal(0), "reservado": Decimal(0)}
            disponible = s["almacen"] - s["reservado"]
            minimo = p.stock_minimo or Decimal(0)
            # Lo reservado ya esta comprometido con pedidos: la alerta se mide sobre lo disponible.
            if s["almacen"] <= 0:
                estado = "Sin existencia"
            elif disponible <= 0:
                estado = "Todo reservado"
            elif minimo and disponible < minimo:
                estado = "Bajo mínimo"
            else:
                estado = "OK"
            filas.append({"producto": p, **s, "disponible": disponible, "minimo": minimo, "estado": estado})
        return filas

    def historial(limite=300):
        return MovimientoPT.query.order_by(MovimientoPT.fecha.desc(), MovimientoPT.id.desc()).limit(limite).all()

    @app.route("/admin/inventario-pt", methods=["GET", "POST"])
    @protegido
    def inventario_pt():
        error = None
        if request.method == "POST":
            try:
                token = request.form.get("token", "").strip()
                if not token or len(token) > 80:
                    raise ValueError("Recarga la página e intenta de nuevo.")
                if MovimientoPT.query.filter_by(token=token).first():
                    return redirect(url_for("inventario_pt", guardado=1))
                producto = db.session.get(PT, int(request.form.get("producto_id") or 0))
                if not producto:
                    raise ValueError("Selecciona un producto terminado.")
                tipo = request.form.get("tipo", "")
                if tipo not in TIPOS_MOVIMIENTO:
                    raise ValueError("Tipo de movimiento inválido.")
                cantidad = Decimal(request.form.get("cantidad", ""))
                if not cantidad.is_finite() or cantidad <= 0 or cantidad >= Decimal("1000000000") or cantidad.as_tuple().exponent < -6:
                    raise ValueError("Cantidad positiva, menor a mil millones y con máximo seis decimales.")
                referencia = request.form.get("referencia", "").strip()
                motivo = request.form.get("motivo", "").strip()
                if not referencia or len(referencia) > 160:
                    raise ValueError("Indica una referencia (conteo, lote de producción, documento) de hasta 160 caracteres.")
                if not motivo or len(motivo) > 500:
                    raise ValueError("Indica el motivo del movimiento (hasta 500 caracteres).")
                s = saldos_por_producto().get(producto.id, {"almacen": Decimal(0), "reservado": Decimal(0)})
                if tipo == "Saldo inicial" and MovimientoPT.query.filter_by(producto_id=producto.id, ubicacion=UBICACION_PT).first():
                    raise ValueError("Este producto ya tiene movimientos en almacén. Usa un ajuste para corregirlo.")
                if tipo == "Ajuste de salida":
                    libre = s["almacen"] - s["reservado"]
                    if cantidad > libre:
                        raise ValueError(f"La salida supera lo disponible sin reservar ({libre:g} pieza(s)). Lo reservado está comprometido con pedidos.")
                    cantidad = -cantidad
                db.session.add(MovimientoPT(
                    producto_id=producto.id,
                    tipo=tipo,
                    cantidad=cantidad,
                    ubicacion=UBICACION_PT,
                    referencia=referencia,
                    motivo=motivo,
                    usuario=usuario_actual(),
                    token=token,
                ))
                db.session.commit()
                return redirect(url_for("inventario_pt", guardado=1))
            except (ValueError, InvalidOperation, IntegrityError) as e:
                db.session.rollback()
                error = str(e) if isinstance(e, ValueError) else "Revisa la cantidad."
        filas = resumen()
        return render_template(
            "inventario_pt.html",
            filas=filas,
            alertas=sum(1 for f in filas if f["estado"] != "OK"),
            movimientos=historial(),
            productos=PT.query.filter_by(estatus="Activo").order_by(PT.nombre).all(),
            tipos=TIPOS_MOVIMIENTO,
            token=secrets.token_hex(24),
            error=error,
        ), (400 if error else 200)

    @app.route("/admin/inventario-pt/pdf")
    @protegido
    def inventario_pt_pdf():
        filas = resumen()
        buffer = generar_pdf(
            titulo="Inventario de producto terminado",
            subtitulo=f"Existencias al {datetime.now().strftime('%d/%m/%Y %H:%M')} · {len(filas)} producto(s)",
            secciones=[
                {
                    "titulo": "Existencias",
                    "columnas": ["Producto", "Almacén", "En tránsito", "Reservado", "Disponible", "Mínimo", "Estado"],
                    "filas": [
                        [
                            celda(f"{f['producto'].sku} · {f['producto'].nombre}"),
                            celda(f"{f['almacen']:g}"),
                            celda(f"{f['transito']:g}", muted=True),
                            celda(f"{f['reservado']:g}", muted=True),
                            celda(f"{f['disponible']:g}", negrita=True),
                            celda(f"{f['minimo']:g}", muted=True),
                            celda(f["estado"], negrita=f["estado"] != "OK"),
                        ]
                        for f in filas
                    ],
                    "anchos": [6, 1.9, 1.9, 1.9, 2, 1.7, 2.9],
                    "vacio": "No hay productos terminados activos.",
                },
                {
                    "titulo": "Últimos movimientos",
                    "columnas": ["Fecha", "Producto", "Tipo", "Ubicación", "Cantidad", "Referencia"],
                    "filas": [
                        [
                            celda(m.fecha.strftime("%d/%m/%Y %H:%M")),
                            celda(m.producto.nombre if m.producto else "—"),
                            celda(m.tipo),
                            celda(m.ubicacion, muted=True),
                            celda(f"{m.cantidad:+g}"),
                            celda(m.referencia, muted=True),
                        ]
                        for m in historial(100)
                    ],
                    "anchos": [2.6, 4.4, 2.8, 2.3, 1.9, 4.4],
                    "vacio": "Sin movimientos.",
                },
            ],
        )
        return respuesta_pdf(buffer, "inventario-producto-terminado.pdf")
