"""Tienda en linea publica: cuentas de cliente, catalogo y carrito.

El flujo real del negocio es: el cliente arma su carrito aqui, pero quien
recibe, atiende, confirma y cierra el pedido es ventas por WhatsApp (no un
checkout 100% automatico). Por eso "Enviar pedido" no crea un PedidoCRM solo:
arma un resumen del carrito y abre un enlace de WhatsApp (wa.me) con el
mensaje listo para mandar al numero de ventas. Ya en la conversacion, ventas
cierra el trato y lo captura en el CRM (crea el PedidoCRM a mano, como
siempre); como busca al cliente por telefono, automaticamente se liga a la
misma cuenta de este cliente y aparece despues en "Mis pedidos".

El precio SIEMPRE se arma desde el catalogo (ProductoTerminado.precio),
nunca de lo que el navegador del cliente mande."""
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import quote

from flask import abort, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


CANAL_TIENDA = "Tienda en línea"
FORMAS_PAGO_TIENDA = ["Contra entrega", "Transferencia", "Efectivo", "Tarjeta"]
# Mismos valores que usa crm.py para calcular disponibilidad de producto
# terminado (fisico en almacen menos lo ya reservado por otros pedidos).
UBICACION_PT = "Almacen PT"
RESERVAS_ABIERTAS = {"Activa"}


