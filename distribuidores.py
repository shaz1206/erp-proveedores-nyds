"""Distribuidores: quien atiende a cada cliente, y revision de ventas.

- Cada cliente puede tener un distribuidor (una persona con rol ventas o
  supervisor). La asignacion vive en una tabla propia, sin tocar crm.py.
- "Confirmar ventas": pedidos que aun esperan confirmacion (Recibido o
  Pendiente de pago), filtrables por distribuidor. Confirmar y cancelar
  usan las mismas rutas del CRM (reservas, eventos, permisos), asi que no
  hay logica de negocio duplicada aqui.
- "Canceladas": pedidos cancelados con quien los cancelo, cuando y por que.
"""
from datetime import datetime

from flask import abort, flash, redirect, render_template, request, session, url_for

ESTADOS_POR_CONFIRMAR = ("Recibido", "Pendiente de pago")
ROLES_DISTRIBUIDOR = ("ventas", "supervisor")
MOTIVO_POR_DEFECTO = "Pedido cambiado a Cancelado."


def registrar_distribuidores(app, db, Persona, ClienteCRM, PedidoCRM, EventoCRM):
    class ClienteDistribuidor(db.Model):
        __tablename__ = "clientes_distribuidor"
        id = db.Column(db.Integer, primary_key=True)
        cliente_id = db.Column(db.Integer, db.ForeignKey("crm_clientes.id"), unique=True, nullable=False)
        cliente = db.relationship(ClienteCRM)
        persona_id = db.Column(db.Integer, db.ForeignKey("personas.id"), nullable=False, index=True)
        persona = db.relationship(Persona)
        asignado_en = db.Column(db.DateTime, default=datetime.utcnow)

    def distribuidores_disponibles():
        return (Persona.query.filter(Persona.activo.is_(True), Persona.rol.in_(ROLES_DISTRIBUIDOR))
                .order_by(Persona.nombre).all())

    def mapa_distribuidores():
        return {a.cliente_id: a.persona for a in ClienteDistribuidor.query.all()}

    def rol_actual():
        if session.get("crm_logueado"):
            return session.get("crm_rol", "")
        if session.get("admin_logueado"):
            return "supervisor"
        return ""

    def exigir_ventas():
        """None si puede entrar; si no, la respuesta que corresponde."""
        if not session.get("crm_logueado") and not session.get("admin_logueado"):
            return redirect(url_for("crm_login"))
        if rol_actual() not in ("supervisor", "ventas"):
            abort(403)
        return None

    def filtrar_por_distribuidor(pedidos, filtro, asignados):
        if not filtro:
            return pedidos
        if filtro == "sin":
            return [p for p in pedidos if p.cliente_id not in asignados]
        try:
            persona_id = int(filtro)
        except ValueError:
            return pedidos
        return [p for p in pedidos if p.cliente_id in asignados and asignados[p.cliente_id].id == persona_id]

    # ---------- ERP: asignar distribuidor a cada cliente ----------
    @app.route("/admin/distribuidores")
    def admin_distribuidores():
        if not session.get("admin_logueado"):
            return redirect(url_for("login_admin"))
        busqueda = (request.args.get("q") or "").strip()
        consulta = ClienteCRM.query
        if busqueda:
            consulta = consulta.filter(db.or_(ClienteCRM.nombre.contains(busqueda, autoescape=True),
                                              ClienteCRM.telefono.contains(busqueda, autoescape=True)))
        asignados = mapa_distribuidores()
        distribuidores = distribuidores_disponibles()
        resumen = [(d, sum(1 for p in asignados.values() if p.id == d.id)) for d in distribuidores]
        return render_template("distribuidores.html", clientes=consulta.order_by(ClienteCRM.nombre).all(),
                               asignados=asignados, distribuidores=distribuidores, resumen=resumen,
                               sin_asignar=ClienteCRM.query.count() - len(asignados), q=busqueda)

    @app.route("/admin/distribuidores/asignar", methods=["POST"])
    def admin_distribuidor_asignar():
        if not session.get("admin_logueado"):
            return redirect(url_for("login_admin"))
        cliente = db.session.get(ClienteCRM, int(request.form.get("cliente_id") or 0))
        if not cliente:
            flash("Cliente no encontrado.", "danger")
            return redirect(url_for("admin_distribuidores"))
        destino = url_for("admin_distribuidores", q=request.form.get("q") or None)
        asignacion = ClienteDistribuidor.query.filter_by(cliente_id=cliente.id).first()
        persona_id = request.form.get("persona_id") or ""
        if not persona_id:
            if asignacion:
                db.session.delete(asignacion)
                db.session.commit()
            flash(f"{cliente.nombre_completo} quedo sin distribuidor.", "success")
            return redirect(destino)
        persona = db.session.get(Persona, int(persona_id)) if persona_id.isdigit() else None
        if not persona or not persona.activo or persona.rol not in ROLES_DISTRIBUIDOR:
            flash("Elige una persona activa con rol ventas o supervisor.", "danger")
            return redirect(destino)
        if asignacion:
            asignacion.persona_id = persona.id
            asignacion.asignado_en = datetime.utcnow()
        else:
            db.session.add(ClienteDistribuidor(cliente_id=cliente.id, persona_id=persona.id))
        db.session.commit()
        flash(f"{cliente.nombre_completo} ahora es de {persona.nombre}.", "success")
        return redirect(destino)

    # ---------- CRM: confirmar ventas ----------
    @app.route("/crm/ventas/confirmar")
    def crm_confirmar_ventas():
        bloqueo = exigir_ventas()
        if bloqueo:
            return bloqueo
        asignados = mapa_distribuidores()
        filtro = request.args.get("distribuidor") or ""
        pendientes = (PedidoCRM.query.filter(PedidoCRM.estado.in_(ESTADOS_POR_CONFIRMAR))
                      .order_by(PedidoCRM.fecha).all())
        pendientes = filtrar_por_distribuidor(pendientes, filtro, asignados)
        return render_template("crm_confirmar_ventas.html", pedidos=pendientes, asignados=asignados,
                               distribuidores=distribuidores_disponibles(), filtro=filtro,
                               total=sum(p.importe_total for p in pendientes), rol=rol_actual())

    # ---------- CRM: ventas canceladas ----------
    @app.route("/crm/ventas/canceladas")
    def crm_ventas_canceladas():
        bloqueo = exigir_ventas()
        if bloqueo:
            return bloqueo
        asignados = mapa_distribuidores()
        filtro = request.args.get("distribuidor") or ""
        canceladas = PedidoCRM.query.filter_by(estado="Cancelado").order_by(PedidoCRM.fecha_actualizacion.desc()).all()
        canceladas = filtrar_por_distribuidor(canceladas, filtro, asignados)
        detalle = {}
        for p in canceladas:
            evento = (EventoCRM.query.filter_by(pedido_id=p.id, evento="Estado Cancelado")
                      .order_by(EventoCRM.id.desc()).first())
            motivo = evento.descripcion if evento and evento.descripcion != MOTIVO_POR_DEFECTO else ""
            detalle[p.id] = {"fecha": evento.fecha if evento else p.fecha_actualizacion,
                             "usuario": evento.usuario if evento else "", "motivo": motivo}
        return render_template("crm_ventas_canceladas.html", pedidos=canceladas, detalle=detalle, asignados=asignados,
                               distribuidores=distribuidores_disponibles(), filtro=filtro,
                               total=sum(p.importe_total for p in canceladas), rol=rol_actual())

    return ClienteDistribuidor
