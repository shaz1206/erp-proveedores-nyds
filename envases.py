"""Catalogo de envases (botes, garrafas, botellas) ligados a las presentaciones.

Cada envase tiene SKU propio, material, capacidad en litros, color, proveedor
y precio por pieza. Se liga a las presentaciones de producto terminado de la
misma capacidad (un bote de 5 L sirve para las presentaciones de 5 L) y,
opcionalmente, a la materia prima que se descuenta del inventario al llenar.
Vive en tablas propias: no modifica materias primas ni presentaciones.
"""
import secrets
from datetime import datetime

from flask import abort, flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError

if __package__:
    from .precios import parsear_capacidad, parsear_precio
else:
    from precios import parsear_capacidad, parsear_precio

MATERIALES = ("PET", "HDPE", "PP", "PEBD", "Vidrio", "Aluminio", "Otro")


def registrar_envases(app, db, MP, Proveedor, PresentacionPT):
    enlace = db.Table(
        "envases_presentaciones",
        db.Column("envase_id", db.Integer, db.ForeignKey("envases.id"), primary_key=True),
        db.Column("presentacion_id", db.Integer, db.ForeignKey("pt_presentaciones.id"), primary_key=True),
    )

    class Envase(db.Model):
        __tablename__ = "envases"
        id = db.Column(db.Integer, primary_key=True)
        sku = db.Column(db.String(20), unique=True, nullable=False)
        nombre = db.Column(db.String(150), nullable=False)
        material = db.Column(db.String(20), nullable=False)
        capacidad = db.Column(db.Numeric(18, 3), nullable=False)
        color = db.Column(db.String(60))
        proveedor_id = db.Column(db.Integer, db.ForeignKey("proveedores.id_proveedor"))
        proveedor = db.relationship(Proveedor)
        mp_id = db.Column(db.Integer, db.ForeignKey("materias_primas.id_mp"))
        mp = db.relationship(MP)
        precio = db.Column(db.Numeric(18, 2))
        notas = db.Column(db.String(500))
        activo = db.Column(db.Boolean, nullable=False, default=True)
        creado_en = db.Column(db.DateTime, default=datetime.utcnow)
        presentaciones = db.relationship(PresentacionPT, secondary=enlace, order_by=PresentacionPT.nombre)

    def autorizado():
        return bool(session.get("admin_logueado"))

    def csrf():
        return session.setdefault("envases_csrf", secrets.token_hex(24))

    def csrf_valido():
        return secrets.compare_digest(request.form.get("csrf", ""), session.get("envases_csrf", "!"))

    def generar_sku():
        mayor = 0
        for (sku,) in db.session.query(Envase.sku).filter(Envase.sku.like("ENV-%")):
            if sku[4:].isdigit():
                mayor = max(mayor, int(sku[4:]))
        return f"ENV-{mayor + 1:06d}"

    def opciones(envase=None):
        proveedores = Proveedor.query.filter_by(estatus="Activo").order_by(Proveedor.razon_social).all()
        if envase and envase.proveedor and envase.proveedor not in proveedores:
            proveedores.append(envase.proveedor)
        materias = MP.query.filter(MP.activo.is_(True)).order_by(MP.nombre_oficial).all()
        if envase and envase.mp and envase.mp not in materias:
            materias.append(envase.mp)
        return proveedores, materias

    def presentaciones_de(capacidad):
        return (PresentacionPT.query.filter_by(capacidad=capacidad, activa=True).order_by(PresentacionPT.producto_id).all()
                if capacidad is not None else [])

    @app.route("/admin/envases")
    def envases_catalogo():
        if not autorizado():
            return redirect(url_for("login_admin"))
        q = request.args.get("q", "").strip()
        material = request.args.get("material", "")
        estado = request.args.get("estado", "")
        consulta = Envase.query
        if q:
            consulta = consulta.filter(db.or_(Envase.sku.contains(q, autoescape=True), Envase.nombre.contains(q, autoescape=True),
                                              Envase.color.contains(q, autoescape=True)))
        if material in MATERIALES:
            consulta = consulta.filter_by(material=material)
        if estado == "activos":
            consulta = consulta.filter(Envase.activo.is_(True))
        elif estado == "inactivos":
            consulta = consulta.filter(Envase.activo.is_(False))
        return render_template("envases.html", envases=consulta.order_by(Envase.capacidad, Envase.nombre).all(), q=q,
                               material=material, estado=estado, materiales=MATERIALES, csrf=csrf())

    @app.route("/admin/envases/nuevo", methods=["GET", "POST"])
    @app.route("/admin/envases/<int:id_envase>", methods=["GET", "POST"])
    def envase_formulario(id_envase=None):
        if not autorizado():
            return redirect(url_for("login_admin"))
        envase = db.get_or_404(Envase, id_envase) if id_envase else None
        valores = {"nombre": "", "material": "HDPE", "capacidad": "", "color": "", "proveedor_id": "", "mp_id": "", "precio": "",
                   "notas": "", "activo": True, "presentaciones": []}
        if envase:
            valores.update(nombre=envase.nombre, material=envase.material, capacidad=f"{envase.capacidad:g}", color=envase.color or "",
                           proveedor_id=envase.proveedor_id or "", mp_id=envase.mp_id or "",
                           precio=f"{envase.precio:.2f}" if envase.precio is not None else "", notas=envase.notas or "",
                           activo=envase.activo, presentaciones=[p.id for p in envase.presentaciones])
        errores = []
        if request.method == "POST":
            if not csrf_valido():
                abort(400, "Sesion de formulario vencida. Recarga la pagina.")
            f = request.form
            valores.update(nombre=f.get("nombre", "").strip(), material=f.get("material", ""), capacidad=f.get("capacidad", "").strip(),
                           color=f.get("color", "").strip(), proveedor_id=f.get("proveedor_id", ""), mp_id=f.get("mp_id", ""),
                           precio=f.get("precio", "").strip(), notas=f.get("notas", "").strip(), activo=bool(f.get("activo")) if envase else True,
                           presentaciones=[int(x) for x in f.getlist("presentaciones") if x.isdigit()])
            if not valores["nombre"] or len(valores["nombre"]) > 150:
                errores.append("Escribe el nombre del envase (maximo 150 caracteres).")
            if valores["material"] not in MATERIALES:
                errores.append("Selecciona un material valido.")
            if len(valores["color"]) > 60:
                errores.append("El color admite maximo 60 caracteres.")
            if len(valores["notas"]) > 500:
                errores.append("Las notas admiten maximo 500 caracteres.")
            capacidad = precio = None
            try:
                capacidad = parsear_capacidad(valores["capacidad"])
            except ValueError as e:
                errores.append(str(e))
            try:
                precio = parsear_precio(valores["precio"])
            except ValueError as e:
                errores.append(str(e))
            proveedor = materia = None
            if valores["proveedor_id"]:
                proveedor = db.session.get(Proveedor, int(valores["proveedor_id"])) if str(valores["proveedor_id"]).isdigit() else None
                if not proveedor or (proveedor.estatus != "Activo" and not (envase and envase.proveedor_id == proveedor.id_proveedor)):
                    errores.append("Selecciona un proveedor activo.")
            if valores["mp_id"]:
                materia = db.session.get(MP, int(valores["mp_id"])) if str(valores["mp_id"]).isdigit() else None
                if not materia:
                    errores.append("Selecciona una materia prima valida.")
            ligadas = []
            if capacidad is not None:
                validas = {p.id: p for p in presentaciones_de(capacidad)}
                for pid in dict.fromkeys(valores["presentaciones"]):
                    if pid not in validas:
                        errores.append("Solo puedes ligar presentaciones activas de la misma capacidad que el envase.")
                        break
                    ligadas.append(validas[pid])
            if not errores:
                destino = envase or Envase(sku=generar_sku())
                destino.nombre, destino.material, destino.capacidad = valores["nombre"], valores["material"], capacidad
                destino.color = valores["color"] or None
                destino.proveedor_id = proveedor.id_proveedor if proveedor else None
                destino.mp_id = materia.id_mp if materia else None
                destino.precio, destino.notas = precio, valores["notas"] or None
                if envase:
                    destino.activo = bool(f.get("activo"))
                destino.presentaciones = ligadas
                db.session.add(destino)
                try:
                    db.session.commit()
                    flash(f"Envase {destino.sku} guardado.", "success")
                    return redirect(url_for("envases_catalogo"))
                except IntegrityError:
                    db.session.rollback()
                    errores.append("No se pudo guardar; intenta de nuevo.")
        proveedores, materias = opciones(envase)
        todas = PresentacionPT.query.filter_by(activa=True).order_by(PresentacionPT.capacidad, PresentacionPT.producto_id).all()
        return render_template("envase_form.html", envase=envase, valores=valores, errores=errores, materiales=MATERIALES,
                               proveedores=proveedores, materias=materias, presentaciones=todas,
                               csrf=csrf()), (400 if errores else 200)

    @app.route("/admin/envases/<int:id_envase>/estado", methods=["POST"])
    def envase_estado(id_envase):
        if not autorizado():
            return redirect(url_for("login_admin"))
        if not csrf_valido():
            abort(400, "Sesion de formulario vencida. Recarga la pagina.")
        envase = db.get_or_404(Envase, id_envase)
        envase.activo = not envase.activo
        db.session.commit()
        flash(f"{envase.sku} {'activado' if envase.activo else 'desactivado'}.", "success")
        return redirect(url_for("envases_catalogo"))

    return Envase