def registrar_tienda(app, db, PT, ClienteCRM, ClienteDomicilio, PedidoCRM, PedidoLineaCRM, MovimientoPT, ReservaERP):

    class TiendaConfiguracion(db.Model):
        """Ajustes de la tienda editables desde el ERP (sin tocar codigo).
        Fila unica (id=1): por ahora solo el numero de WhatsApp de ventas al
        que llegan los pedidos armados desde el carrito."""
        __tablename__ = "tienda_configuracion"
        id = db.Column(db.Integer, primary_key=True)
        whatsapp_ventas = db.Column(db.String(20))

    def obtener_configuracion():
        config = db.session.get(TiendaConfiguracion, 1)
        if not config:
            config = TiendaConfiguracion(id=1)
            db.session.add(config)
            db.session.commit()
        return config

    def disponible_pt(producto_id):
        """Fisico en almacen menos lo que ya esta reservado por otros pedidos
        activos. Es informativo para el cliente y para ventas; el stock real
        se vuelve a revisar cuando ventas reserva el pedido en el CRM."""
        fisico = (
            db.session.query(db.func.coalesce(db.func.sum(MovimientoPT.cantidad), 0))
            .filter_by(producto_id=producto_id, ubicacion=UBICACION_PT)
            .scalar()
        )
        reservado = (
            db.session.query(db.func.coalesce(db.func.sum(ReservaERP.cantidad), 0))
            .filter(ReservaERP.tipo == "Producto terminado", ReservaERP.producto_id == producto_id, ReservaERP.estado.in_(RESERVAS_ABIERTAS))
            .scalar()
        )
        disponible = Decimal(str(fisico or 0)) - Decimal(str(reservado or 0))
        return disponible if disponible > 0 else Decimal("0")

    def normalizar_cantidad(valor):
        try:
            cantidad = Decimal(str(valor))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("Cantidad invalida.")
        if not cantidad.is_finite() or cantidad <= 0:
            raise ValueError("Cantidad invalida.")
        return cantidad

    def cliente_logueado():
        return session.get("cliente_id") is not None

    def cliente_actual():
        if not cliente_logueado():
            return None
        return db.session.get(ClienteCRM, session["cliente_id"])

    def requiere_cliente(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not cliente_logueado():
                return redirect(url_for("tienda_login", next=request.path))
            return fn(*args, **kwargs)

        return wrapper

    def carrito_actual():
        return session.get("carrito") or {}

    def guardar_carrito(carrito):
        limpio = {}
        for clave, valor in carrito.items():
            try:
                cantidad = Decimal(str(valor))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if cantidad > 0:
                limpio[clave] = str(cantidad)
        session["carrito"] = limpio
        session.modified = True

    def carrito_detallado():
        carrito = carrito_actual()
        items = []
        total = Decimal("0")
        for producto_id_str, cantidad_str in carrito.items():
            try:
                producto_id = int(producto_id_str)
                cantidad = Decimal(cantidad_str)
            except (TypeError, ValueError, InvalidOperation):
                continue
            if cantidad <= 0:
                continue
            producto = db.session.get(PT, producto_id)
            if not producto or producto.estatus != "Activo" or producto.precio is None:
                continue
            subtotal = producto.precio * cantidad
            total += subtotal
            items.append({
                "producto": producto,
                "cantidad": cantidad,
                "precio_unitario": producto.precio,
                "subtotal": subtotal,
            })
        return items, total

    @app.context_processor
    def inyectar_contexto_tienda():
        carrito = carrito_actual()
        cantidad = Decimal("0")
        for valor in carrito.values():
            try:
                cantidad += Decimal(valor)
            except (InvalidOperation, TypeError, ValueError):
                continue
        return {"cantidad_carrito": cantidad}

    # ---- Cuenta de cliente ----

    @app.route("/tienda/registro", methods=["GET", "POST"])
    def tienda_registro():
        if cliente_logueado():
            return redirect(url_for("tienda_catalogo"))
        if request.method == "GET":
            return render_template("tienda_registro.html")

        nombre = (request.form.get("nombre") or "").strip()
        telefono = (request.form.get("telefono") or "").strip()
        correo = (request.form.get("correo") or "").strip()
        password = request.form.get("password") or ""
        password_confirmar = request.form.get("password_confirmar") or ""

        error = None
        if not nombre or len(nombre) > 180:
            error = "Escribe tu nombre completo."
        elif not telefono or len(telefono) > 40:
            error = "Escribe un telefono valido."
        elif len(correo) > 180:
            error = "El correo es demasiado largo."
        elif len(password) < 6:
            error = "La contrasena debe tener al menos 6 caracteres."
        elif password != password_confirmar:
            error = "Las contrasenas no coinciden."
        if error:
            return render_template("tienda_registro.html", error=error, nombre=nombre, telefono=telefono, correo=correo), 400

        cliente = ClienteCRM.query.filter_by(telefono=telefono).first()
        if cliente and cliente.password_hash:
            return render_template(
                "tienda_registro.html",
                error="Ya existe una cuenta con ese telefono. Inicia sesion.",
                nombre=nombre, telefono=telefono, correo=correo,
            ), 400

        if cliente:
            # Un cliente con ese telefono ya existia (capturado a mano por
            # ventas); reclamamos la cuenta en vez de duplicar su historial.
            cliente.nombre = nombre or cliente.nombre
            cliente.correo = correo or cliente.correo
            cliente.password_hash = generate_password_hash(password)
        else:
            cliente = ClienteCRM(
                nombre=nombre,
                telefono=telefono,
                correo=correo,
                canal_origen=CANAL_TIENDA,
                password_hash=generate_password_hash(password),
            )
            db.session.add(cliente)
        db.session.commit()

        session["cliente_id"] = cliente.id
        session["cliente_nombre"] = cliente.nombre
        session["cliente_telefono"] = cliente.telefono
        session.permanent = True
        return redirect(url_for("tienda_catalogo"))

    @app.route("/tienda/login", methods=["GET", "POST"])
    def tienda_login():
        if cliente_logueado():
            return redirect(url_for("tienda_catalogo"))
        if request.method == "GET":
            return render_template("tienda_login.html")

        telefono = (request.form.get("telefono") or "").strip()
        password = request.form.get("password") or ""
        cliente = ClienteCRM.query.filter_by(telefono=telefono).first()
        if not cliente or not cliente.password_hash or not check_password_hash(cliente.password_hash, password):
            return render_template("tienda_login.html", error="Telefono o contrasena incorrectos.", telefono=telefono), 401

        session["cliente_id"] = cliente.id
        session["cliente_nombre"] = cliente.nombre
        session["cliente_telefono"] = cliente.telefono
        session.permanent = True
        destino = request.args.get("next") or url_for("tienda_catalogo")
        return redirect(destino)

    @app.route("/tienda/logout")
    def tienda_logout():
        for clave in ["cliente_id", "cliente_nombre", "cliente_telefono"]:
            session.pop(clave, None)
        return redirect(url_for("tienda_catalogo"))

    # ---- Catalogo publico ----

    @app.route("/tienda")
    def tienda_catalogo():
        q = (request.args.get("q") or "").strip()
        consulta = PT.query.filter(PT.estatus == "Activo", PT.precio.isnot(None))
        if q:
            like = f"%{q}%"
            consulta = consulta.filter(db.or_(PT.nombre.ilike(like), PT.categoria.ilike(like)))
        productos = consulta.order_by(PT.nombre).all()
        disponibilidad = {p.id: disponible_pt(p.id) for p in productos}
        return render_template("tienda_catalogo.html", productos=productos, q=q, disponibilidad=disponibilidad)

    @app.route("/tienda/catalogo/agregar/<int:id_producto>", methods=["POST"])
    def tienda_catalogo_agregar(id_producto):
        try:
            cantidad = normalizar_cantidad(request.form.get("cantidad") or "1")
        except ValueError:
            return redirect(url_for("tienda_catalogo"))
        producto = db.session.get(PT, id_producto)
        if not producto or producto.estatus != "Activo" or producto.precio is None:
            return redirect(url_for("tienda_catalogo"))

        disponible = disponible_pt(id_producto)
        if disponible <= 0:
            return redirect(url_for("tienda_catalogo", sin_stock=1))

        carrito = carrito_actual()
        clave = str(id_producto)
        actual = Decimal(carrito.get(clave) or "0")
        nueva = actual + cantidad
        limitado = nueva > disponible
        if limitado:
            nueva = disponible
        carrito[clave] = str(nueva)
        guardar_carrito(carrito)
        return redirect(url_for("tienda_catalogo", agregado=1, limitado=1 if limitado else 0))

    # ---- Carrito ----

    @app.route("/tienda/carrito")
    def tienda_carrito():
        items, total = carrito_detallado()
        disponibilidad = {item["producto"].id: disponible_pt(item["producto"].id) for item in items}
        return render_template("tienda_carrito.html", items=items, total=total, disponibilidad=disponibilidad)

    @app.route("/tienda/carrito/actualizar/<int:id_producto>", methods=["POST"])
    def tienda_carrito_actualizar(id_producto):
        carrito = carrito_actual()
        clave = str(id_producto)
        try:
            cantidad = Decimal(str(request.form.get("cantidad") or "0"))
        except (InvalidOperation, TypeError, ValueError):
            cantidad = None
        limitado = False
        if cantidad is None or cantidad <= 0:
            carrito.pop(clave, None)
        else:
            disponible = disponible_pt(id_producto)
            if cantidad > disponible:
                cantidad = disponible
                limitado = True
            if cantidad <= 0:
                carrito.pop(clave, None)
            else:
                carrito[clave] = str(cantidad)
        guardar_carrito(carrito)
        return redirect(url_for("tienda_carrito", limitado=1 if limitado else 0))

    @app.route("/tienda/carrito/eliminar/<int:id_producto>", methods=["POST"])
    def tienda_carrito_eliminar(id_producto):
        carrito = carrito_actual()
        carrito.pop(str(id_producto), None)
        guardar_carrito(carrito)
        return redirect(url_for("tienda_carrito"))

    # ---- Enviar pedido por WhatsApp (ventas confirma y cierra alla) ----

    def construir_mensaje_whatsapp(cliente, items, total, domicilio, forma_pago, nota, fuera_de_cobertura):
        lineas = [
            f"Hola, soy {cliente.nombre} ({cliente.telefono}). Quiero hacer este pedido "
            "desde la tienda en linea de NYDS:",
            "",
        ]
        for item in items:
            lineas.append(
                f"- {item['cantidad']:g} x {item['producto'].nombre} "
                f"(${item['precio_unitario']:.2f} c/u) = ${item['subtotal']:.2f}"
            )
        lineas.append("")
        lineas.append(f"Total: ${total:.2f}")
        direccion_texto = domicilio.direccion
        if domicilio.referencias:
            direccion_texto += f" ({domicilio.referencias})"
        lineas.append(f"Dirección de entrega: {direccion_texto}")
        lineas.append(f"Zona: {domicilio.zona}")
        lineas.append(f"Forma de pago preferida: {forma_pago}")
        if fuera_de_cobertura:
            lineas.append("Nota: esta dirección está marcada fuera de nuestra zona de cobertura habitual, favor de confirmar si se puede entregar.")
        if nota:
            lineas.append(f"Nota del cliente: {nota}")
        return "\n".join(lineas)

    @app.route("/tienda/checkout", methods=["GET", "POST"])
    @requiere_cliente
    def tienda_checkout():
        items, total = carrito_detallado()
        cliente = cliente_actual()

        if request.method == "GET":
            if not items:
                return redirect(url_for("tienda_carrito"))
            domicilios = ClienteDomicilio.query.filter_by(cliente_id=cliente.id).all()
            return render_template("tienda_checkout.html", items=items, total=total, domicilios=domicilios, formas_pago=FORMAS_PAGO_TIENDA)

        if not items:
            return redirect(url_for("tienda_carrito"))

        domicilio_id = (request.form.get("domicilio_id") or "").strip()
        direccion = (request.form.get("direccion") or "").strip()
        referencias = (request.form.get("referencias") or "").strip()
        zona = (request.form.get("zona") or "Cancun").strip() or "Cancun"
        forma_pago = request.form.get("forma_pago") or "Contra entrega"
        if forma_pago not in FORMAS_PAGO_TIENDA:
            forma_pago = "Contra entrega"
        nota = (request.form.get("nota") or "").strip()[:500]

        def reintentar(error, codigo=400):
            domicilios = ClienteDomicilio.query.filter_by(cliente_id=cliente.id).all()
            return render_template(
                "tienda_checkout.html", items=items, total=total, domicilios=domicilios,
                formas_pago=FORMAS_PAGO_TIENDA, error=error,
            ), codigo

        if not domicilio_id and not direccion:
            return reintentar("Indica una direccion de entrega.")

        fuera_de_cobertura = False
        if domicilio_id:
            domicilio = ClienteDomicilio.query.filter_by(id=domicilio_id, cliente_id=cliente.id).first()
            if not domicilio:
                return reintentar("Selecciona una direccion valida.")
            fuera_de_cobertura = not domicilio.cobertura
        else:
            # Direccion nueva: la guardamos en el perfil del cliente para que
            # la pueda reutilizar despues, igual que si la hubiera capturado
            # ventas desde el CRM.
            domicilio = ClienteDomicilio(
                cliente_id=cliente.id,
                alias="Entrega",
                direccion=direccion,
                referencias=referencias,
                zona=zona,
            )
            db.session.add(domicilio)
            db.session.commit()

        mensaje = construir_mensaje_whatsapp(cliente, items, total, domicilio, forma_pago, nota, fuera_de_cobertura)
        guardar_carrito({})

        config = obtener_configuracion()
        wa_link = f"https://wa.me/{config.whatsapp_ventas}?text={quote(mensaje)}" if config.whatsapp_ventas else None

        return render_template(
            "tienda_pedido_whatsapp.html",
            mensaje=mensaje,
            wa_link=wa_link,
            fuera_de_cobertura=fuera_de_cobertura,
        )

    # ---- Seguimiento de pedidos ya capturados por ventas ----

    @app.route("/tienda/pedido/<folio>")
    @requiere_cliente
    def tienda_pedido_confirmado(folio):
        pedido = PedidoCRM.query.filter_by(folio=folio).first()
        if not pedido or pedido.cliente_id != session.get("cliente_id"):
            abort(404)
        return render_template("tienda_pedido_confirmado.html", pedido=pedido)

    @app.route("/tienda/mis-pedidos")
    @requiere_cliente
    def tienda_mis_pedidos():
        pedidos = PedidoCRM.query.filter_by(cliente_id=session["cliente_id"]).order_by(PedidoCRM.id.desc()).all()
        return render_template("tienda_mis_pedidos.html", pedidos=pedidos)

    # ---- Configuracion (solo staff ERP: numero de WhatsApp de ventas) ----

    @app.route("/admin/tienda/configuracion", methods=["GET", "POST"])
    def tienda_configuracion_admin():
        if not session.get("admin_logueado"):
            return redirect(url_for("login_admin"))
        config = obtener_configuracion()
        if request.method == "POST":
            numero = "".join(ch for ch in (request.form.get("whatsapp_ventas") or "") if ch.isdigit())
            if not numero or not (10 <= len(numero) <= 15):
                return render_template(
                    "tienda_configuracion.html", config=config,
                    error="Escribe el numero completo con lada de pais, solo digitos (10 a 15 digitos). Ej. 529981234567.",
                ), 400
            config.whatsapp_ventas = numero
            db.session.commit()
            return redirect(url_for("tienda_configuracion_admin", guardado=1))
        return render_template("tienda_configuracion.html", config=config)

    return TiendaConfiguracion
