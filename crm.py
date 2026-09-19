"""CRM de pedidos y entregas conectado con inventario y producto terminado."""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
import secrets

from flask import abort, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError

if __package__:
    from .reportes_pdf import celda, generar_pdf, respuesta_pdf
else:
    from reportes_pdf import celda, generar_pdf, respuesta_pdf


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

METODOS_PAGO = ["Efectivo", "Transferencia", "Tarjeta", "Contra entrega", "Trueque"]

RESERVAS_ABIERTAS = {"Activa"}
UBICACION_PT = "Almacen PT"
UBICACION_TRANSITO = "En transito"


def registrar_crm(app, db, MP, PT, Receta, RecetaIngrediente, MovimientoMP, AjusteInventario, usuario_actual, autenticar_persona):
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

    class CategoriaTruequeCRM(db.Model):
        __tablename__ = "crm_categorias_trueque"
        id = db.Column(db.Integer, primary_key=True)
        nombre = db.Column(db.String(80), unique=True, nullable=False)
        activo = db.Column(db.Boolean, nullable=False, default=True)

    class PagoPedidoCRM(db.Model):
        __tablename__ = "crm_pedido_pagos"
        id = db.Column(db.Integer, primary_key=True)
        pedido_id = db.Column(db.Integer, db.ForeignKey("crm_pedidos.id"), nullable=False)
        pedido = db.relationship(PedidoCRM)
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        monto = db.Column(db.Numeric(18, 2), nullable=False)
        metodo = db.Column(db.String(40), nullable=False)
        referencia = db.Column(db.String(160), nullable=False)
        categoria_trueque_id = db.Column(db.Integer, db.ForeignKey("crm_categorias_trueque.id"))
        categoria_trueque = db.relationship(CategoriaTruequeCRM)
        porcentaje_trueque = db.Column(db.Numeric(5, 2))
        usuario = db.Column(db.String(100), nullable=False)
        token = db.Column(db.String(80), unique=True, nullable=False)

    class MovimientoTruequeCRM(db.Model):
        __tablename__ = "crm_trueque_movimientos"
        id = db.Column(db.Integer, primary_key=True)
        cliente_id = db.Column(db.Integer, db.ForeignKey("crm_clientes.id"), nullable=False)
        cliente = db.relationship(ClienteCRM)
        categoria_id = db.Column(db.Integer, db.ForeignKey("crm_categorias_trueque.id"), nullable=False)
        categoria = db.relationship(CategoriaTruequeCRM)
        tipo = db.Column(db.String(10), nullable=False)  # "credito" (a favor del cliente) o "consumo" (aplicado a un pago)
        monto = db.Column(db.Numeric(18, 2), nullable=False)
        pedido_id = db.Column(db.Integer, db.ForeignKey("crm_pedidos.id"))
        pago_id = db.Column(db.Integer, db.ForeignKey("crm_pedido_pagos.id"))
        descripcion = db.Column(db.String(300), nullable=False)
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
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

    def saldo_trueque_cliente(cliente_id):
        total = Decimal("0")
        for mov in MovimientoTruequeCRM.query.filter_by(cliente_id=cliente_id).all():
            total += mov.monto if mov.tipo == "credito" else -mov.monto
        return total

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
            "saldo_trueque_cliente": float(saldo_trueque_cliente(pedido.cliente_id)),
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
            persona = autenticar_persona(usuario, password)
            if persona:
                session["crm_logueado"] = True
                session["crm_usuario"] = persona.usuario
                session["crm_nombre"] = persona.nombre
                session["crm_rol"] = persona.rol
                session.permanent = True
                if request.is_json:
                    return jsonify({"exito": True, "redirect": "/crm/tablero", "rol": persona.rol})
                return redirect(url_for("crm_tablero"))
            if request.is_json:
                return jsonify({"error": "Credenciales incorrectas"}), 401
            return render_template("crm_login.html", error="Credenciales incorrectas"), 401
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

    @app.route("/admin/productos-entregados/pdf")
    @protegido_erp
    def productos_entregados_pdf():
        movimientos = (
            MovimientoPT.query.filter_by(tipo="Entrega confirmada")
            .order_by(MovimientoPT.fecha.desc(), MovimientoPT.id.desc())
            .all()
        )
        filas = []
        for movimiento in movimientos:
            pedido = db.session.get(PedidoCRM, movimiento.pedido_id) if movimiento.pedido_id else None
            filas.append([
                celda(movimiento.fecha.strftime("%d/%m/%Y %H:%M")),
                celda(movimiento.producto.nombre if movimiento.producto else "Producto eliminado"),
                celda(f"{-movimiento.cantidad:g} {movimiento.unidad}"),
                celda(pedido.folio if pedido else movimiento.referencia),
                celda(pedido.cliente.nombre if pedido and pedido.cliente else "Sin pedido vinculado", muted=not pedido),
                celda(movimiento.usuario, muted=True),
            ])
        buffer = generar_pdf(
            titulo="Productos entregados",
            subtitulo=f"Salidas definitivas confirmadas desde el CRM · {len(movimientos)} entrega(s)",
            secciones=[{
                "columnas": ["Fecha", "Producto", "Cantidad", "Pedido", "Cliente", "Usuario"],
                "filas": filas,
                "anchos": [2.6, 4, 2.4, 2.4, 3.6, 2.3],
                "vacio": "Aún no hay productos entregados.",
            }],
        )
        return respuesta_pdf(buffer, "productos-entregados.pdf")

    def _pedidos_en_ruta_por_zona():
        pedidos = (
            PedidoCRM.query.filter_by(estado="En ruta")
            .order_by(PedidoCRM.fecha_actualizacion.asc())
            .all()
        )
        salida_por_pedido = {}
        for mov in MovimientoPT.query.filter_by(tipo="Salida a ruta").order_by(MovimientoPT.fecha.asc()).all():
            if mov.pedido_id and mov.pedido_id not in salida_por_pedido:
                salida_por_pedido[mov.pedido_id] = mov.fecha
        zonas = {}
        for pedido in pedidos:
            zona = pedido.domicilio.zona if pedido.domicilio else "Sin zona"
            zonas.setdefault(zona, []).append((pedido, salida_por_pedido.get(pedido.id)))
        return pedidos, zonas

    @app.route("/crm/rutas")
    @protegido()
    def crm_rutas():
        pedidos, zonas = _pedidos_en_ruta_por_zona()
        grupos = [
            {
                "zona": zona,
                "filas": [{"pedido": pedido, "salida": salida} for pedido, salida in zonas[zona]],
            }
            for zona in sorted(zonas)
        ]
        return render_template(
            "crm_rutas.html",
            grupos=grupos,
            total=len(pedidos),
            hoy_es=fecha_larga_es(datetime.utcnow()),
            nombre_usuario=session.get("crm_nombre") or ("Administrador ERP" if session.get("admin_logueado") else "Equipo NYDS"),
            rol=rol_crm_actual(),
        )

    @app.route("/crm/rutas/pdf")
    @protegido()
    def crm_rutas_pdf():
        pedidos, zonas = _pedidos_en_ruta_por_zona()
        secciones = []
        for zona in sorted(zonas):
            filas = []
            for pedido, salida in zonas[zona]:
                filas.append([
                    celda(pedido.folio),
                    celda(pedido.cliente.nombre if pedido.cliente else "Cliente"),
                    celda(pedido.cliente.telefono if pedido.cliente else "", muted=True),
                    celda(pedido.domicilio.direccion if pedido.domicilio else "", muted=True),
                    celda(dinero(pedido.saldo)),
                    celda(salida.strftime("%d/%m %H:%M") if salida else "—", muted=True),
                ])
            secciones.append({
                "titulo": f"Zona: {zona} ({len(filas)})",
                "columnas": ["Folio", "Cliente", "Teléfono", "Dirección", "Por cobrar", "Salió"],
                "filas": filas,
                "anchos": [2.2, 3.4, 2.7, 6.2, 2.3, 2.4],
            })
        if not secciones:
            secciones = [{
                "columnas": ["Folio", "Cliente", "Teléfono", "Dirección", "Por cobrar", "Salió"],
                "filas": [],
                "vacio": "No hay pedidos en ruta en este momento.",
            }]
        buffer = generar_pdf(
            titulo="Hoja de ruta",
            subtitulo=f"Pedidos en ruta · {fecha_larga_es(datetime.utcnow())} · {len(pedidos)} pedido(s)",
            secciones=secciones,
        )
        return respuesta_pdf(buffer, "hoja-de-ruta.pdf")

    @app.route("/crm/pedidos")
    @protegido()
    def crm_pedidos():
        pedidos = PedidoCRM.query.order_by(PedidoCRM.id.desc()).all()
        ordenes = OrdenLlenadoCRM.query.order_by(OrdenLlenadoCRM.id.desc()).limit(20).all()
        productos = PT.query.filter_by(estatus="Activo").order_by(PT.nombre).all()
        saldos_trueque = {p.cliente_id: float(saldo_trueque_cliente(p.cliente_id)) for p in pedidos}
        return render_template(
            "crm_pedidos.html",
            pedidos=pedidos,
            ordenes=ordenes,
            productos=productos,
            rol=rol_crm_actual(),
            saldos_trueque=saldos_trueque,
        )

    @app.route("/crm/clientes")
    @protegido()
    def crm_clientes():
        clientes = ClienteCRM.query.order_by(ClienteCRM.nombre).all()
        filas = []
        for cliente in clientes:
            pedidos_cliente = PedidoCRM.query.filter_by(cliente_id=cliente.id).all()
            filas.append({
                "cliente": cliente,
                "pedidos": len(pedidos_cliente),
                "total_comprado": sum(float(p.importe_total or 0) for p in pedidos_cliente),
                "total_pendiente": sum(float(p.saldo or 0) for p in pedidos_cliente),
                "saldo_trueque": float(saldo_trueque_cliente(cliente.id)),
            })
        return render_template("crm_clientes.html", filas=filas, rol=rol_crm_actual())

    @app.route("/crm/pagos")
    @protegido()
    def crm_pagos():
        pagos = PagoPedidoCRM.query.order_by(PagoPedidoCRM.fecha.desc()).limit(300).all()
        total_por_metodo = {}
        for pago in PagoPedidoCRM.query.all():
            total_por_metodo.setdefault(pago.metodo, {"cantidad": 0, "total": 0.0})
            total_por_metodo[pago.metodo]["cantidad"] += 1
            total_por_metodo[pago.metodo]["total"] += float(pago.monto)
        trueque_movimientos = MovimientoTruequeCRM.query.order_by(MovimientoTruequeCRM.fecha.desc()).limit(200).all()
        return render_template(
            "crm_pagos.html",
            pagos=pagos,
            total_por_metodo=total_por_metodo,
            trueque_movimientos=trueque_movimientos,
            rol=rol_crm_actual(),
        )

    @app.route("/crm/reportes")
    @protegido()
    def crm_reportes():
        pedidos = PedidoCRM.query.all()
        por_estado = {}
        for p in pedidos:
            por_estado.setdefault(p.estado, {"cantidad": 0, "total": 0.0})
            por_estado[p.estado]["cantidad"] += 1
            por_estado[p.estado]["total"] += float(p.importe_total or 0)

        por_metodo = {}
        for pago in PagoPedidoCRM.query.all():
            por_metodo.setdefault(pago.metodo, {"cantidad": 0, "total": 0.0})
            por_metodo[pago.metodo]["cantidad"] += 1
            por_metodo[pago.metodo]["total"] += float(pago.monto)

        credito_generado = sum(float(m.monto) for m in MovimientoTruequeCRM.query.filter_by(tipo="credito").all())
        credito_consumido = sum(float(m.monto) for m in MovimientoTruequeCRM.query.filter_by(tipo="consumo").all())

        top_clientes = (
            db.session.query(ClienteCRM.nombre, db.func.coalesce(db.func.sum(PedidoCRM.importe_total), 0))
            .join(PedidoCRM, PedidoCRM.cliente_id == ClienteCRM.id)
            .group_by(ClienteCRM.id)
            .order_by(db.func.coalesce(db.func.sum(PedidoCRM.importe_total), 0).desc())
            .limit(10)
            .all()
        )

        return render_template(
            "crm_reportes.html",
            por_estado=por_estado,
            por_metodo=por_metodo,
            credito_generado=credito_generado,
            credito_consumido=credito_consumido,
            credito_vigente=credito_generado - credito_consumido,
            top_clientes=[{"nombre": nombre, "total": float(total)} for nombre, total in top_clientes],
            total_facturado=sum(float(p.importe_total or 0) for p in pedidos),
            total_cobrado=sum(float(p.importe_pagado or 0) for p in pedidos),
            total_pendiente=sum(float(p.saldo or 0) for p in pedidos),
            rol=rol_crm_actual(),
        )

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

    @app.route("/api/crm/categorias-trueque", methods=["GET", "POST"])
    @protegido_json()
    def api_crm_categorias_trueque():
        if request.method == "GET":
            categorias = CategoriaTruequeCRM.query.filter_by(activo=True).order_by(CategoriaTruequeCRM.nombre).all()
            return jsonify([{"id": c.id, "nombre": c.nombre} for c in categorias])
        if not tiene_rol("cobranza", "ventas"):
            return jsonify({"error": "Permiso insuficiente."}), 403
        data = request.json or {}
        try:
            nombre = texto(data, "nombre", 80, True)
            existente = CategoriaTruequeCRM.query.filter(db.func.lower(CategoriaTruequeCRM.nombre) == nombre.lower()).first()
            if existente:
                existente.activo = True
                db.session.commit()
                return jsonify({"id": existente.id, "nombre": existente.nombre}), 200
            categoria = CategoriaTruequeCRM(nombre=nombre)
            db.session.add(categoria)
            db.session.commit()
            return jsonify({"id": categoria.id, "nombre": categoria.nombre}), 201
        except (ValueError, IntegrityError) as e:
            db.session.rollback()
            return jsonify({"error": str(e) if isinstance(e, ValueError) else "No se pudo guardar el giro."}), 400

    @app.route("/api/crm/clientes/<int:id_cliente>/trueque", methods=["POST"])
    @protegido_json("cobranza")
    def api_crm_trueque_credito(id_cliente):
        cliente = db.get_or_404(ClienteCRM, id_cliente)
        data = request.json or {}
        try:
            token = texto(data, "token", 80) or secrets.token_hex(24)
            existente = MovimientoTruequeCRM.query.filter_by(token=token).first()
            if existente:
                return jsonify({"saldo_trueque": float(saldo_trueque_cliente(cliente.id))}), 200
            categoria = db.session.get(CategoriaTruequeCRM, data.get("categoria_id")) if data.get("categoria_id") else None
            if not categoria or not categoria.activo:
                raise ValueError("Selecciona un giro de trueque valido.")
            monto = numero(data.get("monto"), decimales=2, positivo=True)
            movimiento = MovimientoTruequeCRM(
                cliente_id=cliente.id,
                categoria_id=categoria.id,
                tipo="credito",
                monto=monto,
                descripcion=texto(data, "descripcion", 300, True),
                usuario=crm_usuario_actual(),
                token=token,
            )
            db.session.add(movimiento)
            db.session.commit()
            return jsonify({"saldo_trueque": float(saldo_trueque_cliente(cliente.id))}), 201
        except (ValueError, IntegrityError) as e:
            db.session.rollback()
            return jsonify({"error": str(e) if isinstance(e, ValueError) else "No se pudo registrar el trueque."}), 400

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
            metodo = texto(data, "metodo", 40, True)
            if metodo not in METODOS_PAGO:
                raise ValueError("Metodo de pago no valido.")
            categoria_trueque = None
            porcentaje_trueque = None
            monto_trueque = None
            if metodo == "Trueque":
                categoria_trueque = db.session.get(CategoriaTruequeCRM, data.get("categoria_trueque_id")) if data.get("categoria_trueque_id") else None
                if not categoria_trueque or not categoria_trueque.activo:
                    raise ValueError("Selecciona el giro del intercambio.")
                porcentaje_trueque = numero(data.get("porcentaje_trueque"), decimales=2, positivo=True)
                if porcentaje_trueque > 100:
                    raise ValueError("El porcentaje de trueque no puede superar 100.")
                monto_trueque = (monto * porcentaje_trueque / Decimal("100")).quantize(Decimal(".01"))
                disponible = saldo_trueque_cliente(pedido.cliente_id)
                if monto_trueque > disponible:
                    raise ValueError(f"El cliente solo tiene ${disponible:.2f} de saldo a favor por trueque.")
            pago = PagoPedidoCRM(
                pedido_id=pedido.id,
                monto=monto,
                metodo=metodo,
                referencia=texto(data, "referencia", 160, True),
                categoria_trueque_id=categoria_trueque.id if categoria_trueque else None,
                porcentaje_trueque=porcentaje_trueque,
                usuario=crm_usuario_actual(),
                token=token,
            )
            pedido.importe_pagado += monto
            db.session.add(pago)
            db.session.flush()
            if metodo == "Trueque":
                db.session.add(
                    MovimientoTruequeCRM(
                        cliente_id=pedido.cliente_id,
                        categoria_id=categoria_trueque.id,
                        tipo="consumo",
                        monto=monto_trueque,
                        pedido_id=pedido.id,
                        pago_id=pago.id,
                        descripcion=f"Consumo de trueque ({categoria_trueque.nombre}) en pago de {pedido.folio}",
                        usuario=crm_usuario_actual(),
                        token=f"{token}-consumo",
                    )
                )
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
