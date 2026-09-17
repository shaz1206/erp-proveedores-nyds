"""CRM de pedidos y entregas conectado con inventario y producto terminado."""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
import os
import secrets

from flask import abort, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError


DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def fecha_larga_es(dt):
    """'jueves 17 de septiembre', para encabezados del CRM."""
    return f"{DIAS_ES[dt.weekday()]} {dt.day} de {MESES_ES[dt.month]}"


def hace_texto(dt, ahora=None):
    """Texto relativo tipo 'hace 3 h' / 'hace 2 días' a partir de una fecha real."""
    ahora = ahora or datetime.utcnow()
    segundos = max(0, (ahora - dt).total_seconds())
    if segundos < 3600:
        return f"hace {max(1, int(segundos // 60))} min"
    if segundos < 86400:
        return f"hace {int(segundos // 3600)} h"
    dias = int(segundos // 86400)
    return f"hace {dias} día" + ("" if dias == 1 else "s")


def dinero(valor):
    return "${:,.0f}".format(float(valor or 0))


def canal_clase(canal):
    return {"whatsapp": "wa", "instagram": "ig", "facebook": "fb"}.get((canal or "").strip().lower(), "mn")


ESTADOS_PEDIDO = [
    "Recibido",
    "Pendiente de pago",
    "Confirmado",
    "Pendiente de produccion",
    "En preparacion",
    "Listo para entrega",
    "En ruta",
    "Entregado",
    "Intento fallido",
    "Cancelado",
    "Devuelto",
]

RESERVAS_ABIERTAS = {"Activa"}
UBICACION_PT = "Almacen PT"
UBICACION_TRANSITO = "En transito"
CRM_ROLES = {"supervisor", "ventas", "preparacion", "reparto", "cobranza"}
CRM_USUARIOS = {
    "crm.admin": {"password_env": "NYDS_CRM_ADMIN_PASSWORD", "rol": "supervisor", "nombre": "Supervisor CRM"},
    "ventas.nyds": {"password_env": "NYDS_CRM_VENTAS_PASSWORD", "rol": "ventas", "nombre": "Ventas NYDS"},
    "preparacion.nyds": {"password_env": "NYDS_CRM_PREPARACION_PASSWORD", "rol": "preparacion", "nombre": "Preparacion NYDS"},
    "reparto.nyds": {"password_env": "NYDS_CRM_REPARTO_PASSWORD", "rol": "reparto", "nombre": "Reparto NYDS"},
    "caja.nyds": {"password_env": "NYDS_CRM_CAJA_PASSWORD", "rol": "cobranza", "nombre": "Caja NYDS"},
}


def registrar_crm(app, db, MP, PT, Receta, RecetaIngrediente, MovimientoMP, AjusteInventario, usuario_actual):
    class ClienteCRM(db.Model):
        __tablename__ = "crm_clientes"
        id = db.Column(db.Integer, primary_key=True)
        nombre = db.Column(db.String(180), nullable=False)
        telefono = db.Column(db.String(40), nullable=False, index=True)
        correo = db.Column(db.String(180))
        canal_origen = db.Column(db.String(40), nullable=False, default="Manual")
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        domicilios = db.relationship("ClienteDomicilio", backref="cliente", lazy=True, cascade="all, delete-orphan")

    class ClienteDomicilio(db.Model):
        __tablename__ = "crm_cliente_domicilios"
        id = db.Column(db.Integer, primary_key=True)
        cliente_id = db.Column(db.Integer, db.ForeignKey("crm_clientes.id"), nullable=False)
        alias = db.Column(db.String(80), nullable=False, default="Principal")
        direccion = db.Column(db.String(280), nullable=False)
        referencias = db.Column(db.String(500))
        zona = db.Column(db.String(100), nullable=False, default="Cancun")
        cobertura = db.Column(db.Boolean, nullable=False, default=True)
        lat = db.Column(db.Numeric(12, 8))
        lng = db.Column(db.Numeric(12, 8))

    class PedidoCRM(db.Model):
        __tablename__ = "crm_pedidos"
        id = db.Column(db.Integer, primary_key=True)
        folio = db.Column(db.String(24), unique=True, nullable=False)
        cliente_id = db.Column(db.Integer, db.ForeignKey("crm_clientes.id"), nullable=False)
        cliente = db.relationship(ClienteCRM)
        domicilio_id = db.Column(db.Integer, db.ForeignKey("crm_cliente_domicilios.id"), nullable=False)
        domicilio = db.relationship(ClienteDomicilio)
        canal = db.Column(db.String(40), nullable=False, default="Manual")
        estado = db.Column(db.String(40), nullable=False, default="Recibido")
        forma_pago = db.Column(db.String(40), nullable=False, default="Contra entrega")
        pago_requerido_antes_preparar = db.Column(db.Boolean, nullable=False, default=False)
        entrega_propia = db.Column(db.Boolean, nullable=False, default=False)
        colaborador_asignado = db.Column(db.String(100))
        importe_total = db.Column(db.Numeric(18, 2), nullable=False, default=0)
        importe_pagado = db.Column(db.Numeric(18, 2), nullable=False, default=0)
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        fecha_actualizacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
        token = db.Column(db.String(80), unique=True, nullable=False)
        version = db.Column(db.Integer, nullable=False, default=0)
        lineas = db.relationship("PedidoLineaCRM", backref="pedido", lazy=True, cascade="all, delete-orphan")

        @property
        def saldo(self):
            return self.importe_total - self.importe_pagado

    class PedidoLineaCRM(db.Model):
        __tablename__ = "crm_pedido_lineas"
        id = db.Column(db.Integer, primary_key=True)
        pedido_id = db.Column(db.Integer, db.ForeignKey("crm_pedidos.id"), nullable=False)
        producto_id = db.Column(db.Integer, db.ForeignKey("productos_terminados.id"), nullable=False)
        producto = db.relationship(PT)
        cantidad = db.Column(db.Numeric(18, 6), nullable=False)
        precio_unitario = db.Column(db.Numeric(18, 2), nullable=False)
        modo_reserva = db.Column(db.String(40), nullable=False, default="Sin reserva")
        notas = db.Column(db.String(500))

        @property
        def subtotal(self):
            return self.cantidad * self.precio_unitario

    class PagoPedidoCRM(db.Model):
        __tablename__ = "crm_pedido_pagos"
        id = db.Column(db.Integer, primary_key=True)
        pedido_id = db.Column(db.Integer, db.ForeignKey("crm_pedidos.id"), nullable=False)
        pedido = db.relationship(PedidoCRM)
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        monto = db.Column(db.Numeric(18, 2), nullable=False)
        metodo = db.Column(db.String(40), nullable=False)
        referencia = db.Column(db.String(160), nullable=False)
        usuario = db.Column(db.String(100), nullable=False)
        token = db.Column(db.String(80), unique=True, nullable=False)

    class ReservaERP(db.Model):
        __tablename__ = "crm_reservas_erp"
        id = db.Column(db.Integer, primary_key=True)
        pedido_id = db.Column(db.Integer, db.ForeignKey("crm_pedidos.id"), nullable=False, index=True)
        linea_id = db.Column(db.Integer, db.ForeignKey("crm_pedido_lineas.id"), nullable=False, index=True)
        tipo = db.Column(db.String(30), nullable=False)
        producto_id = db.Column(db.Integer, db.ForeignKey("productos_terminados.id"))
        mp_id = db.Column(db.Integer, db.ForeignKey("materias_primas.id_mp"))
        cantidad = db.Column(db.Numeric(18, 6), nullable=False)
        unidad = db.Column(db.String(20), nullable=False)
        estado = db.Column(db.String(20), nullable=False, default="Activa")
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        usuario = db.Column(db.String(100), nullable=False)
        referencia = db.Column(db.String(160), nullable=False)

    class OrdenLlenadoCRM(db.Model):
        __tablename__ = "crm_ordenes_llenado"
        id = db.Column(db.Integer, primary_key=True)
        pedido_id = db.Column(db.Integer, db.ForeignKey("crm_pedidos.id"), nullable=False)
        linea_id = db.Column(db.Integer, db.ForeignKey("crm_pedido_lineas.id"), nullable=False)
        producto_id = db.Column(db.Integer, db.ForeignKey("productos_terminados.id"), nullable=False)
        producto = db.relationship(PT)
        cantidad = db.Column(db.Numeric(18, 6), nullable=False)
        estado = db.Column(db.String(30), nullable=False, default="Pendiente")
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        fecha_cierre = db.Column(db.DateTime)
        usuario = db.Column(db.String(100), nullable=False)
        token_cierre = db.Column(db.String(80), unique=True)

    class MovimientoPT(db.Model):
        __tablename__ = "crm_inventario_pt_movimientos"
        id = db.Column(db.Integer, primary_key=True)
        producto_id = db.Column(db.Integer, db.ForeignKey("productos_terminados.id"), nullable=False)
        producto = db.relationship(PT)
        pedido_id = db.Column(db.Integer, db.ForeignKey("crm_pedidos.id"))
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        tipo = db.Column(db.String(40), nullable=False)
        cantidad = db.Column(db.Numeric(18, 6), nullable=False)
        unidad = db.Column(db.String(20), nullable=False, default="pieza")
        ubicacion = db.Column(db.String(120), nullable=False, default=UBICACION_PT)
        referencia = db.Column(db.String(160), nullable=False)
        usuario = db.Column(db.String(100), nullable=False)
        token = db.Column(db.String(80), unique=True, nullable=False)

    class EventoCRM(db.Model):
        __tablename__ = "crm_eventos"
        id = db.Column(db.Integer, primary_key=True)
        pedido_id = db.Column(db.Integer, db.ForeignKey("crm_pedidos.id"), nullable=False)
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        evento = db.Column(db.String(80), nullable=False)
        descripcion = db.Column(db.String(500), nullable=False)
        usuario = db.Column(db.String(100), nullable=False)
        payload = db.Column(db.JSON)

    class EvidenciaEntregaCRM(db.Model):
        __tablename__ = "crm_entrega_evidencias"
        id = db.Column(db.Integer, primary_key=True)
        pedido_id = db.Column(db.Integer, db.ForeignKey("crm_pedidos.id"), nullable=False)
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        otp = db.Column(db.String(20))
        receptor = db.Column(db.String(160))
        firma = db.Column(db.String(500))
        foto = db.Column(db.String(500))
        lat = db.Column(db.Numeric(12, 8))
        lng = db.Column(db.Numeric(12, 8))
        incidencia = db.Column(db.String(500))
        usuario = db.Column(db.String(100), nullable=False)

    def crm_usuario_actual():
        if session.get("crm_logueado"):
            return session.get("crm_usuario", "crm.nyds")
        return usuario_actual()

    def rol_crm_actual():
        if session.get("crm_logueado"):
            return session.get("crm_rol", "")
        if session.get("admin_logueado"):
            return "supervisor"
        return ""

    def tiene_rol(*roles):
        rol = rol_crm_actual()
        return rol == "supervisor" or rol in roles

    def protegido(*roles):
        def decorador(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                if not session.get("crm_logueado") and not session.get("admin_logueado"):
                    return redirect(url_for("crm_login"))
                if roles and not tiene_rol(*roles):
                    abort(403)
                return fn(*args, **kwargs)

            return wrapper

        return decorador

    def protegido_json(*roles):
        def decorador(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                if not session.get("crm_logueado") and not session.get("admin_logueado"):
                    return jsonify({"error": "No autorizado"}), 403
                if roles and not tiene_rol(*roles):
                    return jsonify({"error": "Permiso insuficiente"}), 403
                return fn(*args, **kwargs)

            return wrapper

        return decorador

    def protegido_erp_json(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("admin_logueado"):
                return jsonify({"error": "No autorizado"}), 403
            return fn(*args, **kwargs)

        return wrapper

    def protegido_erp(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("admin_logueado"):
                return redirect(url_for("login_admin"))
            return fn(*args, **kwargs)

        return wrapper

    def numero(valor, decimales=6, positivo=False):
        try:
            n = Decimal(str(valor))
            if not n.is_finite() or n < 0 or n >= Decimal("1000000000") or n.as_tuple().exponent < -decimales or (positivo and n == 0):
                raise ValueError
            return n
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("Usa cantidades validas y positivas.")

    def texto(data, campo, maximo, obligatorio=False, default=""):
        v = str(data.get(campo, default) or "").strip()
        if len(v) > maximo or (obligatorio and not v):
            raise ValueError(f"Revisa {campo}: obligatorio y maximo {maximo} caracteres.")
        return v

    def registrar_evento(pedido, evento, descripcion, payload=None):
        db.session.add(EventoCRM(pedido_id=pedido.id, evento=evento, descripcion=descripcion[:500], usuario=crm_usuario_actual(), payload=payload))

    def folio_pedido():
        ultimo = db.session.query(db.func.max(PedidoCRM.id)).scalar() or 0
        return f"PED-{ultimo + 1:06d}"

    def saldo_pt(producto_id, ubicacion=UBICACION_PT):
        total = (
            db.session.query(db.func.coalesce(db.func.sum(MovimientoPT.cantidad), 0))
            .filter_by(producto_id=producto_id, ubicacion=ubicacion)
            .scalar()
        )
        return Decimal(str(total or 0))

    def reservado_pt(producto_id):
        total = (
            db.session.query(db.func.coalesce(db.func.sum(ReservaERP.cantidad), 0))
            .filter(ReservaERP.tipo == "Producto terminado", ReservaERP.producto_id == producto_id, ReservaERP.estado.in_(RESERVAS_ABIERTAS))
            .scalar()
        )
        return Decimal(str(total or 0))

    def saldo_mp(mp_id):
        total = Decimal(0)
        for mov in MovimientoMP.query.filter_by(mp_id=mp_id).all():
            proveedor = mov.recepcion.linea.orden.proveedor_nombre if mov.recepcion and mov.recepcion.linea and mov.recepcion.linea.orden else ""
            if not proveedor.startswith("DEMO - "):
                total += mov.cantidad
        for ajuste in AjusteInventario.query.filter_by(mp_id=mp_id, demo=False).all():
            total += ajuste.cantidad
        return total

    def reservado_mp(mp_id):
        total = (
            db.session.query(db.func.coalesce(db.func.sum(ReservaERP.cantidad), 0))
            .filter(ReservaERP.tipo == "Materia prima", ReservaERP.mp_id == mp_id, ReservaERP.estado.in_(RESERVAS_ABIERTAS))
            .scalar()
        )
        return Decimal(str(total or 0))

    def receta_producto(producto_id):
        return (
            Receta.query.filter_by(producto_id=producto_id)
            .filter(Receta.estado.in_(["Activo", "Borrador"]))
            .order_by((Receta.estado == "Activo").desc(), Receta.version.desc(), Receta.id.desc())
            .first()
        )

    def requerimientos_mp(producto_id, cantidad_producto):
        receta = receta_producto(producto_id)
        if not receta or not receta.rendimiento or receta.rendimiento <= 0:
            return None, [{"producto_id": producto_id, "faltante": "Receta o rendimiento no configurado"}]
        factor = cantidad_producto / receta.rendimiento
        requeridos = {}
        pendientes = []
        for linea in receta.lineas:
            if linea.mp_id:
                requeridos.setdefault(linea.mp_id, {"mp": linea.mp, "cantidad": Decimal(0), "unidad": linea.unidad})
                requeridos[linea.mp_id]["cantidad"] += (linea.cantidad * factor).quantize(Decimal(".000001"))
            else:
                pendientes.append({"ingrediente": linea.nombre_original, "faltante": "Subreceta no expandida"})
        return list(requeridos.values()), pendientes

    def liberar_reservas(pedido, estado="Liberada"):
        for reserva in ReservaERP.query.filter_by(pedido_id=pedido.id, estado="Activa").all():
            reserva.estado = estado

    def mover_producto_a_transito(pedido):
        if pedido.pago_requerido_antes_preparar and pedido.saldo > 0:
            raise ValueError("El pedido requiere pago antes de salir.")
        reservas = ReservaERP.query.filter_by(pedido_id=pedido.id, tipo="Producto terminado", estado="Activa").all()
        if not reservas:
            raise ValueError("No hay producto terminado reservado para enviar.")
        for reserva in reservas:
            if saldo_pt(reserva.producto_id, UBICACION_PT) < reserva.cantidad:
                raise ValueError("No hay producto terminado fisico suficiente en almacen.")
            base_token = f"crm-ruta-{pedido.id}-{reserva.id}"
            if not MovimientoPT.query.filter_by(token=f"{base_token}-salida").first():
                db.session.add(
                    MovimientoPT(
                        producto_id=reserva.producto_id,
                        pedido_id=pedido.id,
                        tipo="Salida a ruta",
                        cantidad=-reserva.cantidad,
                        ubicacion=UBICACION_PT,
                        referencia=pedido.folio,
                        usuario=crm_usuario_actual(),
                        token=f"{base_token}-salida",
                    )
                )
                db.session.add(
                    MovimientoPT(
                        producto_id=reserva.producto_id,
                        pedido_id=pedido.id,
                        tipo="Entrada a transito",
                        cantidad=reserva.cantidad,
                        ubicacion=UBICACION_TRANSITO,
                        referencia=pedido.folio,
                        usuario=crm_usuario_actual(),
                        token=f"{base_token}-transito",
                    )
                )

    def reservar_pedido(pedido):
        liberar_reservas(pedido)
        OrdenLlenadoCRM.query.filter_by(pedido_id=pedido.id, estado="Pendiente").update({"estado": "Cancelada"})
        faltantes = []
        requiere_llenado = False
        for linea in pedido.lineas:
            disponible_pt = saldo_pt(linea.producto_id) - reservado_pt(linea.producto_id)
            if disponible_pt >= linea.cantidad:
                db.session.add(
                    ReservaERP(
                        pedido_id=pedido.id,
                        linea_id=linea.id,
                        tipo="Producto terminado",
                        producto_id=linea.producto_id,
                        cantidad=linea.cantidad,
                        unidad="pieza",
                        usuario=crm_usuario_actual(),
                        referencia=pedido.folio,
                    )
                )
                linea.modo_reserva = "Producto terminado"
                continue
            requeridos, pendientes = requerimientos_mp(linea.producto_id, linea.cantidad)
            if pendientes:
                faltantes.extend(pendientes)
                linea.modo_reserva = "Sin reserva"
                continue
            linea_faltantes = []
            for req in requeridos:
                disponible = saldo_mp(req["mp"].id_mp) - reservado_mp(req["mp"].id_mp)
                if disponible < req["cantidad"]:
                    linea_faltantes.append(
                        {
                            "mp_id": req["mp"].id_mp,
                            "materia_prima": req["mp"].nombre_oficial,
                            "disponible": str(disponible),
                            "requerido": str(req["cantidad"]),
                            "unidad": req["mp"].unidad_base,
                        }
                    )
            if linea_faltantes:
                faltantes.extend(linea_faltantes)
                linea.modo_reserva = "Sin reserva"
                continue
            for req in requeridos:
                db.session.add(
                    ReservaERP(
                        pedido_id=pedido.id,
                        linea_id=linea.id,
                        tipo="Materia prima",
                        mp_id=req["mp"].id_mp,
                        cantidad=req["cantidad"],
                        unidad=req["mp"].unidad_base,
                        usuario=crm_usuario_actual(),
                        referencia=pedido.folio,
                    )
                )
            db.session.add(
                OrdenLlenadoCRM(
                    pedido_id=pedido.id,
                    linea_id=linea.id,
                    producto_id=linea.producto_id,
                    cantidad=linea.cantidad,
                    usuario=crm_usuario_actual(),
                )
            )
            requiere_llenado = True
            linea.modo_reserva = "Componentes reservados"
        if faltantes:
            pedido.estado = "Pendiente de produccion"
            registrar_evento(pedido, "Faltantes", "El pedido quedo pendiente de produccion por falta de inventario o receta.", {"faltantes": faltantes})
            return faltantes
        pedido.estado = "Confirmado"
        registrar_evento(pedido, "Reserva ERP", "Pedido confirmado con producto terminado reservado." if not requiere_llenado else "Pedido confirmado con componentes reservados y orden de llenado.")
        return []

    def serializar_pedido(pedido):
        return {
            "id": pedido.id,
            "folio": pedido.folio,
            "cliente": pedido.cliente.nombre,
            "telefono": pedido.cliente.telefono,
            "estado": pedido.estado,
            "canal": pedido.canal,
            "forma_pago": pedido.forma_pago,
            "importe_total": float(pedido.importe_total),
            "importe_pagado": float(pedido.importe_pagado),
            "saldo": float(pedido.saldo),
            "lineas": [
                {
                    "id": l.id,
                    "producto_id": l.producto_id,
                    "producto": l.producto.nombre if l.producto else "",
                    "cantidad": float(l.cantidad),
                    "precio_unitario": float(l.precio_unitario),
                    "modo_reserva": l.modo_reserva,
                }
                for l in pedido.lineas
            ],
        }

    @app.route("/crm/login", methods=["GET", "POST"])
    def crm_login():
        if request.method == "POST":
            usuario = request.form.get("usuario") or (request.json.get("usuario") if request.is_json else None)
            password = request.form.get("password") or (request.json.get("password") if request.is_json else None)
            cuenta = CRM_USUARIOS.get(usuario or "")
            password_esperado = os.environ.get(cuenta["password_env"], "") if cuenta else ""
            if cuenta and password_esperado and secrets.compare_digest(password_esperado, password or ""):
                session["crm_logueado"] = True
                session["crm_usuario"] = usuario
                session["crm_nombre"] = cuenta["nombre"]
                session["crm_rol"] = cuenta["rol"]
                session.permanent = True
                if request.is_json:
                    return jsonify({"exito": True, "redirect": "/crm/tablero", "rol": cuenta["rol"]})
                return redirect(url_for("crm_tablero"))
            if request.is_json:
                return jsonify({"error": "Credenciales incorrectas"}), 401
            return render_template("crm_login.html", error="Credenciales incorrectas")
        return render_template("crm_login.html")

    @app.route("/crm/logout")
    def crm_logout():
        for clave in ["crm_logueado", "crm_usuario", "crm_nombre", "crm_rol"]:
            session.pop(clave, None)
        return redirect(url_for("crm_login"))

    @app.route("/crm")
    @protegido()
    def crm_inicio():
        return redirect(url_for("crm_tablero"))

    @app.route("/admin/crm/pedidos")
    def crm_admin_redirect():
        return redirect(url_for("crm_pedidos"))

    @app.route("/admin/productos-entregados")
    @protegido_erp
    def productos_entregados_erp():
        movimientos = (
            MovimientoPT.query.filter_by(tipo="Entrega confirmada")
            .order_by(MovimientoPT.fecha.desc(), MovimientoPT.id.desc())
            .all()
        )
        entregas = []
        for movimiento in movimientos:
            pedido = db.session.get(PedidoCRM, movimiento.pedido_id) if movimiento.pedido_id else None
            evidencia = (
                EvidenciaEntregaCRM.query.filter_by(pedido_id=movimiento.pedido_id)
                .order_by(EvidenciaEntregaCRM.fecha.desc(), EvidenciaEntregaCRM.id.desc())
                .first()
                if movimiento.pedido_id
                else None
            )
            entregas.append({"movimiento": movimiento, "pedido": pedido, "evidencia": evidencia})
        return render_template("productos_entregados.html", entregas=entregas)

    @app.route("/crm/pedidos")
    @protegido()
    def crm_pedidos():
        pedidos = PedidoCRM.query.order_by(PedidoCRM.id.desc()).all()
        ordenes = OrdenLlenadoCRM.query.order_by(OrdenLlenadoCRM.id.desc()).limit(20).all()
        productos = PT.query.filter_by(estatus="Activo").order_by(PT.nombre).all()
        return render_template("crm_pedidos.html", pedidos=pedidos, ordenes=ordenes, productos=productos, rol=rol_crm_actual())

    @app.route("/api/crm/productos/<int:id_producto>/disponibilidad")
    @protegido_json("ventas", "preparacion", "cobranza")
    def crm_disponibilidad_producto(id_producto):
        producto = db.session.get(PT, id_producto)
        if not producto:
            return jsonify({"error": "Producto terminado no encontrado"}), 404
        fisico = saldo_pt(id_producto)
        reservado = reservado_pt(id_producto)
        return jsonify({"producto_id": id_producto, "fisico": float(fisico), "reservado": float(reservado), "disponible": float(fisico - reservado)})

    @app.route("/api/erp-interno/productos/<int:id_producto>/disponibilidad")
    @protegido_erp_json
    def erp_interno_disponibilidad_producto(id_producto):
        producto = db.session.get(PT, id_producto)
        if not producto:
            return jsonify({"error": "Producto terminado no encontrado"}), 404
        fisico = saldo_pt(id_producto)
        reservado = reservado_pt(id_producto)
        return jsonify({"producto_id": id_producto, "fisico": float(fisico), "reservado": float(reservado), "disponible": float(fisico - reservado)})

    @app.route("/api/crm/pedidos", methods=["GET", "POST"])
    @protegido_json()
    def api_crm_pedidos():
        if request.method == "GET":
            return jsonify([serializar_pedido(p) for p in PedidoCRM.query.order_by(PedidoCRM.id.desc()).all()])
        if not tiene_rol("ventas"):
            return jsonify({"error": "Solo ventas puede crear pedidos."}), 403
        data = request.json or {}
        try:
            token = texto(data, "token", 80) or secrets.token_hex(24)
            existente = PedidoCRM.query.filter_by(token=token).first()
            if existente:
                return jsonify(serializar_pedido(existente)), 200
            cliente_data = data.get("cliente") or {}
            domicilio_data = data.get("domicilio") or {}
            cliente = None
            if data.get("cliente_id"):
                cliente = db.session.get(ClienteCRM, data["cliente_id"])
            if not cliente:
                telefono = texto(cliente_data, "telefono", 40, True)
                cliente = ClienteCRM.query.filter_by(telefono=telefono).first()
            if not cliente:
                cliente = ClienteCRM(
                    nombre=texto(cliente_data, "nombre", 180, True),
                    telefono=texto(cliente_data, "telefono", 40, True),
                    correo=texto(cliente_data, "correo", 180),
                    canal_origen=texto(data, "canal", 40) or "Manual",
                )
                db.session.add(cliente)
                db.session.flush()
            domicilio = None
            if data.get("domicilio_id"):
                domicilio = ClienteDomicilio.query.filter_by(id=data["domicilio_id"], cliente_id=cliente.id).first()
            if not domicilio:
                domicilio = ClienteDomicilio(
                    cliente_id=cliente.id,
                    alias=texto(domicilio_data, "alias", 80) or "Principal",
                    direccion=texto(domicilio_data, "direccion", 280, True),
                    referencias=texto(domicilio_data, "referencias", 500),
                    zona=texto(domicilio_data, "zona", 100) or "Cancun",
                    cobertura=bool(domicilio_data.get("cobertura", True)),
                    lat=numero(domicilio_data["lat"], 8) if domicilio_data.get("lat") not in (None, "") else None,
                    lng=numero(domicilio_data["lng"], 8) if domicilio_data.get("lng") not in (None, "") else None,
                )
                db.session.add(domicilio)
                db.session.flush()
            pedido = PedidoCRM(
                folio=folio_pedido(),
                cliente_id=cliente.id,
                domicilio_id=domicilio.id,
                canal=texto(data, "canal", 40) or "Manual",
                forma_pago=texto(data, "forma_pago", 40) or "Contra entrega",
                pago_requerido_antes_preparar=bool(data.get("pago_requerido_antes_preparar")),
                entrega_propia=bool(data.get("entrega_propia")),
                colaborador_asignado=crm_usuario_actual() if data.get("entrega_propia") else texto(data, "colaborador_asignado", 100),
                token=token,
            )
            db.session.add(pedido)
            db.session.flush()
            total = Decimal("0")
            for item in data.get("lineas") or []:
                producto = db.session.get(PT, item.get("producto_id"))
                if not producto or producto.estatus != "Activo":
                    raise ValueError("Selecciona productos terminados activos.")
                cantidad = numero(item.get("cantidad"), positivo=True)
                precio = numero(item.get("precio_unitario"), decimales=2)
                linea = PedidoLineaCRM(pedido_id=pedido.id, producto_id=producto.id, cantidad=cantidad, precio_unitario=precio, notas=texto(item, "notas", 500))
                db.session.add(linea)
                total += cantidad * precio
            if total <= 0:
                raise ValueError("Agrega al menos una linea con importe mayor a cero.")
            pedido.importe_total = total.quantize(Decimal(".01"))
            registrar_evento(pedido, "Pedido recibido", "Pedido creado desde el CRM.", {"canal": pedido.canal})
            db.session.commit()
            return jsonify(serializar_pedido(pedido)), 201
        except (ValueError, IntegrityError) as e:
            db.session.rollback()
            return jsonify({"error": str(e) if isinstance(e, ValueError) else "No se pudo guardar el pedido."}), 400

    @app.route("/api/crm/pedidos/<int:id_pedido>/confirmar", methods=["POST"])
    @protegido_json("ventas")
    def api_crm_confirmar_pedido(id_pedido):
        pedido = db.get_or_404(PedidoCRM, id_pedido)
        if pedido.estado not in {"Recibido", "Pendiente de pago", "Pendiente de produccion", "Confirmado"}:
            return jsonify({"error": "El pedido ya avanzo y no puede reservarse de nuevo."}), 400
        if not pedido.domicilio.cobertura:
            return jsonify({"error": "El domicilio esta fuera de cobertura."}), 400
        if pedido.pago_requerido_antes_preparar and pedido.saldo > 0:
            pedido.estado = "Pendiente de pago"
            registrar_evento(pedido, "Pago pendiente", "El pedido requiere pago antes de prepararse.")
            db.session.commit()
            return jsonify(serializar_pedido(pedido)), 200
        faltantes = reservar_pedido(pedido)
        db.session.commit()
        respuesta = serializar_pedido(pedido)
        respuesta["faltantes"] = faltantes
        return jsonify(respuesta), 200

    @app.route("/api/crm/pedidos/<int:id_pedido>/pagos", methods=["POST"])
    @protegido_json("cobranza")
    def api_crm_pago_pedido(id_pedido):
        pedido = db.get_or_404(PedidoCRM, id_pedido)
        data = request.json or {}
        try:
            token = texto(data, "token", 80) or secrets.token_hex(24)
            existente = PagoPedidoCRM.query.filter_by(token=token).first()
            if existente:
                return jsonify(serializar_pedido(pedido)), 200
            monto = numero(data.get("monto"), decimales=2, positivo=True)
            if pedido.importe_pagado + monto > pedido.importe_total:
                raise ValueError("El pago supera el saldo pendiente.")
            pago = PagoPedidoCRM(
                pedido_id=pedido.id,
                monto=monto,
                metodo=texto(data, "metodo", 40, True),
                referencia=texto(data, "referencia", 160, True),
                usuario=crm_usuario_actual(),
                token=token,
            )
            pedido.importe_pagado += monto
            db.session.add(pago)
            registrar_evento(pedido, "Pago recibido", f"Pago registrado por {monto}.", {"metodo": pago.metodo})
            db.session.commit()
            return jsonify(serializar_pedido(pedido)), 201
        except (ValueError, IntegrityError) as e:
            db.session.rollback()
            return jsonify({"error": str(e) if isinstance(e, ValueError) else "No se pudo registrar el pago."}), 400

    @app.route("/api/crm/ordenes-llenado/<int:id_orden>/completar", methods=["POST"])
    @protegido_json("preparacion")
    def api_crm_completar_llenado(id_orden):
        orden = db.get_or_404(OrdenLlenadoCRM, id_orden)
        if orden.estado != "Pendiente":
            return jsonify({"error": "La orden de llenado ya fue cerrada."}), 400
        data = request.json or {}
        token = texto(data, "token", 80) or secrets.token_hex(24)
        if MovimientoPT.query.filter_by(token=token).first():
            return jsonify({"mensaje": "Movimiento ya registrado"}), 200
        pedido = db.session.get(PedidoCRM, orden.pedido_id)
        reservas = ReservaERP.query.filter_by(pedido_id=orden.pedido_id, linea_id=orden.linea_id, tipo="Materia prima", estado="Activa").all()
        try:
            for reserva in reservas:
                disponible = saldo_mp(reserva.mp_id) - reservado_mp(reserva.mp_id) + reserva.cantidad
                if disponible < reserva.cantidad:
                    raise ValueError("La materia prima reservada ya no esta disponible. Revisa inventario.")
                db.session.add(
                    AjusteInventario(
                        mp_id=reserva.mp_id,
                        tipo="Ajuste de salida",
                        cantidad=-reserva.cantidad,
                        unidad=reserva.unidad,
                        ubicacion=texto(data, "ubicacion_mp", 220) or "Produccion",
                        lote=texto(data, "lote", 100),
                        demo=False,
                        motivo=f"Consumo por llenado {pedido.folio}",
                        referencia=f"CRM-LLENADO-{orden.id}",
                        usuario=crm_usuario_actual(),
                        token=f"crm-mp-{orden.id}-{reserva.id}",
                    )
                )
                reserva.estado = "Consumida"
            db.session.add(
                MovimientoPT(
                    producto_id=orden.producto_id,
                    pedido_id=orden.pedido_id,
                    tipo="Alta por llenado",
                    cantidad=orden.cantidad,
                    ubicacion=UBICACION_PT,
                    referencia=f"CRM-LLENADO-{orden.id}",
                    usuario=crm_usuario_actual(),
                    token=token,
                )
            )
            db.session.add(
                ReservaERP(
                    pedido_id=orden.pedido_id,
                    linea_id=orden.linea_id,
                    tipo="Producto terminado",
                    producto_id=orden.producto_id,
                    cantidad=orden.cantidad,
                    unidad="pieza",
                    usuario=crm_usuario_actual(),
                    referencia=pedido.folio,
                )
            )
            orden.estado = "Completada"
            orden.fecha_cierre = datetime.utcnow()
            orden.token_cierre = token
            linea = db.session.get(PedidoLineaCRM, orden.linea_id)
            if linea:
                linea.modo_reserva = "Producto terminado"
            registrar_evento(pedido, "Llenado completado", f"Orden de llenado {orden.id} completada.")
            db.session.commit()
            return jsonify({"mensaje": "Orden completada", "pedido": serializar_pedido(pedido)}), 200
        except (ValueError, IntegrityError) as e:
            db.session.rollback()
            return jsonify({"error": str(e) if isinstance(e, ValueError) else "No se pudo cerrar la orden."}), 400

    @app.route("/api/crm/pedidos/<int:id_pedido>/estado", methods=["POST"])
    @protegido_json("ventas", "preparacion", "reparto")
    def api_crm_estado_pedido(id_pedido):
        pedido = db.get_or_404(PedidoCRM, id_pedido)
        data = request.json or {}
        nuevo_estado = texto(data, "estado", 40, True)
        if nuevo_estado not in ESTADOS_PEDIDO:
            return jsonify({"error": "Estado no permitido."}), 400
        if nuevo_estado == "Entregado":
            return jsonify({"error": "La entrega debe registrarse con evidencia desde el endpoint de entrega."}), 400
        if nuevo_estado in {"En preparacion", "Listo para entrega"} and not tiene_rol("preparacion"):
            return jsonify({"error": "Solo preparacion puede cambiar este estado."}), 403
        if nuevo_estado in {"En ruta", "Intento fallido", "Devuelto"} and not tiene_rol("reparto"):
            return jsonify({"error": "Solo reparto puede cambiar este estado."}), 403
        if nuevo_estado in {"Cancelado", "Pendiente de pago", "Confirmado"} and not tiene_rol("ventas"):
            return jsonify({"error": "Solo ventas puede cambiar este estado."}), 403
        try:
            if nuevo_estado == "En ruta":
                if pedido.estado not in {"Listo para entrega", "Confirmado"}:
                    raise ValueError("Solo un pedido confirmado o listo puede salir a ruta.")
                mover_producto_a_transito(pedido)
            if nuevo_estado in {"Cancelado", "Devuelto"}:
                liberar_reservas(pedido)
            pedido.estado = nuevo_estado
            registrar_evento(pedido, f"Estado {nuevo_estado}", texto(data, "comentario", 500) or f"Pedido cambiado a {nuevo_estado}.")
            db.session.commit()
            return jsonify(serializar_pedido(pedido)), 200
        except (ValueError, IntegrityError) as e:
            db.session.rollback()
            return jsonify({"error": str(e) if isinstance(e, ValueError) else "No se pudo cambiar el estado."}), 400

    @app.route("/api/crm/pedidos/<int:id_pedido>/entrega", methods=["POST"])
    @protegido_json("reparto")
    def api_crm_entrega_pedido(id_pedido):
        pedido = db.get_or_404(PedidoCRM, id_pedido)
        data = request.json or {}
        try:
            entregado = bool(data.get("entregado", True))
            evidencia = EvidenciaEntregaCRM(
                pedido_id=pedido.id,
                otp=texto(data, "otp", 20),
                receptor=texto(data, "receptor", 160),
                firma=texto(data, "firma", 500),
                foto=texto(data, "foto", 500),
                lat=numero(data["lat"], 8) if data.get("lat") not in (None, "") else None,
                lng=numero(data["lng"], 8) if data.get("lng") not in (None, "") else None,
                incidencia=texto(data, "incidencia", 500),
                usuario=crm_usuario_actual(),
            )
            if entregado and not any([evidencia.otp, evidencia.receptor, evidencia.firma, evidencia.foto]):
                raise ValueError("Registra al menos una evidencia de entrega.")
            if entregado:
                if pedido.estado in {"Confirmado", "Listo para entrega"}:
                    mover_producto_a_transito(pedido)
                if pedido.estado not in {"En ruta", "Confirmado", "Listo para entrega"}:
                    raise ValueError("Solo un pedido confirmado, listo o en ruta puede entregarse.")
                for reserva in ReservaERP.query.filter_by(pedido_id=pedido.id, tipo="Producto terminado", estado="Activa").all():
                    token = f"crm-entrega-{pedido.id}-{reserva.id}"
                    if not MovimientoPT.query.filter_by(token=token).first():
                        db.session.add(MovimientoPT(producto_id=reserva.producto_id, pedido_id=pedido.id, tipo="Entrega confirmada", cantidad=-reserva.cantidad, ubicacion=UBICACION_TRANSITO, referencia=pedido.folio, usuario=crm_usuario_actual(), token=token))
                    reserva.estado = "Consumida"
                pedido.estado = "Entregado"
                registrar_evento(pedido, "Entrega confirmada", "Pedido entregado con evidencia.", {"receptor": evidencia.receptor})
            else:
                pedido.estado = "Intento fallido"
                registrar_evento(pedido, "Intento fallido", evidencia.incidencia or "Entrega no concretada.")
            db.session.add(evidencia)
            db.session.commit()
            return jsonify(serializar_pedido(pedido)), 200
        except (ValueError, IntegrityError) as e:
            db.session.rollback()
            return jsonify({"error": str(e) if isinstance(e, ValueError) else "No se pudo registrar la entrega."}), 400

    @app.route("/crm/tablero")
    @protegido()
    def crm_tablero():
        ahora = datetime.utcnow()
        pedidos = PedidoCRM.query.order_by(PedidoCRM.fecha_actualizacion.desc()).all()

        ordenes_por_pedido = {}
        for orden in OrdenLlenadoCRM.query.all():
            ordenes_por_pedido.setdefault(orden.pedido_id, []).append(orden)

        salida_ruta_por_pedido = {}
        for mov in MovimientoPT.query.filter_by(tipo="Salida a ruta").order_by(MovimientoPT.fecha.asc()).all():
            if mov.pedido_id:
                salida_ruta_por_pedido[mov.pedido_id] = mov.fecha

        avatar_clase = {"VN": "av-ventas", "PN": "av-prep", "RN": "av-reparto", "CN": "av-caja"}
        definicion_columnas = [
            ("recibido", "Recibido", {"Recibido"}, "VN"),
            ("confirmado", "Confirmado", {"Confirmado", "Pendiente de pago"}, "VN"),
            ("preparacion", "En preparación", {"En preparacion", "Pendiente de produccion"}, "PN"),
            ("listo", "Listo", {"Listo para entrega"}, "PN"),
            ("ruta", "En ruta", {"En ruta"}, "RN"),
            ("entregado", "Entregado", {"Entregado", "Cancelado", "Devuelto", "Intento fallido"}, "CN"),
        ]

        def nota_para(p):
            if p.estado == "Pendiente de pago":
                return dinero(p.saldo) + " pendientes de anticipo antes de preparar", False
            if p.estado == "Pendiente de produccion":
                return "Insumos insuficientes: no se pudo reservar todo", False
            if p.estado == "Cancelado":
                return "Pedido cancelado", True
            if p.estado == "Devuelto":
                return "Pedido devuelto", True
            if p.estado == "Intento fallido":
                return "Intento de entrega fallido, reprogramar", True
            return None, False

        columnas = []
        for clave, etiqueta, estados, avatar in definicion_columnas:
            tarjetas = []
            for p in pedidos:
                if p.estado not in estados:
                    continue
                progreso = None
                if clave in {"confirmado", "preparacion"}:
                    # El llenado de componentes ocurre mientras el pedido sigue "Confirmado"
                    # (o ya renombrado a mano a "En preparacion"): no hay un tercer estado
                    # automatico para eso en el modelo de datos real.
                    ordenes = ordenes_por_pedido.get(p.id, [])
                    if ordenes:
                        completadas = sum(1 for o in ordenes if o.estado == "Completada")
                        progreso = round(completadas * 100 / len(ordenes))
                marca_tiempo = salida_ruta_por_pedido.get(p.id) if clave == "ruta" else None
                nota_texto, nota_alerta = nota_para(p)
                tarjetas.append({
                    "id": p.id,
                    "folio": p.folio,
                    "cliente": p.cliente.nombre if p.cliente else "Cliente",
                    "canal": p.canal,
                    "canal_clase": canal_clase(p.canal),
                    "total_fmt": dinero(p.importe_total),
                    "avatar": avatar,
                    "avatar_clase": avatar_clase[avatar],
                    "hace": hace_texto(marca_tiempo or p.fecha_actualizacion, ahora),
                    "progreso": progreso,
                    "nota": nota_texto,
                    "nota_alerta": nota_alerta,
                })
            columnas.append({
                "clave": clave,
                "etiqueta": etiqueta,
                "tarjetas": tarjetas,
                "cuenta": len(tarjetas),
                "total_fmt": dinero(sum(float(p.importe_total or 0) for p in pedidos if p.estado in estados)),
            })

        activos = [p for p in pedidos if p.estado not in {"Entregado", "Cancelado", "Devuelto"}]
        nuevos_semana = sum(1 for p in pedidos if p.fecha >= ahora - timedelta(days=7))
        entregas_hoy = sum(1 for p in pedidos if p.estado == "Entregado" and p.fecha_actualizacion.date() == ahora.date())
        en_ruta_ahora = sum(1 for p in pedidos if p.estado == "En ruta")
        pendientes_cobro = [p for p in activos if p.saldo > 0]

        metricas = {
            "activos": len(activos),
            "nuevos_semana": nuevos_semana,
            "valor_gestion_fmt": dinero(sum(float(p.importe_total or 0) for p in activos)),
            "entregas_hoy": entregas_hoy,
            "en_ruta_ahora": en_ruta_ahora,
            "por_cobrar_fmt": dinero(sum(float(p.saldo or 0) for p in pendientes_cobro)),
            "por_cobrar_cuenta": len(pendientes_cobro),
        }

        return render_template(
            "crm_tablero.html",
            columnas=columnas,
            metricas=metricas,
            hoy_es=fecha_larga_es(ahora),
            nombre_usuario=session.get("crm_nombre") or ("Administrador ERP" if session.get("admin_logueado") else "Equipo NYDS"),
            rol=rol_crm_actual(),
        )

    return (
        ClienteCRM,
        ClienteDomicilio,
        PedidoCRM,
        PedidoLineaCRM,
        PagoPedidoCRM,
        ReservaERP,
        OrdenLlenadoCRM,
        MovimientoPT,
        EventoCRM,
        EvidenciaEntregaCRM,
    )
