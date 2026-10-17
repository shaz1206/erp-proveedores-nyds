"""Ordenes de produccion y lista de compra automatica.

Una orden de produccion dice "voy a producir X unidades del producto P". Con
la receta del producto se calcula que materia prima hace falta (incluyendo
subrecetas). Al registrar la produccion se descuenta esa materia prima del
inventario MP y se da de alta el producto terminado en el almacen PT.

La lista de compra suma lo que necesitan todas las ordenes planeadas,
le resta lo que hay disponible (existencia menos reservado por pedidos) y
sugiere a que proveedor comprar; desde ahi se emite la orden de compra con
el mismo formulario y las mismas validaciones del modulo de compras.
"""
import re
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from flask import abort, flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError

UBICACION_PT = "Almacen PT"
UBICACION_MP_DEFECTO = "Produccion"
SEIS = Decimal(".000001")
MAX_CANTIDAD = Decimal("1000000000")


def registrar_produccion(app, db, MP, PT, Receta, AjusteInventario, MovimientoMP, MovimientoPT,
                         ReservaERP, Proveedor, MateriaPrimaProveedor, usuario_actual):
    class OrdenProduccion(db.Model):
        __tablename__ = "produccion_ordenes"
        id = db.Column(db.Integer, primary_key=True)
        folio = db.Column(db.String(20), unique=True)
        producto_id = db.Column(db.Integer, db.ForeignKey("productos_terminados.id"), nullable=False, index=True)
        producto = db.relationship(PT)
        receta_id = db.Column(db.Integer, db.ForeignKey("recetas_borrador.id"))
        cantidad = db.Column(db.Numeric(18, 6), nullable=False)
        unidad = db.Column(db.String(20))
        estado = db.Column(db.String(20), nullable=False, default="Planeada")
        notas = db.Column(db.String(500))
        creada_en = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        creada_por = db.Column(db.String(100), nullable=False)
        cerrada_en = db.Column(db.DateTime)
        cerrada_por = db.Column(db.String(100))
        token = db.Column(db.String(80), unique=True, nullable=False)
        consumos = db.relationship("ProduccionConsumo", backref="orden", cascade="all, delete-orphan",
                                   order_by="ProduccionConsumo.id")

    class ProduccionConsumo(db.Model):
        __tablename__ = "produccion_consumos"
        id = db.Column(db.Integer, primary_key=True)
        orden_id = db.Column(db.Integer, db.ForeignKey("produccion_ordenes.id"), nullable=False, index=True)
        mp_id = db.Column(db.Integer, db.ForeignKey("materias_primas.id_mp"), nullable=False)
        mp = db.relationship(MP)
        cantidad = db.Column(db.Numeric(18, 6), nullable=False)
        unidad = db.Column(db.String(20), nullable=False)

    # ---------- calculo de materia prima ----------
    def receta_de(producto_id):
        return (Receta.query.filter_by(producto_id=producto_id).filter(Receta.estado.in_(["Activo", "Borrador"]))
                .order_by((Receta.estado == "Activo").desc(), Receta.version.desc(), Receta.id.desc()).first())

    def expandir(receta, cantidad, acumulado, nivel=0):
        if nivel > 5:
            raise ValueError("Las subrecetas estan demasiado anidadas; revisa la receta.")
        if not receta.rendimiento or receta.rendimiento <= 0:
            raise ValueError(f"La receta {receta.nombre} no tiene rendimiento configurado.")
        factor = cantidad / receta.rendimiento
        for linea in receta.lineas:
            requerido = linea.cantidad * factor
            if linea.mp_id:
                dato = acumulado.setdefault(linea.mp_id, {"mp": linea.mp, "cantidad": Decimal(0), "unidad": linea.unidad})
                dato["cantidad"] += requerido
            elif linea.subreceta:
                expandir(linea.subreceta, requerido, acumulado, nivel + 1)
            else:
                raise ValueError(f"No se pudo identificar el ingrediente {linea.nombre_original} de la receta.")

    def requerimientos(producto_id, cantidad):
        receta = receta_de(producto_id)
        if not receta:
            raise ValueError("Este producto no tiene receta. Crea o activa su receta primero.")
        acumulado = {}
        expandir(receta, cantidad, acumulado)
        if not acumulado:
            raise ValueError("La receta no tiene materias primas.")
        lista = [{**d, "cantidad": d["cantidad"].quantize(SEIS)} for d in acumulado.values()]
        lista.sort(key=lambda d: d["mp"].nombre_oficial)
        return receta, lista

    def saldo_mp(mp_id):
        total = Decimal(0)
        for mov in MovimientoMP.query.filter_by(mp_id=mp_id).all():
            orden = mov.recepcion.linea.orden if mov.recepcion and mov.recepcion.linea else None
            if not (orden and (orden.proveedor_nombre or "").startswith("DEMO - ")):
                total += mov.cantidad
        for ajuste in AjusteInventario.query.filter_by(mp_id=mp_id, demo=False).all():
            total += ajuste.cantidad
        return total

    def reservado_mp(mp_id):
        total = (db.session.query(db.func.coalesce(db.func.sum(ReservaERP.cantidad), 0))
                 .filter(ReservaERP.tipo == "Materia prima", ReservaERP.mp_id == mp_id, ReservaERP.estado == "Activa").scalar())
        return Decimal(str(total or 0))

    def disponible_mp(mp_id):
        return saldo_mp(mp_id) - reservado_mp(mp_id)

    def necesidades(orden):
        filas = []
        for c in orden.consumos:
            disponible = disponible_mp(c.mp_id)
            filas.append({"mp": c.mp, "requerido": c.cantidad, "unidad": c.unidad, "disponible": disponible,
                          "faltante": max(Decimal(0), c.cantidad - disponible)})
        return filas

    def numero_positivo(texto):
        texto = str(texto or "").strip()
        if not re.fullmatch(r"\d+(\.\d+)?", texto):
            raise ValueError("Escribe una cantidad valida, solo numeros.")
        try:
            valor = Decimal(texto)
        except InvalidOperation:
            raise ValueError("Escribe una cantidad valida, solo numeros.")
        if not valor.is_finite() or valor <= 0 or valor >= MAX_CANTIDAD or valor.as_tuple().exponent < -6:
            raise ValueError("La cantidad debe ser mayor a cero, con maximo seis decimales.")
        return valor

    def autorizado():
        return bool(session.get("admin_logueado"))

    def csrf():
        return session.setdefault("produccion_csrf", secrets.token_hex(24))

    def csrf_valido():
        return secrets.compare_digest(request.form.get("csrf", ""), session.get("produccion_csrf", "!"))

    # ---------- ordenes ----------
    @app.route("/admin/produccion")
    def produccion_ordenes():
        if not autorizado():
            return redirect(url_for("login_admin"))
        estado = request.args.get("estado")
        if estado is None:  # al entrar se muestra lo que falta por producir
            estado = "Planeada" if OrdenProduccion.query.filter_by(estado="Planeada").first() else "todas"
        consulta = OrdenProduccion.query
        if estado in ("Planeada", "Producida", "Cancelada"):
            consulta = consulta.filter_by(estado=estado)
        productos = PT.query.filter_by(estatus="Activo").order_by(PT.nombre).all()
        con_receta = {p.id: bool(receta_de(p.id)) for p in productos}
        return render_template("produccion.html", ordenes=consulta.order_by(OrdenProduccion.id.desc()).all(),
                               productos=productos, con_receta=con_receta, estado=estado, csrf=csrf(),
                               token=secrets.token_hex(24))

    @app.route("/admin/produccion", methods=["POST"])
    def produccion_crear():
        if not autorizado():
            return redirect(url_for("login_admin"))
        if not csrf_valido():
            abort(400, "Sesion de formulario vencida. Recarga la pagina.")
        token = (request.form.get("token") or "")[:80]
        existente = OrdenProduccion.query.filter_by(token=token).first() if token else None
        if existente:
            return redirect(url_for("produccion_detalle", id_orden=existente.id))
        try:
            if not token:
                raise ValueError("Recarga la pagina e intenta de nuevo.")
            producto = db.session.get(PT, int(request.form.get("producto_id") or 0))
            if not producto or producto.estatus != "Activo":
                raise ValueError("Selecciona un producto terminado activo.")
            cantidad = numero_positivo(request.form.get("cantidad"))
            receta, lista = requerimientos(producto.id, cantidad)
            orden = OrdenProduccion(producto_id=producto.id, receta_id=receta.id, cantidad=cantidad,
                                    unidad=receta.unidad_rendimiento, notas=(request.form.get("notas") or "").strip()[:500] or None,
                                    creada_por=usuario_actual(), token=token)
            db.session.add(orden)
            db.session.flush()
            orden.folio = f"OP-{orden.id:06d}"
            for d in lista:
                orden.consumos.append(ProduccionConsumo(mp_id=d["mp"].id_mp, cantidad=d["cantidad"], unidad=d["unidad"]))
            db.session.commit()
            flash(f"Orden {orden.folio} planeada.", "success")
            return redirect(url_for("produccion_detalle", id_orden=orden.id))
        except (ValueError, IntegrityError) as e:
            db.session.rollback()
            flash(str(e) if isinstance(e, ValueError) else "No se pudo guardar la orden.", "danger")
            return redirect(url_for("produccion_ordenes"))

    @app.route("/admin/produccion/<int:id_orden>")
    def produccion_detalle(id_orden):
        if not autorizado():
            return redirect(url_for("login_admin"))
        orden = db.get_or_404(OrdenProduccion, id_orden)
        filas = necesidades(orden) if orden.estado == "Planeada" else [
            {"mp": c.mp, "requerido": c.cantidad, "unidad": c.unidad, "disponible": None, "faltante": Decimal(0)}
            for c in orden.consumos]
        return render_template("produccion_detalle.html", orden=orden, filas=filas, csrf=csrf(),
                               faltan=any(f["faltante"] > 0 for f in filas), ubicacion=UBICACION_MP_DEFECTO)

    @app.route("/admin/produccion/<int:id_orden>/producir", methods=["POST"])
    def produccion_producir(id_orden):
        if not autorizado():
            return redirect(url_for("login_admin"))
        if not csrf_valido():
            abort(400, "Sesion de formulario vencida. Recarga la pagina.")
        orden = db.get_or_404(OrdenProduccion, id_orden)
        try:
            if orden.estado != "Planeada":
                raise ValueError(f"La orden ya esta {orden.estado.lower()}.")
            faltantes = [f for f in necesidades(orden) if f["faltante"] > 0]
            if faltantes:
                nombres = ", ".join(f["mp"].nombre_oficial for f in faltantes)
                raise ValueError(f"No hay materia prima suficiente: {nombres}.")
            ubicacion = (request.form.get("ubicacion") or "").strip()[:220] or UBICACION_MP_DEFECTO
            usuario = usuario_actual()
            for c in orden.consumos:
                db.session.add(AjusteInventario(
                    mp_id=c.mp_id, tipo="Ajuste de salida", cantidad=-c.cantidad, unidad=c.unidad, ubicacion=ubicacion,
                    lote="", demo=False, motivo=f"Consumo por produccion {orden.folio}", referencia=orden.folio,
                    usuario=usuario, token=f"prod-mp-{orden.id}-{c.id}"))
            db.session.add(MovimientoPT(
                producto_id=orden.producto_id, tipo="Alta por produccion", cantidad=orden.cantidad, unidad="pieza",
                ubicacion=UBICACION_PT, referencia=orden.folio, usuario=usuario, token=f"prod-pt-{orden.id}"))
            orden.estado = "Producida"
            orden.cerrada_en = datetime.utcnow()
            orden.cerrada_por = usuario
            db.session.commit()
            flash(f"{orden.folio} registrada: {orden.cantidad:g} {orden.producto.nombre} entraron al almacen PT.", "success")
        except (ValueError, IntegrityError) as e:
            db.session.rollback()
            flash(str(e) if isinstance(e, ValueError) else "No se pudo registrar la produccion.", "danger")
        return redirect(url_for("produccion_detalle", id_orden=id_orden))

    @app.route("/admin/produccion/<int:id_orden>/cancelar", methods=["POST"])
    def produccion_cancelar(id_orden):
        if not autorizado():
            return redirect(url_for("login_admin"))
        if not csrf_valido():
            abort(400, "Sesion de formulario vencida. Recarga la pagina.")
        orden = db.get_or_404(OrdenProduccion, id_orden)
        if orden.estado != "Planeada":
            flash(f"La orden ya esta {orden.estado.lower()}.", "danger")
        else:
            orden.estado = "Cancelada"
            orden.cerrada_en = datetime.utcnow()
            orden.cerrada_por = usuario_actual()
            db.session.commit()
            flash(f"{orden.folio} cancelada.", "success")
        return redirect(url_for("produccion_detalle", id_orden=id_orden))

    # ---------- lista de compra ----------
    def condiciones_vigentes():
        hoy = date.today()
        return (MateriaPrimaProveedor.query.join(Proveedor, MateriaPrimaProveedor.id_proveedor == Proveedor.id_proveedor)
                .join(MP, MateriaPrimaProveedor.id_mp == MP.id_mp)
                .filter(MateriaPrimaProveedor.activo.is_(True), Proveedor.estatus == "Activo", MP.activo.is_(True),
                        MateriaPrimaProveedor.vigencia_desde <= hoy,
                        db.or_(MateriaPrimaProveedor.vigencia_hasta.is_(None), MateriaPrimaProveedor.vigencia_hasta >= hoy))
                .order_by(MateriaPrimaProveedor.principal.desc(), MateriaPrimaProveedor.precio_vigente).all())

    def presentaciones_necesarias(condicion, faltante):
        """Presentaciones (enteras) para cubrir el faltante respetando la compra minima del proveedor."""
        factor = condicion.factor_a_unidad_base
        if not factor or factor <= 0:
            return None
        cantidad = (faltante / factor).to_integral_value(rounding=ROUND_CEILING)
        minimo = condicion.compra_minima or Decimal(0)
        if condicion.unidad_compra_minima == "Presentación":
            minimas = minimo
        elif condicion.unidad_compra_minima == condicion.unidad_contenido and condicion.contenido_presentacion:
            minimas = minimo / condicion.contenido_presentacion
        elif condicion.unidad_compra_minima == condicion.materia_prima.unidad_base:
            minimas = minimo / factor
        else:
            minimas = Decimal(0)
        return max(cantidad, minimas.to_integral_value(rounding=ROUND_CEILING), Decimal(1))

    @app.route("/admin/produccion/lista-compra")
    def produccion_lista_compra():
        if not autorizado():
            return redirect(url_for("login_admin"))
        requerido = {}
        for orden in OrdenProduccion.query.filter_by(estado="Planeada").order_by(OrdenProduccion.id).all():
            for c in orden.consumos:
                fila = requerido.setdefault(c.mp_id, {"mp": c.mp, "requerido": Decimal(0), "ordenes": []})
                fila["requerido"] += c.cantidad
                fila["ordenes"].append(orden.folio)
        condiciones = {}
        for c in condiciones_vigentes():
            condiciones.setdefault(c.id_mp, c)  # la primera es la principal / mas barata
        por_proveedor, sin_proveedor, suficientes = {}, [], []
        for mp_id, fila in sorted(requerido.items(), key=lambda kv: kv[1]["mp"].nombre_oficial):
            disponible = disponible_mp(mp_id)
            faltante = max(Decimal(0), fila["requerido"] - disponible)
            fila.update(disponible=disponible, faltante=faltante)
            if faltante <= 0:
                suficientes.append(fila)
                continue
            cond = condiciones.get(mp_id)
            cantidad = presentaciones_necesarias(cond, faltante) if cond else None
            if cond is None or cantidad is None:
                sin_proveedor.append(fila)
                continue
            fila.update(condicion=cond, presentaciones=cantidad)
            grupo = por_proveedor.setdefault((cond.id_proveedor, cond.moneda), {"proveedor": cond.proveedor, "moneda": cond.moneda,
                                                                                 "filas": [], "lead": 0})
            grupo["filas"].append(fila)
            grupo["lead"] = max(grupo["lead"], cond.lead_time_dias or 0)
        grupos = sorted(por_proveedor.values(), key=lambda g: g["proveedor"].razon_social)
        for g in grupos:
            g["entrega"] = date.today() + timedelta(days=g["lead"])
        session.setdefault("compras_csrf", secrets.token_hex(24))
        return render_template("produccion_lista_compra.html", grupos=grupos, sin_proveedor=sin_proveedor,
                               suficientes=suficientes, compras_csrf=session["compras_csrf"], hoy=date.today(),
                               token=secrets.token_hex(24), hay_ordenes=bool(requerido))

    return OrdenProduccion, ProduccionConsumo
