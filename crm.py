"""CRM de pedidos y entregas conectado con inventario y producto terminado."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
import os
import re
import secrets
import unicodedata

from flask import abort, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

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


def dinero2(valor):
    return "${:,.2f}".format(float(valor or 0))


def rango_mes(valor=None):
    """'2026-10' -> (1 oct 2026, 1 nov 2026). Sin valor o invalido: mes actual."""
    hoy = datetime.utcnow()
    try:
        anio, mes = (int(x) for x in str(valor).split("-"))
        inicio = datetime(anio, mes, 1)
    except (TypeError, ValueError):
        inicio = datetime(hoy.year, hoy.month, 1)
    fin = datetime(inicio.year + (inicio.month == 12), inicio.month % 12 + 1, 1)
    return inicio, fin


def nombre_mes(inicio):
    return f"{MESES_ES[inicio.month]} {inicio.year}"


def meses_atras(n, desde=None):
    """Primer dia de los ultimos n meses, del mas reciente al mas antiguo."""
    actual = desde or datetime.utcnow()
    anio, mes = actual.year, actual.month
    meses = []
    for _ in range(n):
        meses.append(datetime(anio, mes, 1))
        anio, mes = (anio - 1, 12) if mes == 1 else (anio, mes - 1)
    return meses


def contexto_mes(valor=None):
    """Mes elegido en pantalla (?mes=AAAA-MM) mas las opciones del selector."""
    inicio, fin = rango_mes(valor)
    opciones = meses_atras(12)
    if inicio not in opciones:
        opciones.append(inicio)
    return {
        "inicio": inicio,
        "fin": fin,
        "valor": inicio.strftime("%Y-%m"),
        "etiqueta": nombre_mes(inicio),
        "es_actual": inicio == opciones[0],
        "opciones": [{"valor": m.strftime("%Y-%m"), "etiqueta": nombre_mes(m).capitalize()} for m in opciones],
    }


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
# Pedidos que todavia pueden programarse en una ruta (aun no salen ni estan cerrados).
ESTADOS_POR_RUTEAR = {"Confirmado", "Listo para entrega"}

RESERVAS_ABIERTAS = {"Activa"}
UBICACION_PT = "Almacen PT"
UBICACION_TRANSITO = "En transito"
TIPOS_DOCUMENTO_PEDIDO = ["Recibido firmado", "Factura", "CXC / Comprobante de cobro", "Otro"]
ESTADOS_SIN_VENTA = {"Cancelado", "Devuelto"}
RFC_CLIENTE_REGEX = re.compile(r"^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$")


def normalizar_busqueda(valor):
    """Minusculas, sin acentos y sin espacios repetidos, para comparar al buscar."""
    valor = unicodedata.normalize("NFKD", str(valor or "").lower())
    valor = "".join(c for c in valor if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", valor).strip()


def solo_digitos(valor):
    return re.sub(r"\D", "", str(valor or ""))


def cliente_coincide(cliente, consulta):
    """Cada palabra de la busqueda debe aparecer en nombre, apellidos, RFC,
    correo, telefono o alguna direccion del cliente. Asi "juan perez" o
    "perez 998" encuentran al cliente aunque los datos esten en campos
    distintos. Los numeros se comparan solo por digitos, para que
    "998 123 4567" y "9981234567" coincidan."""
    palabras = normalizar_busqueda(consulta).split()
    if not palabras:
        return True
    partes = [cliente.nombre, cliente.apellidos, cliente.rfc, cliente.correo, cliente.telefono]
    for domicilio in cliente.domicilios:
        partes += [domicilio.direccion, domicilio.referencias, domicilio.zona]
    texto_cliente = normalizar_busqueda(" ".join(p for p in partes if p))
    telefono = solo_digitos(cliente.telefono)
    for palabra in palabras:
        if palabra in texto_cliente:
            continue
        digitos = solo_digitos(palabra)
        if digitos and re.fullmatch(r"[\d().+-]{3,}", palabra) and digitos in telefono:
            continue
        return False
    return True


def registrar_crm(app, db, MP, PT, Receta, RecetaIngrediente, MovimientoMP, AjusteInventario, usuario_actual, autenticar_persona, UPLOAD_FOLDER, allowed_file):
    class ClienteCRM(db.Model):
        __tablename__ = "crm_clientes"
        id = db.Column(db.Integer, primary_key=True)
        nombre = db.Column(db.String(180), nullable=False)
        apellidos = db.Column(db.String(180))
        rfc = db.Column(db.String(13), index=True)
        telefono = db.Column(db.String(40), nullable=False, default="", index=True)
        correo = db.Column(db.String(180))
        canal_origen = db.Column(db.String(40), nullable=False, default="Manual")
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        password_hash = db.Column(db.String(255))
        domicilios = db.relationship("ClienteDomicilio", backref="cliente", lazy=True, cascade="all, delete-orphan")

        @property
        def nombre_completo(self):
            return " ".join(p for p in (self.nombre, self.apellidos) if p)

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
        # Ruta en la que va el pedido. Si la ruta de su zona ya paso ese dia,
        # solo entra como urgente y con cargo extra (sumado a importe_total).
        fecha_ruta = db.Column(db.Date)
        urgente = db.Column(db.Boolean, default=False)
        cargo_urgente = db.Column(db.Numeric(18, 2), default=0)
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
        pedido = db.relationship(PedidoCRM)
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
        motivo = db.Column(db.String(500))
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

    class PedidoDocumentoCRM(db.Model):
        __tablename__ = "crm_pedido_documentos"
        id = db.Column(db.Integer, primary_key=True)
        pedido_id = db.Column(db.Integer, db.ForeignKey("crm_pedidos.id"), nullable=False, index=True)
        tipo_documento = db.Column(db.String(60), nullable=False)
        nombre_original = db.Column(db.String(255), nullable=False)
        nombre_archivo_guardado = db.Column(db.String(255), nullable=False)
        tamano_bytes = db.Column(db.Integer)
        usuario = db.Column(db.String(100), nullable=False)
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        activo = db.Column(db.Boolean, default=True, nullable=False)

    def crm_usuario_actual():
        if session.get("crm_logueado"):
            return session.get("crm_usuario", "crm.nyds")
        if session.get("cliente_id"):
            return f"cliente:{session.get('cliente_telefono') or session.get('cliente_id')}"
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

    def resumen_trueque_por_negocio(inicio, fin):
        """Por cada negocio (cliente) con trueque: saldo con el que empezo el
        mes, lo que se le abono a favor en el mes, lo que uso en el mes y el
        saldo con el que cierra."""
        cero = Decimal("0")
        filas = {}
        movimientos = (
            MovimientoTruequeCRM.query.filter(MovimientoTruequeCRM.fecha < fin)
            .order_by(MovimientoTruequeCRM.fecha)
            .all()
        )
        for mov in movimientos:
            fila = filas.setdefault(mov.cliente_id, {
                "cliente": mov.cliente,
                "giros": set(),
                "saldo_inicial": cero,
                "a_favor": cero,
                "usado": cero,
            })
            if mov.categoria:
                fila["giros"].add(mov.categoria.nombre)
            if mov.fecha < inicio:
                fila["saldo_inicial"] += mov.monto if mov.tipo == "credito" else -mov.monto
            elif mov.tipo == "credito":
                fila["a_favor"] += mov.monto
            else:
                fila["usado"] += mov.monto
        resultado = []
        for fila in filas.values():
            fila["saldo"] = fila["saldo_inicial"] + fila["a_favor"] - fila["usado"]
            fila["giros"] = ", ".join(sorted(fila["giros"]))
            if fila["saldo"] or fila["a_favor"] or fila["usado"]:
                resultado.append(fila)
        resultado.sort(key=lambda f: (f["cliente"].nombre_completo if f["cliente"] else "").lower())
        totales = {clave: sum((f[clave] for f in resultado), cero) for clave in ("saldo_inicial", "a_favor", "usado", "saldo")}
        return resultado, totales

    def fechas_entrega():
        """pedido_id -> fecha en que se confirmo la entrega."""
        fechas = {}
        for mov in MovimientoPT.query.filter_by(tipo="Entrega confirmada").all():
            if mov.pedido_id and (mov.pedido_id not in fechas or mov.fecha < fechas[mov.pedido_id]):
                fechas[mov.pedido_id] = mov.fecha
        return fechas

    def zonas_con_ruta_hoy():
        """Zonas por las que ya salio la ruta hoy (algun pedido de esa zona ya salio a ruta)."""
        hoy = datetime.utcnow()
        inicio = datetime(hoy.year, hoy.month, hoy.day)
        zonas = set()
        for mov in MovimientoPT.query.filter(MovimientoPT.tipo == "Salida a ruta", MovimientoPT.fecha >= inicio).all():
            pedido = db.session.get(PedidoCRM, mov.pedido_id) if mov.pedido_id else None
            if pedido and pedido.domicilio:
                zonas.add(pedido.domicilio.zona)
        return zonas

    def pedidos_del_mes(inicio, fin, incluir_cancelados=False):
        consulta = PedidoCRM.query.filter(PedidoCRM.fecha >= inicio, PedidoCRM.fecha < fin)
        pedidos = consulta.order_by(PedidoCRM.fecha.desc(), PedidoCRM.id.desc()).all()
        if incluir_cancelados:
            return pedidos
        return [p for p in pedidos if p.estado not in ESTADOS_SIN_VENTA]

    def pagos_del_mes(inicio, fin):
        return (
            PagoPedidoCRM.query.filter(PagoPedidoCRM.fecha >= inicio, PagoPedidoCRM.fecha < fin)
            .order_by(PagoPedidoCRM.fecha.desc(), PagoPedidoCRM.id.desc())
            .all()
        )

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
            "fecha_ruta": pedido.fecha_ruta.isoformat() if pedido.fecha_ruta else None,
            "urgente": bool(pedido.urgente),
            "cargo_urgente": float(pedido.cargo_urgente or 0),
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

    def _pedidos_por_programar():
        """Pedidos listos para salir que aun no estan en ruta, separados en:
        por asignar (sin ruta), aceptados para hoy y programados a futuro."""
        hoy = datetime.utcnow().date()
        zonas_hoy = zonas_con_ruta_hoy()
        por_asignar, para_hoy, programados = [], [], []
        pedidos = (
            PedidoCRM.query.filter(PedidoCRM.estado.in_(ESTADOS_POR_RUTEAR))
            .order_by(PedidoCRM.fecha.asc())
            .all()
        )
        for pedido in pedidos:
            zona = pedido.domicilio.zona if pedido.domicilio else "Sin zona"
            fila = {"pedido": pedido, "zona": zona, "ruta_paso": zona in zonas_hoy}
            if not pedido.fecha_ruta:
                por_asignar.append(fila)
            elif pedido.fecha_ruta <= hoy:
                para_hoy.append(fila)
            else:
                programados.append(fila)
        programados.sort(key=lambda f: f["pedido"].fecha_ruta)
        return por_asignar, para_hoy, programados

    def _entregas_del_mes(inicio, fin):
        entregas = []
        for pedido_id, fecha in fechas_entrega().items():
            if inicio <= fecha < fin:
                pedido = db.session.get(PedidoCRM, pedido_id)
                if pedido:
                    entregas.append({"pedido": pedido, "fecha": fecha})
        entregas.sort(key=lambda e: e["fecha"], reverse=True)
        return entregas

    @app.route("/crm/rutas")
    @protegido()
    def crm_rutas():
        mes = contexto_mes(request.args.get("mes"))
        pedidos, zonas = _pedidos_en_ruta_por_zona()
        grupos = [
            {
                "zona": zona,
                "filas": [{"pedido": pedido, "salida": salida} for pedido, salida in zonas[zona]],
            }
            for zona in sorted(zonas)
        ]
        por_asignar, para_hoy, programados = _pedidos_por_programar()
        return render_template(
            "crm_rutas.html",
            grupos=grupos,
            total=len(pedidos),
            por_asignar=por_asignar,
            para_hoy=para_hoy,
            programados=programados,
            entregas=_entregas_del_mes(mes["inicio"], mes["fin"]),
            mes=mes,
            hoy=datetime.utcnow().date(),
            manana=(datetime.utcnow().date() + timedelta(days=1)).isoformat(),
            hoy_es=fecha_larga_es(datetime.utcnow()),
            nombre_usuario=session.get("crm_nombre") or ("Administrador ERP" if session.get("admin_logueado") else "Equipo NYDS"),
            rol=rol_crm_actual(),
        )

    @app.route("/api/crm/pedidos/<int:id_pedido>/ruta", methods=["POST"])
    @protegido_json("ventas", "reparto")
    def api_crm_programar_ruta(id_pedido):
        """accion='hoy': entra en la ruta de hoy. Si la ruta de su zona ya
        paso (o se pide urgente), va como urgente y exige un cargo extra que
        se suma al total del pedido. accion='siguiente': se programa para una
        ruta posterior (por omision manana) y se quita cualquier cargo urgente."""
        pedido = db.get_or_404(PedidoCRM, id_pedido)
        data = request.json or {}
        try:
            if pedido.estado not in ESTADOS_POR_RUTEAR:
                raise ValueError("Solo un pedido confirmado o listo, que aun no sale, puede programarse en ruta.")
            accion = texto(data, "accion", 20, True)
            hoy = datetime.utcnow().date()
            cargo_anterior = pedido.cargo_urgente or Decimal("0")
            if accion == "hoy":
                zona = pedido.domicilio.zona if pedido.domicilio else ""
                urgente = bool(data.get("urgente")) or zona in zonas_con_ruta_hoy()
                cargo = Decimal("0")
                if urgente:
                    if data.get("cargo_urgente") in (None, ""):
                        raise ValueError("La ruta de esta zona ya paso: captura el cargo por entrega urgente.")
                    cargo = numero(data.get("cargo_urgente"), decimales=2, positivo=True)
                nuevo_total = pedido.importe_total - cargo_anterior + cargo
                if nuevo_total < pedido.importe_pagado:
                    raise ValueError("El nuevo total quedaria por debajo de lo ya pagado.")
                pedido.importe_total = nuevo_total
                pedido.urgente = urgente
                pedido.cargo_urgente = cargo
                pedido.fecha_ruta = hoy
                if urgente:
                    registrar_evento(pedido, "Ruta urgente", f"Aceptado en la ruta de hoy como urgente con cargo de {cargo}.", {"cargo_urgente": str(cargo)})
                else:
                    registrar_evento(pedido, "Ruta asignada", "Aceptado en la ruta de hoy.")
            elif accion == "siguiente":
                fecha_txt = texto(data, "fecha", 10)
                try:
                    fecha = datetime.strptime(fecha_txt, "%Y-%m-%d").date() if fecha_txt else hoy + timedelta(days=1)
                except ValueError:
                    raise ValueError("Fecha de ruta no valida.")
                if fecha <= hoy:
                    raise ValueError("La siguiente ruta debe ser a partir de manana.")
                if pedido.importe_total - cargo_anterior < pedido.importe_pagado:
                    raise ValueError("No se puede quitar el cargo urgente: ya esta pagado.")
                pedido.importe_total -= cargo_anterior
                pedido.urgente = False
                pedido.cargo_urgente = Decimal("0")
                pedido.fecha_ruta = fecha
                registrar_evento(pedido, "Ruta programada", f"Programado para la ruta del {fecha.strftime('%d/%m/%Y')}.")
            else:
                raise ValueError("Accion de ruta no valida.")
            db.session.commit()
            return jsonify(serializar_pedido(pedido)), 200
        except (ValueError, IntegrityError) as e:
            db.session.rollback()
            return jsonify({"error": str(e) if isinstance(e, ValueError) else "No se pudo programar la ruta."}), 400

    @app.route("/crm/rutas/historial/pdf")
    @protegido()
    def crm_rutas_historial_pdf():
        mes = contexto_mes(request.args.get("mes"))
        entregas = _entregas_del_mes(mes["inicio"], mes["fin"])
        filas = []
        for e in entregas:
            p = e["pedido"]
            filas.append([
                celda(e["fecha"].strftime("%d/%m/%Y %H:%M")),
                celda(p.folio),
                celda(p.cliente.nombre_completo if p.cliente else ""),
                celda(p.domicilio.zona if p.domicilio else "", muted=True),
                celda(f"Urgente +{dinero2(p.cargo_urgente)}" if p.urgente else "Normal", muted=not p.urgente),
                celda(dinero2(p.importe_total)),
            ])
        urgentes = [e for e in entregas if e["pedido"].urgente]
        buffer = generar_pdf(
            titulo="Historial de entregas",
            subtitulo=(
                f"{mes['etiqueta'].capitalize()} · {len(entregas)} entrega(s) · "
                f"{len(urgentes)} urgente(s) por {dinero2(sum((e['pedido'].cargo_urgente or 0 for e in urgentes), Decimal('0')))} en cargos"
            ),
            secciones=[{
                "columnas": ["Entregado", "Folio", "Cliente", "Zona", "Tipo", "Total"],
                "filas": filas,
                "anchos": [2.8, 2.3, 4.6, 2.6, 3.3, 2.8],
                "vacio": "No hubo entregas en este mes.",
            }],
        )
        return respuesta_pdf(buffer, f"entregas-{mes['valor']}.pdf")

    @app.route("/crm/rutas/pdf")
    @protegido()
    def crm_rutas_pdf():
        pedidos, zonas = _pedidos_en_ruta_por_zona()
        secciones = []
        for zona in sorted(zonas):
            filas = []
            for pedido, salida in zonas[zona]:
                filas.append([
                    celda(f"{pedido.folio}\nURGENTE" if pedido.urgente else pedido.folio, negrita=bool(pedido.urgente)),
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

    def serializar_cliente(cliente):
        return {
            "id": cliente.id,
            "nombre": cliente.nombre,
            "apellidos": cliente.apellidos or "",
            "nombre_completo": cliente.nombre_completo,
            "rfc": cliente.rfc or "",
            "telefono": cliente.telefono,
            "correo": cliente.correo or "",
            "domicilios": [
                {"id": d.id, "alias": d.alias, "direccion": d.direccion, "referencias": d.referencias or "", "zona": d.zona}
                for d in cliente.domicilios
            ],
        }

    def buscar_clientes(consulta):
        clientes = ClienteCRM.query.order_by(ClienteCRM.nombre, ClienteCRM.apellidos).all()
        return [c for c in clientes if cliente_coincide(c, consulta)]

    @app.route("/api/crm/clientes")
    @protegido_json()
    def api_crm_clientes():
        consulta = request.args.get("q", "").strip()
        if len(consulta) < 2:
            return jsonify([])
        return jsonify([serializar_cliente(c) for c in buscar_clientes(consulta)[:20]])

    @app.route("/api/crm/clientes", methods=["POST"])
    @protegido_json("ventas", "cobranza")
    def api_crm_cliente_alta():
        """Alta manual de cliente desde el directorio. Solo el nombre es
        obligatorio; el domicilio se crea solo si se captura una direccion."""
        data = request.json or {}
        try:
            nombre = texto(data, "nombre", 180, True)
            telefono = texto(data, "telefono", 40)
            if telefono and ClienteCRM.query.filter_by(telefono=telefono).first():
                raise ValueError("Ya existe un cliente con ese telefono.")
            rfc = texto(data, "rfc", 13).upper()
            if rfc and not RFC_CLIENTE_REGEX.match(rfc):
                raise ValueError("El RFC del cliente no tiene un formato valido.")
            cliente = ClienteCRM(
                nombre=nombre,
                apellidos=texto(data, "apellidos", 180) or None,
                rfc=rfc or None,
                telefono=telefono,
                correo=texto(data, "correo", 180),
                canal_origen=texto(data, "canal_origen", 40) or "Manual",
            )
            db.session.add(cliente)
            db.session.flush()
            direccion = texto(data, "direccion", 280)
            if direccion:
                db.session.add(ClienteDomicilio(
                    cliente_id=cliente.id,
                    alias="Principal",
                    direccion=direccion,
                    referencias=texto(data, "referencias", 500),
                    zona=texto(data, "zona", 100) or "Cancun",
                    cobertura=True,
                ))
            db.session.commit()
            return jsonify(serializar_cliente(cliente)), 201
        except (ValueError, IntegrityError) as e:
            db.session.rollback()
            return jsonify({"error": str(e) if isinstance(e, ValueError) else "No se pudo guardar el cliente."}), 400

    def filas_clientes(consulta, inicio, fin):
        filas = []
        for cliente in buscar_clientes(consulta):
            pedidos = [p for p in PedidoCRM.query.filter_by(cliente_id=cliente.id).all() if p.estado not in ESTADOS_SIN_VENTA]
            del_mes = [p for p in pedidos if inicio <= p.fecha < fin]
            filas.append({
                "cliente": cliente,
                "pedidos": len(pedidos),
                "pedidos_mes": len(del_mes),
                "comprado_mes": float(sum((p.importe_total for p in del_mes), Decimal("0"))),
                "total_comprado": float(sum((p.importe_total for p in pedidos), Decimal("0"))),
                "total_pendiente": float(sum((p.saldo for p in pedidos if p.saldo > 0), Decimal("0"))),
                "saldo_trueque": float(saldo_trueque_cliente(cliente.id)),
            })
        return filas

    @app.route("/crm/clientes")
    @protegido()
    def crm_clientes():
        consulta = request.args.get("q", "").strip()
        mes = contexto_mes(request.args.get("mes"))
        filas = filas_clientes(consulta, mes["inicio"], mes["fin"])
        return render_template("crm_clientes.html", filas=filas, q=consulta, mes=mes, rol=rol_crm_actual())

    @app.route("/crm/clientes/pdf")
    @protegido()
    def crm_clientes_pdf():
        mes = contexto_mes(request.args.get("mes"))
        filas = filas_clientes(request.args.get("q", "").strip(), mes["inicio"], mes["fin"])
        buffer = generar_pdf(
            titulo="Directorio de clientes",
            subtitulo=f"Compras de {mes['etiqueta']} e histórico acumulado · {len(filas)} cliente(s)",
            secciones=[{
                "columnas": ["Cliente", "Teléfono", "Pedidos mes", "Comprado mes", "Histórico", "Por cobrar", "Trueque"],
                "filas": [
                    [
                        celda(f["cliente"].nombre_completo),
                        celda(f["cliente"].telefono or "—", muted=True),
                        celda(f["pedidos_mes"]),
                        celda(dinero2(f["comprado_mes"])),
                        celda(dinero2(f["total_comprado"])),
                        celda(dinero2(f["total_pendiente"])),
                        celda(dinero2(f["saldo_trueque"])),
                    ]
                    for f in filas
                ],
                "anchos": [4.4, 2.6, 1.7, 2.4, 2.5, 2.4, 2.4],
                "vacio": "No hay clientes registrados.",
            }],
        )
        return respuesta_pdf(buffer, f"clientes-{mes['valor']}.pdf")

    @app.route("/crm/clientes/<int:id_cliente>/pdf")
    @protegido()
    def crm_cliente_historial_pdf(id_cliente):
        """Historial completo de un cliente: pedidos, pagos y trueque, mes por mes."""
        cliente = db.get_or_404(ClienteCRM, id_cliente)
        pedidos = PedidoCRM.query.filter_by(cliente_id=cliente.id).order_by(PedidoCRM.fecha.desc()).all()
        ids = [p.id for p in pedidos]
        pagos = (
            PagoPedidoCRM.query.filter(PagoPedidoCRM.pedido_id.in_(ids)).order_by(PagoPedidoCRM.fecha.desc()).all()
            if ids else []
        )
        trueques = MovimientoTruequeCRM.query.filter_by(cliente_id=cliente.id).order_by(MovimientoTruequeCRM.fecha.desc()).all()

        por_mes = {}
        for p in pedidos:
            if p.estado in ESTADOS_SIN_VENTA:
                continue
            fila = por_mes.setdefault(p.fecha.strftime("%Y-%m"), {"pedidos": 0, "comprado": Decimal("0"), "pagado": Decimal("0")})
            fila["pedidos"] += 1
            fila["comprado"] += p.importe_total
        for pago in pagos:
            fila = por_mes.setdefault(pago.fecha.strftime("%Y-%m"), {"pedidos": 0, "comprado": Decimal("0"), "pagado": Decimal("0")})
            fila["pagado"] += pago.monto

        datos = [cliente.telefono, cliente.correo, cliente.rfc]
        domicilios = "; ".join(d.direccion for d in cliente.domicilios)
        buffer = generar_pdf(
            titulo=f"Historial de {cliente.nombre_completo}",
            subtitulo=" · ".join(d for d in datos + [domicilios] if d) or "Sin datos de contacto",
            secciones=[
                {
                    "titulo": "Resumen por mes",
                    "columnas": ["Mes", "Pedidos", "Comprado", "Pagado"],
                    "filas": [
                        [celda(nombre_mes(rango_mes(clave)[0]).capitalize()), celda(f["pedidos"]), celda(dinero2(f["comprado"])), celda(dinero2(f["pagado"]))]
                        for clave, f in sorted(por_mes.items(), reverse=True)
                    ],
                    "anchos": [5.5, 3, 5, 4.9],
                    "vacio": "Sin movimientos.",
                },
                {
                    "titulo": "Pedidos",
                    "columnas": ["Fecha", "Folio", "Estado", "Productos", "Total", "Saldo"],
                    "filas": [
                        [
                            celda(p.fecha.strftime("%d/%m/%Y")),
                            celda(p.folio),
                            celda(p.estado + (" · urgente" if p.urgente else ""), muted=True),
                            celda("\n".join(f"{float(l.cantidad):g} {l.producto.nombre if l.producto else 'Producto'}" for l in p.lineas)),
                            celda(dinero2(p.importe_total)),
                            celda(dinero2(p.saldo)),
                        ]
                        for p in pedidos
                    ],
                    "anchos": [2.2, 2.4, 3, 5.6, 2.6, 2.6],
                    "vacio": "Sin pedidos.",
                },
                {
                    "titulo": "Pagos",
                    "columnas": ["Fecha", "Pedido", "Método", "Referencia", "Monto"],
                    "filas": [
                        [celda(pg.fecha.strftime("%d/%m/%Y")), celda(pg.pedido.folio if pg.pedido else "—"), celda(pg.metodo), celda(pg.referencia, muted=True), celda(dinero2(pg.monto))]
                        for pg in pagos
                    ],
                    "anchos": [2.4, 2.6, 3, 7.4, 3],
                    "vacio": "Sin pagos.",
                },
                {
                    "titulo": f"Trueque · saldo actual {dinero2(saldo_trueque_cliente(cliente.id))}",
                    "columnas": ["Fecha", "Giro", "Movimiento", "Descripción", "Monto"],
                    "filas": [
                        [
                            celda(m.fecha.strftime("%d/%m/%Y")),
                            celda(m.categoria.nombre if m.categoria else "—"),
                            celda("A favor" if m.tipo == "credito" else "Usado"),
                            celda(m.descripcion, muted=True),
                            celda(dinero2(m.monto)),
                        ]
                        for m in trueques
                    ],
                    "anchos": [2.4, 3, 2.4, 7.6, 3],
                    "vacio": "Sin movimientos de trueque.",
                },
            ],
        )
        return respuesta_pdf(buffer, f"cliente-{cliente.id}-historial.pdf")

    def datos_pagos(inicio, fin):
        pagos = pagos_del_mes(inicio, fin)
        total_por_metodo = {}
        for pago in pagos:
            total_por_metodo.setdefault(pago.metodo, {"cantidad": 0, "total": 0.0})
            total_por_metodo[pago.metodo]["cantidad"] += 1
            total_por_metodo[pago.metodo]["total"] += float(pago.monto)
        trueque_movimientos = (
            MovimientoTruequeCRM.query.filter(MovimientoTruequeCRM.fecha >= inicio, MovimientoTruequeCRM.fecha < fin)
            .order_by(MovimientoTruequeCRM.fecha.desc())
            .all()
        )
        trueque_negocios, trueque_totales = resumen_trueque_por_negocio(inicio, fin)
        return {
            "pagos": pagos,
            "total_por_metodo": total_por_metodo,
            "total_cobrado": sum(float(p.monto) for p in pagos),
            "trueque_movimientos": trueque_movimientos,
            "trueque_negocios": trueque_negocios,
            "trueque_totales": trueque_totales,
        }

    @app.route("/crm/pagos")
    @protegido()
    def crm_pagos():
        mes = contexto_mes(request.args.get("mes"))
        return render_template("crm_pagos.html", mes=mes, rol=rol_crm_actual(), **datos_pagos(mes["inicio"], mes["fin"]))

    @app.route("/crm/pagos/pdf")
    @protegido()
    def crm_pagos_pdf():
        mes = contexto_mes(request.args.get("mes"))
        d = datos_pagos(mes["inicio"], mes["fin"])
        t = d["trueque_totales"]
        buffer = generar_pdf(
            titulo="Pagos y trueque",
            subtitulo=f"{mes['etiqueta'].capitalize()} · {len(d['pagos'])} pago(s) · cobrado {dinero2(d['total_cobrado'])}",
            secciones=[
                {
                    "titulo": "Cobrado por método",
                    "columnas": ["Método", "Pagos", "Total"],
                    "filas": [[celda(m), celda(v["cantidad"]), celda(dinero2(v["total"]))] for m, v in d["total_por_metodo"].items()],
                    "anchos": [8, 4, 6.4],
                    "vacio": "Sin pagos en el mes.",
                },
                {
                    "titulo": "Pagos del mes",
                    "columnas": ["Fecha", "Pedido", "Cliente", "Método", "Referencia", "Monto"],
                    "filas": [
                        [
                            celda(p.fecha.strftime("%d/%m/%Y %H:%M")),
                            celda(p.pedido.folio if p.pedido else "—"),
                            celda(p.pedido.cliente.nombre_completo if p.pedido and p.pedido.cliente else "—"),
                            celda(p.metodo + (f" · {p.categoria_trueque.nombre} {p.porcentaje_trueque}%" if p.metodo == "Trueque" and p.categoria_trueque else "")),
                            celda(p.referencia, muted=True),
                            celda(dinero2(p.monto)),
                        ]
                        for p in d["pagos"]
                    ],
                    "anchos": [2.7, 2.2, 3.6, 3.2, 4.1, 2.6],
                    "vacio": "Sin pagos en el mes.",
                },
                {
                    "titulo": "Trueque por negocio",
                    "columnas": ["Negocio", "Giro", "Saldo inicial", "A favor", "Usado", "Saldo"],
                    "filas": [
                        [
                            celda(f["cliente"].nombre_completo if f["cliente"] else "—"),
                            celda(f["giros"] or "—", muted=True),
                            celda(dinero2(f["saldo_inicial"])),
                            celda(dinero2(f["a_favor"])),
                            celda(dinero2(f["usado"])),
                            celda(dinero2(f["saldo"]), negrita=True),
                        ]
                        for f in d["trueque_negocios"]
                    ] + ([[
                        celda("Total", negrita=True), celda(""),
                        celda(dinero2(t["saldo_inicial"]), negrita=True), celda(dinero2(t["a_favor"]), negrita=True),
                        celda(dinero2(t["usado"]), negrita=True), celda(dinero2(t["saldo"]), negrita=True),
                    ]] if d["trueque_negocios"] else []),
                    "anchos": [4.2, 3.2, 2.8, 2.7, 2.7, 2.8],
                    "vacio": "Sin trueque en el mes.",
                },
                {
                    "titulo": "Movimientos de trueque",
                    "columnas": ["Fecha", "Negocio", "Giro", "Tipo", "Pedido", "Monto"],
                    "filas": [
                        [
                            celda(m.fecha.strftime("%d/%m/%Y")),
                            celda(m.cliente.nombre_completo if m.cliente else "—"),
                            celda(m.categoria.nombre if m.categoria else "—", muted=True),
                            celda("A favor" if m.tipo == "credito" else "Usado"),
                            celda(m.pedido.folio if m.pedido_id and m.pedido else "—", muted=True),
                            celda(dinero2(m.monto)),
                        ]
                        for m in d["trueque_movimientos"]
                    ],
                    "anchos": [2.4, 4.4, 3.2, 2.4, 3, 3],
                    "vacio": "Sin movimientos de trueque en el mes.",
                },
            ],
        )
        return respuesta_pdf(buffer, f"pagos-{mes['valor']}.pdf")

    def datos_reportes(inicio, fin):
        pedidos = pedidos_del_mes(inicio, fin, incluir_cancelados=True)
        ventas = [p for p in pedidos if p.estado not in ESTADOS_SIN_VENTA]
        por_estado = {}
        for p in pedidos:
            por_estado.setdefault(p.estado, {"cantidad": 0, "total": 0.0})
            por_estado[p.estado]["cantidad"] += 1
            por_estado[p.estado]["total"] += float(p.importe_total or 0)

        pagos = pagos_del_mes(inicio, fin)
        por_metodo = {}
        for pago in pagos:
            por_metodo.setdefault(pago.metodo, {"cantidad": 0, "total": 0.0})
            por_metodo[pago.metodo]["cantidad"] += 1
            por_metodo[pago.metodo]["total"] += float(pago.monto)

        _, trueque = resumen_trueque_por_negocio(inicio, fin)

        por_cliente = {}
        for p in ventas:
            nombre = p.cliente.nombre_completo if p.cliente else "Cliente"
            por_cliente[nombre] = por_cliente.get(nombre, 0.0) + float(p.importe_total or 0)
        top_clientes = sorted(por_cliente.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            "por_estado": por_estado,
            "por_metodo": por_metodo,
            "credito_generado": float(trueque["a_favor"]),
            "credito_consumido": float(trueque["usado"]),
            "credito_vigente": float(trueque["saldo"]),
            "top_clientes": [{"nombre": n, "total": t} for n, t in top_clientes],
            "total_facturado": sum(float(p.importe_total or 0) for p in ventas),
            "total_cobrado": sum(float(p.monto) for p in pagos),
            "total_pendiente": sum(float(p.saldo or 0) for p in ventas if p.saldo > 0),
            "pedidos_mes": len(ventas),
            "urgentes_mes": sum(1 for p in ventas if p.urgente),
            "cargos_urgentes": sum(float(p.cargo_urgente or 0) for p in ventas if p.urgente),
        }

    def historico_mensual(n=12):
        filas = []
        for inicio in meses_atras(n):
            fin = rango_mes(inicio.strftime("%Y-%m"))[1]
            ventas = pedidos_del_mes(inicio, fin)
            _, trueque = resumen_trueque_por_negocio(inicio, fin)
            filas.append({
                "valor": inicio.strftime("%Y-%m"),
                "mes": nombre_mes(inicio).capitalize(),
                "pedidos": len(ventas),
                "facturado": sum(float(p.importe_total or 0) for p in ventas),
                "cobrado": sum(float(p.monto) for p in pagos_del_mes(inicio, fin)),
                "trueque_a_favor": float(trueque["a_favor"]),
                "trueque_usado": float(trueque["usado"]),
            })
        return filas

    @app.route("/crm/reportes")
    @protegido()
    def crm_reportes():
        mes = contexto_mes(request.args.get("mes"))
        return render_template(
            "crm_reportes.html",
            mes=mes,
            historico=historico_mensual(),
            rol=rol_crm_actual(),
            **datos_reportes(mes["inicio"], mes["fin"]),
        )

    @app.route("/crm/reportes/pdf")
    @protegido()
    def crm_reportes_pdf():
        mes = contexto_mes(request.args.get("mes"))
        d = datos_reportes(mes["inicio"], mes["fin"])
        buffer = generar_pdf(
            titulo="Reporte del CRM",
            subtitulo=f"{mes['etiqueta'].capitalize()} · {d['pedidos_mes']} pedido(s)",
            secciones=[
                {
                    "titulo": "Resumen del mes",
                    "columnas": ["Concepto", "Importe"],
                    "filas": [
                        [celda("Generado (pedidos del mes)"), celda(dinero2(d["total_facturado"]))],
                        [celda("Cobrado en el mes"), celda(dinero2(d["total_cobrado"]))],
                        [celda("Pendiente por cobrar de pedidos del mes"), celda(dinero2(d["total_pendiente"]))],
                        [celda(f"Cargos por entregas urgentes ({d['urgentes_mes']})"), celda(dinero2(d["cargos_urgentes"]))],
                        [celda("Trueque a favor generado"), celda(dinero2(d["credito_generado"]))],
                        [celda("Trueque usado"), celda(dinero2(d["credito_consumido"]))],
                        [celda("Saldo de trueque al cierre del mes"), celda(dinero2(d["credito_vigente"]))],
                    ],
                    "anchos": [12.4, 6],
                },
                {
                    "titulo": "Pedidos por estado",
                    "columnas": ["Estado", "Pedidos", "Importe"],
                    "filas": [[celda(e), celda(v["cantidad"]), celda(dinero2(v["total"]))] for e, v in d["por_estado"].items()],
                    "anchos": [8, 4, 6.4],
                    "vacio": "Sin pedidos en el mes.",
                },
                {
                    "titulo": "Cobros por método",
                    "columnas": ["Método", "Pagos", "Total"],
                    "filas": [[celda(m), celda(v["cantidad"]), celda(dinero2(v["total"]))] for m, v in d["por_metodo"].items()],
                    "anchos": [8, 4, 6.4],
                    "vacio": "Sin pagos en el mes.",
                },
                {
                    "titulo": "Top clientes del mes",
                    "columnas": ["#", "Cliente", "Comprado"],
                    "filas": [[celda(i), celda(c["nombre"]), celda(dinero2(c["total"]))] for i, c in enumerate(d["top_clientes"], 1)],
                    "anchos": [1.4, 10.6, 6.4],
                    "vacio": "Sin compras en el mes.",
                },
                {
                    "titulo": "Histórico de los últimos 12 meses",
                    "columnas": ["Mes", "Pedidos", "Generado", "Cobrado", "Trueque a favor", "Trueque usado"],
                    "filas": [
                        [celda(h["mes"]), celda(h["pedidos"]), celda(dinero2(h["facturado"])), celda(dinero2(h["cobrado"])), celda(dinero2(h["trueque_a_favor"])), celda(dinero2(h["trueque_usado"]))]
                        for h in historico_mensual()
                    ],
                    "anchos": [3.6, 2, 3.3, 3.3, 3.1, 3.1],
                },
            ],
        )
        return respuesta_pdf(buffer, f"reporte-crm-{mes['valor']}.pdf")

    @app.route("/crm/pedidos")
    @protegido()
    def crm_pedidos():
        mes = contexto_mes(request.args.get("mes"))
        pedidos = pedidos_mes_y_abiertos(mes["inicio"], mes["fin"])
        ordenes = OrdenLlenadoCRM.query.order_by(OrdenLlenadoCRM.id.desc()).limit(20).all()
        productos = PT.query.filter_by(estatus="Activo").order_by(PT.nombre).all()
        saldos_trueque = {p.cliente_id: float(saldo_trueque_cliente(p.cliente_id)) for p in pedidos}
        return render_template(
            "crm_pedidos.html",
            pedidos=pedidos,
            ordenes=ordenes,
            productos=productos,
            mes=mes,
            saldos_trueque=saldos_trueque,
            metodos_pago=METODOS_PAGO,
            rol=rol_crm_actual(),
        )

    def pedidos_mes_y_abiertos(inicio, fin):
        """Pedidos creados en el mes, mas los de meses anteriores que siguen
        abiertos (para que nada pendiente desaparezca al cambiar de mes)."""
        cerrados = {"Entregado"} | ESTADOS_SIN_VENTA
        return [
            p for p in PedidoCRM.query.order_by(PedidoCRM.id.desc()).all()
            if inicio <= p.fecha < fin or (p.fecha < inicio and p.estado not in cerrados)
        ]

    @app.route("/crm/pedidos/pdf")
    @protegido()
    def crm_pedidos_pdf():
        mes = contexto_mes(request.args.get("mes"))
        pedidos = pedidos_del_mes(mes["inicio"], mes["fin"], incluir_cancelados=True)
        ventas = [p for p in pedidos if p.estado not in ESTADOS_SIN_VENTA]
        buffer = generar_pdf(
            titulo="Pedidos del mes",
            subtitulo=(
                f"{mes['etiqueta'].capitalize()} · {len(pedidos)} pedido(s) · generado "
                f"{dinero2(sum((p.importe_total for p in ventas), Decimal('0')))} · por cobrar "
                f"{dinero2(sum((p.saldo for p in ventas if p.saldo > 0), Decimal('0')))}"
            ),
            secciones=[{
                "columnas": ["Fecha", "Folio", "Cliente", "Productos", "Estado", "Pago", "Total", "Saldo"],
                "filas": [
                    [
                        celda(p.fecha.strftime("%d/%m/%Y")),
                        celda(p.folio),
                        celda(p.cliente.nombre_completo if p.cliente else ""),
                        celda("\n".join(f"{float(l.cantidad):g} {l.producto.nombre if l.producto else 'Producto'}" for l in p.lineas)),
                        celda(p.estado + ("\nUrgente" if p.urgente else ""), muted=True),
                        celda(p.forma_pago, muted=True),
                        celda(dinero2(p.importe_total)),
                        celda(dinero2(p.saldo)),
                    ]
                    for p in pedidos
                ],
                "anchos": [1.9, 2.1, 3, 3.9, 2.2, 1.9, 1.8, 1.6],
                "vacio": "No hubo pedidos en este mes.",
            }],
        )
        return respuesta_pdf(buffer, f"pedidos-{mes['valor']}.pdf")

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

    def crear_pedido_desde_datos(data):
        """Crea un PedidoCRM (mas su cliente/domicilio/lineas) a partir del
        mismo formato de datos que ya usaba el API del CRM. La usan tanto el
        API interno (ventas capturando a mano) como la tienda publica
        (el cliente arma su propio carrito): en ambos casos el pedido queda
        en 'Recibido', pendiente de que ventas lo reserve, igual que hoy."""
        token = texto(data, "token", 80) or secrets.token_hex(24)
        existente = PedidoCRM.query.filter_by(token=token).first()
        if existente:
            return existente, True
        cliente_data = data.get("cliente") or {}
        domicilio_data = data.get("domicilio") or {}
        cliente = None
        if data.get("cliente_id"):
            cliente = db.session.get(ClienteCRM, data["cliente_id"])
        telefono = texto(cliente_data, "telefono", 40)
        if not cliente and telefono:
            cliente = ClienteCRM.query.filter_by(telefono=telefono).first()
        if not cliente:
            rfc = texto(cliente_data, "rfc", 13).upper()
            if rfc and not RFC_CLIENTE_REGEX.match(rfc):
                raise ValueError("El RFC del cliente no tiene un formato valido.")
            cliente = ClienteCRM(
                nombre=texto(cliente_data, "nombre", 180, True),
                apellidos=texto(cliente_data, "apellidos", 180) or None,
                rfc=rfc or None,
                telefono=telefono,
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
        registrar_evento(pedido, "Pedido recibido", f"Pedido creado desde {pedido.canal}.", {"canal": pedido.canal})
        return pedido, False

    @app.route("/api/crm/pedidos", methods=["GET", "POST"])
    @protegido_json()
    def api_crm_pedidos():
        if request.method == "GET":
            return jsonify([serializar_pedido(p) for p in PedidoCRM.query.order_by(PedidoCRM.id.desc()).all()])
        if not tiene_rol("ventas"):
            return jsonify({"error": "Solo ventas puede crear pedidos."}), 403
        data = request.json or {}
        try:
            pedido, ya_existia = crear_pedido_desde_datos(data)
            db.session.commit()
            return jsonify(serializar_pedido(pedido)), (200 if ya_existia else 201)
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
                pedido.fecha_ruta = pedido.fecha_ruta or datetime.utcnow().date()
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

    def metricas_tablero(mes, ahora):
        todos = PedidoCRM.query.all()
        activos = [p for p in todos if p.estado not in {"Entregado", "Cancelado", "Devuelto"}]
        del_mes = pedidos_del_mes(mes["inicio"], mes["fin"])
        entregas = fechas_entrega()
        entregas_mes = sum(1 for f in entregas.values() if mes["inicio"] <= f < mes["fin"])
        entregas_hoy = sum(1 for f in entregas.values() if f.date() == ahora.date())
        pendientes_cobro = [p for p in activos if p.saldo > 0]
        cobrado_mes = sum(float(p.monto) for p in pagos_del_mes(mes["inicio"], mes["fin"]))
        return {
            "activos": len(activos),
            "nuevos_mes": len(del_mes),
            "generado_mes": sum(float(p.importe_total or 0) for p in del_mes),
            "generado_mes_fmt": dinero(sum(float(p.importe_total or 0) for p in del_mes)),
            "cobrado_mes_fmt": dinero(cobrado_mes),
            "cobrado_mes": cobrado_mes,
            "entregas_mes": entregas_mes,
            "entregas_hoy": entregas_hoy,
            "en_ruta_ahora": sum(1 for p in todos if p.estado == "En ruta"),
            "por_cobrar": sum(float(p.saldo or 0) for p in pendientes_cobro),
            "por_cobrar_fmt": dinero(sum(float(p.saldo or 0) for p in pendientes_cobro)),
            "por_cobrar_cuenta": len(pendientes_cobro),
        }

    @app.route("/crm/tablero/pdf")
    @protegido()
    def crm_tablero_pdf():
        mes = contexto_mes(request.args.get("mes"))
        m = metricas_tablero(mes, datetime.utcnow())
        del_mes = pedidos_del_mes(mes["inicio"], mes["fin"], incluir_cancelados=True)
        por_estado = {}
        for p in del_mes:
            fila = por_estado.setdefault(p.estado, [0, Decimal("0")])
            fila[0] += 1
            fila[1] += p.importe_total
        buffer = generar_pdf(
            titulo="Tablero de pedidos",
            subtitulo=f"{mes['etiqueta'].capitalize()} · generado el {fecha_larga_es(datetime.utcnow())}",
            secciones=[
                {
                    "titulo": "Indicadores",
                    "columnas": ["Indicador", "Valor"],
                    "filas": [
                        [celda(f"Generado en {mes['etiqueta']}"), celda(f"{dinero2(m['generado_mes'])} ({m['nuevos_mes']} pedidos)")],
                        [celda(f"Cobrado en {mes['etiqueta']}"), celda(dinero2(m["cobrado_mes"]))],
                        [celda(f"Entregas en {mes['etiqueta']}"), celda(m["entregas_mes"])],
                        [celda("Pedidos activos hoy"), celda(m["activos"])],
                        [celda("Por cobrar hoy"), celda(f"{dinero2(m['por_cobrar'])} ({m['por_cobrar_cuenta']} pedidos)")],
                    ],
                    "anchos": [9, 9.4],
                },
                {
                    "titulo": "Pedidos del mes por estado",
                    "columnas": ["Estado", "Pedidos", "Importe"],
                    "filas": [[celda(e), celda(v[0]), celda(dinero2(v[1]))] for e, v in por_estado.items()],
                    "anchos": [8, 4, 6.4],
                    "vacio": "No hubo pedidos en este mes.",
                },
            ],
        )
        return respuesta_pdf(buffer, f"tablero-{mes['valor']}.pdf")

    @app.route("/crm/tablero")
    @protegido()
    def crm_tablero():
        ahora = datetime.utcnow()
        mes = contexto_mes(request.args.get("mes"))
        cerrados = {"Entregado", "Cancelado", "Devuelto"}
        # Los pedidos abiertos siempre se ven; los ya cerrados solo en el mes en que se cerraron.
        pedidos = [
            p for p in PedidoCRM.query.order_by(PedidoCRM.fecha_actualizacion.desc()).all()
            if p.estado not in cerrados or mes["inicio"] <= p.fecha_actualizacion < mes["fin"]
        ]

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
            if p.urgente and p.estado in ESTADOS_POR_RUTEAR | {"En ruta"}:
                return f"Entrega urgente (+{dinero(p.cargo_urgente)})", True
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

        metricas = metricas_tablero(mes, ahora)

        return render_template(
            "crm_tablero.html",
            columnas=columnas,
            metricas=metricas,
            mes=mes,
            hoy_es=fecha_larga_es(ahora),
            nombre_usuario=session.get("crm_nombre") or ("Administrador ERP" if session.get("admin_logueado") else "Equipo NYDS"),
            rol=rol_crm_actual(),
        )

    @app.route("/api/crm/pedidos/<int:id_pedido>/documentos", methods=["GET", "POST"])
    @protegido_json()
    def documentos_pedido(id_pedido):
        pedido = db.session.get(PedidoCRM, id_pedido)
        if not pedido:
            return jsonify({"error": "Pedido no encontrado"}), 404

        if request.method == "GET":
            docs = (
                PedidoDocumentoCRM.query.filter_by(pedido_id=id_pedido, activo=True)
                .order_by(PedidoDocumentoCRM.fecha.desc())
                .all()
            )
            return jsonify([
                {
                    "id": d.id,
                    "tipo_documento": d.tipo_documento,
                    "nombre_original": d.nombre_original,
                    "tamano_bytes": d.tamano_bytes,
                    "usuario": d.usuario,
                    "fecha": d.fecha.strftime("%d/%m/%Y %H:%M"),
                    "url_descarga": url_for("descargar_documento_pedido", id_documento=d.id),
                }
                for d in docs
            ])

        archivo = request.files.get("archivo")
        tipo = (request.form.get("tipo_documento") or "Otro").strip()
        if tipo not in TIPOS_DOCUMENTO_PEDIDO:
            tipo = "Otro"
        if not archivo or not archivo.filename:
            return jsonify({"error": "Archivo requerido"}), 400
        if not allowed_file(archivo.filename):
            return jsonify({"error": "Tipo de archivo no permitido"}), 400

        nombre_original = archivo.filename
        ext = nombre_original.rsplit(".", 1)[1].lower()
        nombre_guardado = secure_filename(f"pedido_{id_pedido}_{int(datetime.utcnow().timestamp())}.{ext}")
        ruta_guardado = os.path.join(UPLOAD_FOLDER, nombre_guardado)
        archivo.save(ruta_guardado)

        doc = PedidoDocumentoCRM(
            pedido_id=id_pedido,
            tipo_documento=tipo,
            nombre_original=nombre_original,
            nombre_archivo_guardado=nombre_guardado,
            tamano_bytes=os.path.getsize(ruta_guardado),
            usuario=crm_usuario_actual(),
        )
        db.session.add(doc)
        registrar_evento(pedido, "Documento", f"Se subio: {tipo} ({nombre_original})")
        db.session.commit()
        return jsonify({"id": doc.id}), 201

    @app.route("/api/crm/pedidos/documentos/<int:id_documento>/descargar", methods=["GET"])
    @protegido()
    def descargar_documento_pedido(id_documento):
        doc = db.session.get(PedidoDocumentoCRM, id_documento)
        if not doc or not doc.activo:
            abort(404)
        return send_from_directory(
            UPLOAD_FOLDER, doc.nombre_archivo_guardado, as_attachment=True, download_name=doc.nombre_original
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
        PedidoDocumentoCRM,
        crear_pedido_desde_datos,
        MovimientoTruequeCRM,
    )
