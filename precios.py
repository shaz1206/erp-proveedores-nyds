"""Presentaciones (1 L, 5 L, 20 L...) y listas de precios por cliente.

Un producto terminado se vende en varias presentaciones y cada presentacion
tiene un precio base. Una lista de precios (ej. "Distribuidores", "Mayoreo")
puede sobreescribir ese precio; a cada cliente se le asigna como maximo una
lista. El precio que se le cobra a un cliente es siempre:

    precio de su lista para esa presentacion  ->  si no hay, precio base.

Todo vive en tablas propias (no se altera crm.py ni la tabla de clientes): la
asignacion cliente-lista es una tabla aparte.
"""
from datetime import datetime
from io import BytesIO
from decimal import Decimal, InvalidOperation

from flask import (abort, flash, jsonify, redirect, send_file,
                   render_template, request, session, url_for)
from sqlalchemy.exc import IntegrityError

if __package__:
    from .reportes_pdf import generar_pdf, respuesta_pdf
else:
    from reportes_pdf import generar_pdf, respuesta_pdf

PRESENTACIONES_ESTANDAR = (("1 L", Decimal("1")), ("5 L", Decimal("5")), ("20 L", Decimal("20")))
MAX_PRECIO = Decimal("1000000")


def parsear_precio(texto):
    """Devuelve Decimal (>0, max 2 decimales) o None si viene vacio."""
    texto = (texto or "").strip().replace(",", "")
    if not texto:
        return None
    try:
        valor = Decimal(texto)
    except InvalidOperation:
        raise ValueError("Escribe un precio valido, solo numeros.")
    if (not valor.is_finite() or valor <= 0 or valor >= MAX_PRECIO
            or valor.as_tuple().exponent < -2):
        raise ValueError("El precio debe ser mayor a cero, menor a 1,000,000 y con maximo 2 decimales.")
    return valor


def parsear_capacidad(texto):
    texto = (texto or "").strip()
    try:
        valor = Decimal(texto)
    except InvalidOperation:
        raise ValueError("Escribe la capacidad en litros, solo numeros.")
    if (not valor.is_finite() or valor <= 0 or valor >= Decimal("10000")
            or valor.as_tuple().exponent < -3):
        raise ValueError("La capacidad debe ser mayor a cero y con maximo 3 decimales.")
    return valor


