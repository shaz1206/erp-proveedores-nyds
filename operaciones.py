"""Panel de salud operativa para validar la columna vertebral ERP/CRM."""
from collections import defaultdict
from decimal import Decimal
from functools import wraps

from flask import jsonify, redirect, render_template, session
from sqlalchemy import inspect


def registrar_operaciones(
    app,
    db,
    Proveedor,
    MP,
    CondicionMP,
    PT,
    Receta,
    RecetaIngrediente,
    OrdenCompra,
    MovimientoMP,
    AjusteInventario,
    PedidoCRM,
    PedidoLineaCRM,
    ReservaERP,
    OrdenLlenadoCRM,
    MovimientoPT,
):
    tablas_requeridas = [
        "proveedores",
        "materias_primas",
        "materia_prima_proveedor",
        "productos_terminados",
        "recetas_borrador",
        "receta_ingredientes",
        "compra_ordenes",
        "inventario_mp_entradas",
        "inventario_mp_ajustes",
        "crm_clientes",
        "crm_pedidos",
        "crm_pedido_lineas",
        "crm_reservas_erp",
        "crm_ordenes_llenado",
        "crm_inventario_pt_movimientos",
    ]

    def protegido(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("admin_logueado"):
                return redirect("/login")
            return fn(*args, **kwargs)

        return wrapper

    def item(clave, nombre, estado, detalle, href=None):
        return {"clave": clave, "nombre": nombre, "estado": estado, "detalle": detalle, "href": href}

    def incidencia(severidad, titulo, detalle, href=None):
        return {"severidad": severidad, "titulo": titulo, "detalle": detalle, "href": href}

    def contar(modelo):
        return modelo.query.count()

    def saldo_mp_real():
        saldos = defaultdict(Decimal)
        for mov in MovimientoMP.query.all():
            recepcion = mov.recepcion
            orden = recepcion.linea.orden if recepcion and recepcion.linea else None
            if orden and str(orden.proveedor_nombre or "").startswith("DEMO - "):
                continue
            saldos[mov.mp_id] += Decimal(str(mov.cantidad or 0))
        for ajuste in AjusteInventario.query.filter_by(demo=False).all():
            saldos[ajuste.mp_id] += Decimal(str(ajuste.cantidad or 0))
        return saldos

    def saldo_pt_por_ubicacion():
        saldos = defaultdict(Decimal)
        for mov in MovimientoPT.query.all():
            saldos[(mov.producto_id, mov.ubicacion)] += Decimal(str(mov.cantidad or 0))
        return saldos

    def diagnostico_operativo():
        tablas_existentes = set(inspect(db.engine).get_table_names())
        faltantes = [tabla for tabla in tablas_requeridas if tabla not in tablas_existentes]
        incidencias = []

        for tabla in faltantes:
            incidencias.append(incidencia("bloqueo", "Tabla faltante", f"Falta crear la tabla {tabla}. Reinicia la app para ejecutar db.create_all()."))

        metricas = {
            "proveedores": contar(Proveedor),
            "materias_primas": contar(MP),
            "materias_primas_activas": MP.query.filter_by(activo=True).count(),
            "productos_terminados": contar(PT),
            "productos_terminados_activos": PT.query.filter_by(estatus="Activo").count(),
            "recetas": contar(Receta),
            "ordenes_compra": contar(OrdenCompra),
            "pedidos_crm": contar(PedidoCRM),
            "reservas_erp": contar(ReservaERP),
            "ordenes_llenado_pendientes": OrdenLlenadoCRM.query.filter_by(estado="Pendiente").count(),
        }

        if metricas["materias_primas"] == 0:
            incidencias.append(incidencia("info", "Materias primas pendientes", "Todavia no hay catalogo maestro cargado. Esto no bloquea el desarrollo estructural."))
        if metricas["productos_terminados"] == 0:
            incidencias.append(incidencia("info", "Productos terminados pendientes", "Aun no hay productos terminados para probar pedidos reales."))
        if metricas["pedidos_crm"] == 0:
            incidencias.append(incidencia("info", "CRM sin pedidos", "El flujo esta listo para probarse con pedidos demo cuando haya productos activos."))

        for mp in MP.query.filter_by(activo=True).order_by(MP.nombre_oficial).all():
            condiciones = CondicionMP.query.filter_by(id_mp=mp.id_mp, activo=True).all()
            if mp.se_compra_externamente and not condiciones:
                incidencias.append(incidencia("alerta", "Materia prima sin proveedor activo", mp.nombre_oficial, f"/admin/materias-primas/{mp.id_mp}"))
            elif mp.se_compra_externamente and not any(c.principal for c in condiciones):
                incidencias.append(incidencia("alerta", "Materia prima sin proveedor principal", mp.nombre_oficial, f"/admin/materias-primas/{mp.id_mp}"))
            if not mp.inventario_config:
                incidencias.append(incidencia("alerta", "Materia prima sin politica de inventario", mp.nombre_oficial, f"/admin/materias-primas/{mp.id_mp}"))
            if not any(ubi.activo and ubi.principal for ubi in mp.ubicaciones):
                incidencias.append(incidencia("alerta", "Materia prima sin ubicacion principal", mp.nombre_oficial, f"/admin/materias-primas/{mp.id_mp}"))

        productos_activos = PT.query.filter_by(estatus="Activo").order_by(PT.nombre).all()
        for producto in productos_activos:
            receta = (
                Receta.query.filter_by(producto_id=producto.id)
                .filter(Receta.estado.in_(["Activo", "Borrador"]))
                .order_by((Receta.estado == "Activo").desc(), Receta.version.desc(), Receta.id.desc())
                .first()
            )
            if not receta:
                incidencias.append(incidencia("alerta", "Producto activo sin receta conectada", producto.nombre, f"/admin/productos-terminados/{producto.id}"))
                continue
            if not receta.rendimiento or receta.rendimiento <= 0:
                incidencias.append(incidencia("alerta", "Receta sin rendimiento usable", receta.nombre, f"/admin/recetas/{receta.id}"))
            if not receta.lineas:
                incidencias.append(incidencia("alerta", "Receta sin ingredientes", receta.nombre, f"/admin/recetas/{receta.id}"))

        pendientes_receta = (
            RecetaIngrediente.query.filter(RecetaIngrediente.mp_id.is_(None), RecetaIngrediente.subreceta_id.is_(None))
            .order_by(RecetaIngrediente.id)
            .limit(20)
            .all()
        )
        for linea in pendientes_receta:
            incidencias.append(incidencia("alerta", "Ingrediente de receta sin liga maestra", linea.nombre_original, f"/admin/recetas/{linea.receta_id}"))

        for linea in PedidoLineaCRM.query.all():
            if not db.session.get(PT, linea.producto_id):
                incidencias.append(incidencia("bloqueo", "Linea CRM sin producto terminado", f"Linea {linea.id} del pedido {linea.pedido_id}."))

        estados_finales = {"Entregado", "Cancelado", "Devuelto"}
        for reserva in ReservaERP.query.filter_by(estado="Activa").all():
            pedido = db.session.get(PedidoCRM, reserva.pedido_id)
            if not pedido:
                incidencias.append(incidencia("bloqueo", "Reserva sin pedido CRM", f"Reserva {reserva.id}."))
            elif pedido.estado in estados_finales:
                incidencias.append(incidencia("bloqueo", "Reserva activa en pedido cerrado", f"{pedido.folio} esta en estado {pedido.estado}.", "/crm/pedidos"))

        for mp_id, saldo in saldo_mp_real().items():
            if saldo < 0:
                mp = db.session.get(MP, mp_id)
                incidencias.append(incidencia("bloqueo", "Inventario MP negativo", f"{mp.nombre_oficial if mp else mp_id}: {saldo}.", "/admin/inventario-mp"))

        for (producto_id, ubicacion), saldo in saldo_pt_por_ubicacion().items():
            if saldo < 0:
                producto = db.session.get(PT, producto_id)
                incidencias.append(incidencia("bloqueo", "Inventario PT negativo", f"{producto.nombre if producto else producto_id} en {ubicacion}: {saldo}.", "/crm/pedidos"))

        bloqueos = sum(1 for i in incidencias if i["severidad"] == "bloqueo")
        alertas = sum(1 for i in incidencias if i["severidad"] == "alerta")
        estado_general = "bloqueado" if bloqueos else "atencion" if alertas else "listo"

        secciones = [
            item("catalogos", "Catalogos maestros", "listo" if metricas["materias_primas"] or metricas["productos_terminados"] else "pendiente", "Materias primas, proveedores y producto terminado viven en tablas maestras."),
            item("abastecimiento", "Compras e inventario MP", "listo", "Ordenes de compra, recepcion y ajustes alimentan el saldo de materias primas.", "/admin/inventario-mp"),
            item("recetas", "Recetas y llenado", "atencion" if pendientes_receta else "listo", "Las recetas conectan producto terminado con materias primas o subrecetas.", "/admin/recetas"),
            item("crm", "CRM y entregas", "listo", "Pedidos, reservas, llenado, ruta y entrega usan el inventario del ERP.", "/crm/pedidos"),
            item("marca", "Diseno NYDS", "listo", "Navegacion y componentes comparten la identidad visual de nyds.mx."),
        ]

        return {
            "estado_general": estado_general,
            "bloqueos": bloqueos,
            "alertas": alertas,
            "metricas": metricas,
            "secciones": secciones,
            "incidencias": incidencias,
        }

    @app.route("/admin/operaciones")
    @protegido
    def operaciones():
        return render_template("operaciones.html", diagnostico=diagnostico_operativo())

    @app.route("/api/operaciones/salud")
    @protegido
    def api_operaciones_salud():
        return jsonify(diagnostico_operativo())