def registrar_precios(app, db, PT, ClienteCRM):
    class PresentacionPT(db.Model):
        __tablename__ = "pt_presentaciones"
        __table_args__ = (db.UniqueConstraint("producto_id", "nombre", name="uq_presentacion_producto_nombre"),)
        id = db.Column(db.Integer, primary_key=True)
        producto_id = db.Column(db.Integer, db.ForeignKey("productos_terminados.id"), nullable=False, index=True)
        producto = db.relationship(PT)
        nombre = db.Column(db.String(40), nullable=False)
        capacidad = db.Column(db.Numeric(18, 3), nullable=False)
        precio_base = db.Column(db.Numeric(18, 2))
        activa = db.Column(db.Boolean, nullable=False, default=True)
        creada_en = db.Column(db.DateTime, default=datetime.utcnow)

    class ListaPrecios(db.Model):
        __tablename__ = "listas_precios"
        id = db.Column(db.Integer, primary_key=True)
        nombre = db.Column(db.String(100), unique=True, nullable=False)
        descripcion = db.Column(db.String(300))
        activa = db.Column(db.Boolean, nullable=False, default=True)
        creada_en = db.Column(db.DateTime, default=datetime.utcnow)

    class ListaPreciosItem(db.Model):
        __tablename__ = "listas_precios_items"
        __table_args__ = (db.UniqueConstraint("lista_id", "presentacion_id", name="uq_lista_presentacion"),)
        id = db.Column(db.Integer, primary_key=True)
        lista_id = db.Column(db.Integer, db.ForeignKey("listas_precios.id"), nullable=False, index=True)
        presentacion_id = db.Column(db.Integer, db.ForeignKey("pt_presentaciones.id"), nullable=False, index=True)
        precio = db.Column(db.Numeric(18, 2), nullable=False)

    class ClienteListaPrecios(db.Model):
        __tablename__ = "clientes_lista_precios"
        id = db.Column(db.Integer, primary_key=True)
        cliente_id = db.Column(db.Integer, db.ForeignKey("crm_clientes.id"), unique=True, nullable=False)
        cliente = db.relationship(ClienteCRM)
        lista_id = db.Column(db.Integer, db.ForeignKey("listas_precios.id"), nullable=False, index=True)
        lista = db.relationship(ListaPrecios)

    # ---------- reglas de negocio ----------
    def lista_de_cliente(cliente_id):
        if not cliente_id:
            return None
        asignacion = ClienteListaPrecios.query.filter_by(cliente_id=cliente_id).first()
        if asignacion and asignacion.lista.activa:
            return asignacion.lista
        return None

    def resolver_precio(presentacion, cliente_id=None):
        """(precio, origen). origen: 'lista', 'base' o None si no hay precio."""
        lista = lista_de_cliente(cliente_id)
        if lista:
            item = ListaPreciosItem.query.filter_by(lista_id=lista.id, presentacion_id=presentacion.id).first()
            if item:
                return item.precio, "lista"
        if presentacion.precio_base is not None:
            return presentacion.precio_base, "base"
        return None, None

    def presentaciones_activas():
        return (PresentacionPT.query.join(PT, PresentacionPT.producto_id == PT.id)
                .filter(PresentacionPT.activa.is_(True))
                .order_by(PT.nombre, PresentacionPT.capacidad).all())

    def autorizado_admin():
        return bool(session.get("admin_logueado"))

    def autorizado_api():
        return bool(session.get("admin_logueado") or session.get("crm_logueado"))

    def volver(endpoint, **valores):
        return redirect(url_for(endpoint, **valores))

    # ---------- presentaciones ----------
    @app.route("/admin/precios/presentaciones")
    def precios_presentaciones():
        if not autorizado_admin():
            return redirect(url_for("login_admin"))
        productos = PT.query.order_by(PT.nombre).all()
        por_producto = {p.id: [] for p in productos}
        for pres in PresentacionPT.query.order_by(PresentacionPT.capacidad).all():
            por_producto.setdefault(pres.producto_id, []).append(pres)
        return render_template("precios_presentaciones.html", productos=productos, por_producto=por_producto,
                               estandar=[n for n, _ in PRESENTACIONES_ESTANDAR])

    @app.route("/admin/precios/presentaciones", methods=["POST"])
    def precios_presentacion_crear():
        if not autorizado_admin():
            return redirect(url_for("login_admin"))
        try:
            producto = db.session.get(PT, int(request.form.get("producto_id") or 0))
            if not producto:
                raise ValueError("Selecciona un producto terminado.")
            nombre = " ".join((request.form.get("nombre") or "").split())
            if not nombre or len(nombre) > 40:
                raise ValueError("Escribe el nombre de la presentacion (ej. 5 L), maximo 40 caracteres.")
            capacidad = parsear_capacidad(request.form.get("capacidad"))
            precio = parsear_precio(request.form.get("precio_base"))
            existe = (PresentacionPT.query.filter(PresentacionPT.producto_id == producto.id,
                                                  db.func.lower(PresentacionPT.nombre) == nombre.lower()).first())
            if existe:
                raise ValueError(f"{producto.nombre} ya tiene la presentacion {nombre}.")
            db.session.add(PresentacionPT(producto_id=producto.id, nombre=nombre, capacidad=capacidad, precio_base=precio))
            db.session.commit()
            flash(f"Presentacion {nombre} agregada a {producto.nombre}.", "success")
        except (ValueError, IntegrityError) as e:
            db.session.rollback()
            flash(str(e) if isinstance(e, ValueError) else "No se pudo guardar la presentacion.", "danger")
        return volver("precios_presentaciones")

    @app.route("/admin/precios/presentaciones/estandar/<int:id_producto>", methods=["POST"])
    def precios_presentaciones_estandar(id_producto):
        if not autorizado_admin():
            return redirect(url_for("login_admin"))
        producto = db.get_or_404(PT, id_producto)
        existentes = {p.nombre.lower() for p in PresentacionPT.query.filter_by(producto_id=producto.id)}
        nuevas = 0
        for nombre, capacidad in PRESENTACIONES_ESTANDAR:
            if nombre.lower() in existentes:
                continue
            # Si el producto ya traia su presentacion y precio en el catalogo, se aprovecha.
            precio = producto.precio if (producto.presentacion or "").strip().lower() == nombre.lower() else None
            db.session.add(PresentacionPT(producto_id=producto.id, nombre=nombre, capacidad=capacidad, precio_base=precio))
            nuevas += 1
        db.session.commit()
        flash(f"Se agregaron {nuevas} presentaciones estandar a {producto.nombre}." if nuevas
              else f"{producto.nombre} ya tiene las presentaciones estandar.", "success" if nuevas else "info")
        return volver("precios_presentaciones")

    @app.route("/admin/precios/presentaciones/estandar-todos", methods=["POST"])
    def precios_presentaciones_estandar_todos():
        """Agrega 1 L, 5 L y 20 L a todos los productos activos que no las tengan (sin precio)."""
        if not autorizado_admin():
            return redirect(url_for("login_admin"))
        existentes = {(p.producto_id, p.nombre.lower()) for p in PresentacionPT.query}
        nuevas = 0
        for producto in PT.query.filter_by(estatus="Activo").order_by(PT.nombre):
            for nombre, capacidad in PRESENTACIONES_ESTANDAR:
                if (producto.id, nombre.lower()) in existentes:
                    continue
                precio = producto.precio if (producto.presentacion or "").strip().lower() == nombre.lower() else None
                db.session.add(PresentacionPT(producto_id=producto.id, nombre=nombre, capacidad=capacidad, precio_base=precio))
                nuevas += 1
        db.session.commit()
        flash(f"Se agregaron {nuevas} presentaciones (1 L, 5 L y 20 L) a los productos activos." if nuevas
              else "Todos los productos activos ya tienen 1 L, 5 L y 20 L.", "success" if nuevas else "info")
        return volver("precios_presentaciones")

    @app.route("/admin/precios/presentaciones/<int:id_presentacion>/editar", methods=["POST"])
    def precios_presentacion_editar(id_presentacion):
        if not autorizado_admin():
            return redirect(url_for("login_admin"))
        pres = db.get_or_404(PresentacionPT, id_presentacion)
        try:
            pres.precio_base = parsear_precio(request.form.get("precio_base"))
            pres.activa = request.form.get("activa") == "1"
            db.session.commit()
            flash(f"{pres.producto.nombre} {pres.nombre} actualizada.", "success")
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "danger")
        return volver("precios_presentaciones")

    # ---------- listas ----------
    @app.route("/admin/precios")
    def precios_listas():
        if not autorizado_admin():
            return redirect(url_for("login_admin"))
        listas = ListaPrecios.query.order_by(ListaPrecios.nombre).all()
        resumen = []
        for lista in listas:
            resumen.append({
                "lista": lista,
                "precios": ListaPreciosItem.query.filter_by(lista_id=lista.id).count(),
                "clientes": ClienteListaPrecios.query.filter_by(lista_id=lista.id).count(),
            })
        sin_lista = ClienteCRM.query.count() - ClienteListaPrecios.query.count()
        return render_template("precios.html", resumen=resumen, sin_lista=sin_lista,
                               total_presentaciones=PresentacionPT.query.filter_by(activa=True).count())

    @app.route("/admin/precios", methods=["POST"])
    def precios_lista_crear():
        if not autorizado_admin():
            return redirect(url_for("login_admin"))
        nombre = " ".join((request.form.get("nombre") or "").split())
        descripcion = (request.form.get("descripcion") or "").strip()[:300] or None
        if not nombre or len(nombre) > 100:
            flash("Escribe el nombre de la lista (maximo 100 caracteres).", "danger")
            return volver("precios_listas")
        if ListaPrecios.query.filter(db.func.lower(ListaPrecios.nombre) == nombre.lower()).first():
            flash(f"Ya existe una lista llamada {nombre}.", "danger")
            return volver("precios_listas")
        try:
            ajuste = Decimal((request.form.get("ajuste") or "0").strip().replace(",", "").replace("%", "") or "0")
            if not ajuste.is_finite() or ajuste <= Decimal("-100") or ajuste > Decimal("1000"):
                raise InvalidOperation
        except InvalidOperation:
            flash("Escribe el porcentaje solo con números, por ejemplo -10 o 5.", "danger")
            return volver("precios_listas")
        origen = (request.form.get("copiar_de") or "").strip()
        lista = ListaPrecios(nombre=nombre, descripcion=descripcion)
        db.session.add(lista)
        db.session.flush()
        copiados = 0
        if origen:
            propios = ({i.presentacion_id: i.precio for i in ListaPreciosItem.query.filter_by(lista_id=int(origen))}
                       if origen.isdigit() else {})
            for pres in presentaciones_activas():
                precio = propios.get(pres.id, pres.precio_base)
                if precio is None:
                    continue
                nuevo = (Decimal(precio) * (Decimal(100) + ajuste) / Decimal(100)).quantize(Decimal("0.01"))
                if nuevo > 0:
                    db.session.add(ListaPreciosItem(lista_id=lista.id, presentacion_id=pres.id, precio=nuevo))
                    copiados += 1
        db.session.commit()
        flash(f"Lista {nombre} creada" + (f" con {copiados} precios." if copiados else ". Captura sus precios abajo."), "success")
        return volver("precios_lista", id_lista=lista.id)

    @app.route("/admin/precios/<int:id_lista>")
    def precios_lista(id_lista):
        if not autorizado_admin():
            return redirect(url_for("login_admin"))
        lista = db.get_or_404(ListaPrecios, id_lista)
        presentaciones = presentaciones_activas()
        precios = {i.presentacion_id: i.precio for i in ListaPreciosItem.query.filter_by(lista_id=lista.id)}
        asignaciones = (ClienteListaPrecios.query.filter_by(lista_id=lista.id).join(ClienteCRM).order_by(ClienteCRM.nombre).all())
        actuales = {a.cliente_id: a.lista.nombre for a in ClienteListaPrecios.query.all()}
        clientes = [(c, actuales.get(c.id)) for c in ClienteCRM.query.order_by(ClienteCRM.nombre).all()
                    if actuales.get(c.id) != lista.nombre]
        return render_template("precios_lista.html", lista=lista, presentaciones=presentaciones, precios=precios,
                               asignaciones=asignaciones, clientes=clientes)

    @app.route("/admin/precios/<int:id_lista>/guardar", methods=["POST"])
    def precios_lista_guardar(id_lista):
        if not autorizado_admin():
            return redirect(url_for("login_admin"))
        lista = db.get_or_404(ListaPrecios, id_lista)
        try:
            nombre = " ".join((request.form.get("nombre") or "").split()) or lista.nombre
            if nombre.lower() != lista.nombre.lower() and ListaPrecios.query.filter(
                    db.func.lower(ListaPrecios.nombre) == nombre.lower()).first():
                raise ValueError(f"Ya existe una lista llamada {nombre}.")
            nuevos = {}
            for pres in PresentacionPT.query.all():
                nuevos[pres.id] = parsear_precio(request.form.get(f"precio_{pres.id}"))
            lista.nombre = nombre[:100]
            lista.descripcion = (request.form.get("descripcion") or "").strip()[:300] or None
            lista.activa = request.form.get("activa") == "1"
            existentes = {i.presentacion_id: i for i in ListaPreciosItem.query.filter_by(lista_id=lista.id)}
            for pres_id, precio in nuevos.items():
                item = existentes.get(pres_id)
                if precio is None:
                    if item:
                        db.session.delete(item)  # sin precio propio: vuelve al precio base
                elif item:
                    item.precio = precio
                else:
                    db.session.add(ListaPreciosItem(lista_id=lista.id, presentacion_id=pres_id, precio=precio))
            db.session.commit()
            flash("Lista guardada.", "success")
        except (ValueError, IntegrityError) as e:
            db.session.rollback()
            flash(str(e) if isinstance(e, ValueError) else "No se pudo guardar la lista.", "danger")
        return volver("precios_lista", id_lista=id_lista)

    @app.route("/admin/precios/<int:id_lista>/clientes", methods=["POST"])
    def precios_lista_asignar(id_lista):
        if not autorizado_admin():
            return redirect(url_for("login_admin"))
        lista = db.get_or_404(ListaPrecios, id_lista)
        cliente = db.session.get(ClienteCRM, int(request.form.get("cliente_id") or 0))
        if not cliente:
            flash("Selecciona un cliente.", "danger")
            return volver("precios_lista", id_lista=id_lista)
        asignacion = ClienteListaPrecios.query.filter_by(cliente_id=cliente.id).first()
        if asignacion:
            asignacion.lista_id = lista.id
        else:
            db.session.add(ClienteListaPrecios(cliente_id=cliente.id, lista_id=lista.id))
        db.session.commit()
        flash(f"{cliente.nombre_completo} ahora usa la lista {lista.nombre}.", "success")
        return volver("precios_lista", id_lista=id_lista)

    @app.route("/admin/precios/<int:id_lista>/clientes/<int:id_cliente>/quitar", methods=["POST"])
    def precios_lista_quitar(id_lista, id_cliente):
        if not autorizado_admin():
            return redirect(url_for("login_admin"))
        asignacion = ClienteListaPrecios.query.filter_by(cliente_id=id_cliente, lista_id=id_lista).first()
        if asignacion:
            db.session.delete(asignacion)
            db.session.commit()
            flash("Cliente quitado de la lista; volvera a usar el precio base.", "success")
        return volver("precios_lista", id_lista=id_lista)

    @app.route("/admin/precios/<int:id_lista>/pdf")
    def precios_lista_pdf(id_lista):
        if not autorizado_admin():
            return redirect(url_for("login_admin"))
        lista = db.get_or_404(ListaPrecios, id_lista)
        propios = {i.presentacion_id: i.precio for i in ListaPreciosItem.query.filter_by(lista_id=lista.id)}
        filas = []
        for pres in presentaciones_activas():
            precio = propios.get(pres.id, pres.precio_base)
            if precio is None:
                continue
            filas.append([pres.producto.nombre, pres.nombre, f"${precio:,.2f}"])
        buffer = generar_pdf(
            f"Lista de precios - {lista.nombre}", lista.descripcion or "Precios por presentacion",
            [{"titulo": None, "columnas": ["Producto", "Presentación", "Precio"], "filas": filas,
              "anchos": [10.5, 4, 3.4], "vacio": "Esta lista aun no tiene precios."}],
        )
        return respuesta_pdf(buffer, f"lista-precios-{lista.id}.pdf")

    # ---------- API para el CRM ----------
    @app.route("/api/precios/producto/<int:id_producto>")
    def api_precios_producto(id_producto):
        if not autorizado_api():
            return jsonify({"error": "Necesitas iniciar sesion."}), 403
        cliente_id = request.args.get("cliente_id", type=int)
        lista = lista_de_cliente(cliente_id)
        respuesta = []
        for pres in (PresentacionPT.query.filter_by(producto_id=id_producto, activa=True)
                     .order_by(PresentacionPT.capacidad).all()):
            precio, origen = resolver_precio(pres, cliente_id)
            respuesta.append({"presentacion_id": pres.id, "nombre": pres.nombre,
                              "precio": float(precio) if precio is not None else None, "origen": origen})
        return jsonify({"lista": lista.nombre if lista else None, "presentaciones": respuesta})

    @app.route("/api/precios/listas")
    def api_precios_listas():
        """Tipos de venta (listas activas) para elegir al registrar un cliente o un pedido."""
        if not autorizado_api():
            return jsonify({"error": "Necesitas iniciar sesion."}), 403
        listas = ListaPrecios.query.filter_by(activa=True).order_by(ListaPrecios.id).all()
        return jsonify([{"id": l.id, "nombre": l.nombre} for l in listas])

    @app.route("/api/precios/cliente/<int:id_cliente>/lista", methods=["POST"])
    def api_precios_cliente_lista(id_cliente):
        """Cambia el tipo de venta de un cliente. lista_id vacio = precio general."""
        if not autorizado_api():
            return jsonify({"error": "Necesitas iniciar sesion."}), 403
        cliente = db.session.get(ClienteCRM, id_cliente)
        if not cliente:
            return jsonify({"error": "Cliente no encontrado."}), 404
        valor = (request.get_json(silent=True) or {}).get("lista_id")
        asignacion = ClienteListaPrecios.query.filter_by(cliente_id=cliente.id).first()
        if valor in (None, ""):
            if asignacion:
                db.session.delete(asignacion)
            db.session.commit()
            return jsonify({"lista": None})
        try:
            lista = db.session.get(ListaPrecios, int(valor))
        except (TypeError, ValueError):
            lista = None
        if not lista or not lista.activa:
            return jsonify({"error": "Ese tipo de venta no existe."}), 400
        if asignacion:
            asignacion.lista_id = lista.id
        else:
            db.session.add(ClienteListaPrecios(cliente_id=cliente.id, lista_id=lista.id))
        db.session.commit()
        return jsonify({"lista": lista.nombre})

    @app.route("/api/precios/catalogo")
    def api_precios_catalogo():
        """Catalogo para la pantalla de nuevo pedido: productos activos con presentaciones y precios del cliente."""
        if not autorizado_api():
            return jsonify({"error": "Necesitas iniciar sesion."}), 403
        cliente_id = request.args.get("cliente_id", type=int)
        lista = lista_de_cliente(cliente_id)
        por_producto = {}
        for pres in (PresentacionPT.query.filter_by(activa=True).order_by(PresentacionPT.capacidad).all()):
            precio, origen = resolver_precio(pres, cliente_id)
            por_producto.setdefault(pres.producto_id, []).append({
                "presentacion_id": pres.id, "nombre": pres.nombre,
                "precio": float(precio) if precio is not None else None, "origen": origen})
        productos = []
        for p in PT.query.filter_by(estatus="Activo").order_by(PT.nombre).all():
            productos.append({
                "id": p.id, "sku": p.sku, "nombre": p.nombre, "categoria": p.categoria,
                "precio": float(p.precio) if p.precio is not None else None,
                "imagen": url_for("api_precios_imagen", id_producto=p.id) if p.imagen_tipo else None,
                "presentaciones": por_producto.get(p.id, [])})
        return jsonify({"lista": lista.nombre if lista else None, "productos": productos})

    @app.route("/api/precios/producto/<int:id_producto>/imagen")
    def api_precios_imagen(id_producto):
        if not autorizado_api():
            return jsonify({"error": "Necesitas iniciar sesion."}), 403
        producto = db.session.get(PT, id_producto)
        if not producto or not producto.imagen:
            abort(404)
        respuesta = send_file(BytesIO(producto.imagen), mimetype=producto.imagen_tipo, max_age=300)
        respuesta.headers["X-Content-Type-Options"] = "nosniff"
        return respuesta

    return PresentacionPT, ListaPrecios, ListaPreciosItem, ClienteListaPrecios, resolver_precio
