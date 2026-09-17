"""Panel de salud operativa para validar la columna vertebral ERP/CRM."""
from collections import defaultdict
from decimal import Decimal
from functools import wraps

from flask import jsonify, redirect, render_template, session
from sqlalchemy import inspect


# Documentacion viva del ciclo de un pedido: describe, paso a paso, que hace
# cada rol del CRM y que se escribe realmente en las tablas del ERP. El
# contenido es literal al codigo de crm.py; si cambia una regla o una tabla
# ahi, se actualiza aqui tambien.
PASOS_CICLO_PEDIDO = [
    {
        "id": "alta",
        "numero": "1",
        "titulo": "Ventas crea el pedido",
        "rol": "ventas.nyds",
        "ejecucion": "se ejecuta en el CRM",
        "estado": "Recibido",
        "resumen": "Cliente nuevo o existente buscado por telefono o RFC, domicilio de entrega y lineas de producto terminado con cantidad y precio.",
        "que_hace": "Se identifica al cliente: existente buscado por telefono o RFC, o alta nueva. Se registra el domicilio de entrega con zona, cobertura y coordenadas opcionales, y se capturan las lineas de producto terminado con cantidad y precio. El importe total se calcula sumando cantidad × precio.",
        "tablas": [
            ("crm_clientes", "Busqueda por telefono o RFC; alta solo si no existe, si existe se reutiliza el cliente con su canal de origen."),
            ("crm_cliente_domicilios", "Alta del domicilio con alias, direccion, referencias, zona, cobertura y lat/lng opcionales."),
            ("crm_pedidos", "Folio secuencial PED-000001, estado Recibido, canal, forma de pago, importe total y token de idempotencia."),
            ("crm_pedido_lineas", "Una linea por producto terminado con cantidad, precio unitario, notas y modo_reserva = Sin reserva."),
            ("crm_eventos", "Evento «Pedido recibido» con el canal en el payload."),
        ],
        "reglas": [
            "Solo el rol ventas puede crear pedidos; cualquier otro rol recibe 403 (el supervisor puede siempre).",
            "Solo se aceptan productos terminados con estatus Activo.",
            "El pedido debe sumar un importe mayor a cero y cantidades positivas validas.",
            "Reenviar el mismo token devuelve el pedido ya creado en lugar de duplicarlo.",
        ],
        "metodo": "POST",
        "ruta": "/api/crm/pedidos",
        "archivo_funcion": "crm.py · api_crm_pedidos()",
    },
    {
        "id": "reserva",
        "numero": "2",
        "titulo": "El sistema evalua inventario al confirmar",
        "rol": "ventas.nyds",
        "ejecucion": "CRM · consulta inventario del ERP",
        "estado": None,
        "resumen": "Al confirmar, reservar_pedido() recorre cada linea del pedido y resuelve una de tres salidas posibles.",
        "que_hace": "Si el pedido exige pago antes de prepararse y aun tiene saldo, se detiene en Pendiente de pago. Si no, reservar_pedido() recorre cada linea: primero intenta reservar producto terminado ya existente en Almacen PT (descontando lo que otros pedidos ya tengan reservado); si no alcanza, busca la receta activa del producto, calcula la materia prima necesaria segun su rendimiento y, si hay suficiente materia prima libre, la reserva y genera una orden de llenado pendiente; si no alcanza ni producto ni materia prima, esa linea queda sin reserva.",
        "ramas": [
            {"letra": "A", "titulo": "Hay producto terminado disponible", "texto": "Reserva directa del producto terminado. El pedido pasa a Confirmado y no genera orden de llenado."},
            {"letra": "B", "titulo": "No hay producto, si alcanza la materia prima", "texto": "Descuenta materia prima segun la receta activa, reserva sus componentes y genera una orden de llenado pendiente."},
            {"letra": "C", "titulo": "No alcanza ni producto ni materia prima", "texto": "La linea queda sin reserva y, si ninguna linea del pedido pudo resolverse, el pedido pasa a Pendiente de produccion."},
        ],
        "tablas": [
            ("crm_reservas_erp", "Una reserva Activa por linea: tipo Producto terminado (contra Almacen PT) o tipo Materia prima (calculada desde la receta), con el folio del pedido como referencia."),
            ("crm_ordenes_llenado", "Se crea una orden Pendiente por cada linea que se resolvio con materia prima en vez de producto terminado ya existente."),
            ("crm_pedido_lineas", "modo_reserva se actualiza a Producto terminado, Componentes reservados o queda Sin reserva."),
            ("crm_eventos", "Evento Reserva ERP o Faltantes segun el resultado."),
        ],
        "reglas": [
            "El domicilio debe tener cobertura; si no, la confirmacion se rechaza.",
            "Si el pedido exige pago antes de prepararse y aun hay saldo, pasa a Pendiente de pago y no reserva nada todavia.",
            "Nunca se generan reservas negativas: si ninguna fuente alcanza, la linea queda registrada como faltante.",
            "Un pedido solo puede confirmarse desde Recibido, Pendiente de pago, Pendiente de produccion o Confirmado.",
        ],
        "metodo": "POST",
        "ruta": "/api/crm/pedidos/<id_pedido>/confirmar",
        "archivo_funcion": "crm.py · api_crm_confirmar_pedido() / reservar_pedido()",
    },
    {
        "id": "llenado",
        "numero": "3",
        "titulo": "Preparacion cierra la orden de llenado",
        "rol": "preparacion.nyds",
        "ejecucion": "CRM · escribe en el ERP",
        "estado": None,
        "resumen": "Consume la materia prima reservada, da de alta el producto terminado resultante: nuevo inventario real del ERP.",
        "que_hace": "Preparacion cierra una orden de llenado pendiente. Por cada reserva de materia prima ligada a esa orden se valida que siga habiendo existencia suficiente y se registra un ajuste de inventario de salida (consumo). Se da de alta el producto terminado resultante en Almacen PT y se genera automaticamente la reserva de ese producto terminado para el pedido.",
        "tablas": [
            ("inventario_mp_ajustes", "Ajuste «Ajuste de salida» por cada materia prima reservada, con referencia CRM-LLENADO-<id> y motivo de consumo por llenado."),
            ("crm_reservas_erp", "Las reservas de materia prima pasan a Consumida; se crea una nueva reserva Activa de tipo Producto terminado."),
            ("crm_inventario_pt_movimientos", "Movimiento «Alta por llenado» que da de alta el producto terminado en Almacen PT."),
            ("crm_ordenes_llenado", "La orden pasa de Pendiente a Completada con fecha y token de cierre."),
            ("crm_pedido_lineas", "modo_reserva pasa a Producto terminado."),
            ("crm_eventos", "Evento Llenado completado."),
        ],
        "reglas": [
            "Solo el rol preparacion puede completar ordenes de llenado (el supervisor puede siempre).",
            "Una orden ya Completada no puede cerrarse otra vez.",
            "Si la materia prima reservada ya no alcanza al momento de cerrar, se rechaza en vez de dejar inventario negativo.",
            "Reenviar el mismo token no duplica el movimiento de alta.",
        ],
        "metodo": "POST",
        "ruta": "/api/crm/ordenes-llenado/<id_orden>/completar",
        "archivo_funcion": "crm.py · api_crm_completar_llenado()",
    },
    {
        "id": "listo",
        "numero": "4",
        "titulo": "El pedido pasa a Listo para entrega",
        "rol": "preparacion.nyds",
        "ejecucion": "se ejecuta en CRM",
        "estado": None,
        "resumen": "Cambio de estatus operativo, sin movimiento de inventario. Las reservas de producto terminado siguen activas.",
        "que_hace": "Cambio de estatus operativo puro: no se toca inventario ni reservas. Las reservas de producto terminado activas para ese pedido permanecen igual.",
        "tablas": [
            ("crm_pedidos", "El campo estado pasa a Listo para entrega."),
            ("crm_eventos", "Evento Estado Listo para entrega, con el comentario opcional que capture preparacion."),
        ],
        "reglas": [
            "Solo preparacion puede mover el pedido a En preparacion o Listo para entrega (el supervisor puede siempre).",
            "El estado Entregado nunca se asigna por esta via: solo el endpoint de entrega con evidencia puede cerrarlo.",
        ],
        "metodo": "POST",
        "ruta": "/api/crm/pedidos/<id_pedido>/estado",
        "archivo_funcion": "crm.py · api_crm_estado_pedido()",
    },
    {
        "id": "ruta",
        "numero": "5",
        "titulo": "Reparto lo marca En ruta",
        "rol": "reparto.nyds",
        "ejecucion": "CRM · escribe en el ERP",
        "estado": None,
        "resumen": "El producto sale de Almacen PT y entra a la tabla En transito con dos movimientos espejo.",
        "que_hace": "Solo un pedido Confirmado o Listo para entrega puede salir a ruta. mover_producto_a_transito() valida que exista fisicamente el producto terminado reservado en Almacen PT y, si es asi, registra dos movimientos espejo por la misma cantidad: una salida de Almacen PT y una entrada a En transito.",
        "tablas": [
            ("crm_inventario_pt_movimientos", "Dos movimientos por reserva: Salida a ruta (negativo, Almacen PT) y Entrada a transito (positivo, En transito), ambos con el folio del pedido como referencia."),
            ("crm_pedidos", "El campo estado pasa a En ruta."),
            ("crm_eventos", "Evento Estado En ruta."),
        ],
        "reglas": [
            "Solo reparto puede mover a En ruta, Intento fallido o Devuelto (el supervisor puede siempre).",
            "Si no hay producto terminado reservado o el saldo fisico en Almacen PT no alcanza, se rechaza en vez de generar inventario negativo.",
            "Los movimientos usan un token derivado del pedido y la reserva, asi que reintentar no duplica el par de movimientos.",
        ],
        "metodo": "POST",
        "ruta": "/api/crm/pedidos/<id_pedido>/estado",
        "archivo_funcion": "crm.py · api_crm_estado_pedido() / mover_producto_a_transito()",
    },
    {
        "id": "entrega",
        "numero": "6",
        "titulo": "Reparto registra la entrega con evidencia obligatoria",
        "rol": "reparto.nyds",
        "ejecucion": "CRM · escribe en el ERP",
        "estado": "Entregado",
        "resumen": "Foto, firma, OTP o receptor. Solo entonces se descuenta el producto terminado definitivamente y el pedido pasa a Entregado.",
        "que_hace": "Reparto confirma o falla la entrega. Si se confirma, exige al menos una evidencia (OTP, receptor, firma o foto); si el pedido seguia Confirmado o Listo para entrega (no paso antes por En ruta), primero ejecuta el mismo movimiento a transito. Solo entonces descuenta definitivamente el producto terminado desde En transito y cierra las reservas como Consumidas.",
        "tablas": [
            ("crm_entrega_evidencias", "Registro de evidencia: OTP, receptor, firma, foto, coordenadas e incidencia."),
            ("crm_inventario_pt_movimientos", "Movimiento Entrega confirmada (negativo, En transito) por cada reserva de producto terminado."),
            ("crm_reservas_erp", "Las reservas de producto terminado pasan a Consumida."),
            ("crm_pedidos", "El campo estado pasa a Entregado (o Intento fallido si no se concreto)."),
            ("crm_eventos", "Evento Entrega confirmada o Intento fallido."),
        ],
        "reglas": [
            "Solo reparto puede registrar la entrega (el supervisor puede siempre).",
            "Una entrega marcada como concretada sin ninguna evidencia se rechaza.",
            "Solo un pedido Confirmado, Listo para entrega o En ruta puede entregarse.",
        ],
        "metodo": "POST",
        "ruta": "/api/crm/pedidos/<id_pedido>/entrega",
        "archivo_funcion": "crm.py · api_crm_entrega_pedido()",
    },
]

PASO_LATERAL_CICLO_PEDIDO = {
    "id": "cobranza",
    "numero": "S",
    "titulo": "Cobranza registra pagos contra el saldo",
    "rol": "caja.nyds",
    "ejecucion": "se ejecuta en CRM",
    "estado": None,
    "resumen": "Pagos parciales en cualquier punto del ciclo. Si el pedido exige pago antes de prepararse, bloquea la confirmacion y la salida a ruta.",
    "que_hace": "Cobranza registra un pago parcial o total contra el saldo pendiente del pedido (importe_total menos importe_pagado). El pago no puede superar el saldo.",
    "tablas": [
        ("crm_pedido_pagos", "Un registro de pago con monto, metodo, referencia, usuario y token de idempotencia."),
        ("crm_pedidos", "importe_pagado se incrementa; el saldo se recalcula."),
        ("crm_eventos", "Evento Pago recibido con el metodo."),
    ],
    "reglas": [
        "Solo cobranza puede registrar pagos (el supervisor puede siempre).",
        "El pago no puede superar el saldo pendiente del pedido.",
        "Si el pedido exige pago antes de prepararse, mientras haya saldo la confirmacion queda detenida en Pendiente de pago.",
        "Reenviar el mismo token no duplica el pago.",
    ],
    "metodo": "POST",
    "ruta": "/api/crm/pedidos/<id_pedido>/pagos",
    "archivo_funcion": "crm.py · api_crm_pago_pedido()",
}

RAMAS_ALTERNAS_CICLO_PEDIDO = [
    {
        "id": "cancelado",
        "simbolo": "✕",
        "titulo": "Cancelado",
        "rol": "ventas.nyds",
        "resumen": "La cancela ventas. Libera reservas pero no genera materia prima.",
        "que_hace": "Ventas cancela el pedido desde cualquier estado previo al cierre. liberar_reservas() marca como Liberada toda reserva Activa del pedido; el inventario reservado vuelve a estar disponible para otros pedidos. No revierte materia prima ya consumida en un llenado previo.",
        "tablas": [
            ("crm_reservas_erp", "Las reservas Activas del pedido pasan a Liberada."),
            ("crm_pedidos", "El campo estado pasa a Cancelado."),
            ("crm_eventos", "Evento Estado Cancelado."),
        ],
        "reglas": ["Solo ventas puede cancelar (el supervisor puede siempre)."],
        "metodo": "POST",
        "ruta": "/api/crm/pedidos/<id_pedido>/estado",
        "archivo_funcion": "crm.py · api_crm_estado_pedido() / liberar_reservas()",
    },
    {
        "id": "devuelto",
        "simbolo": "↶",
        "titulo": "Devuelto",
        "rol": "reparto.nyds",
        "resumen": "Lo ejecuta reparto. Libera reservas activas; los movimientos de inventario ya registrados no se revierten solos.",
        "que_hace": "Reparto marca el pedido como devuelto. Igual que en cancelacion, se liberan las reservas activas restantes, pero los movimientos de inventario que ya se hayan registrado (salida a ruta, entrada a transito) no se revierten automaticamente.",
        "tablas": [
            ("crm_reservas_erp", "Las reservas Activas del pedido pasan a Liberada."),
            ("crm_pedidos", "El campo estado pasa a Devuelto."),
            ("crm_eventos", "Evento Estado Devuelto."),
        ],
        "reglas": ["Solo reparto puede marcar Devuelto (el supervisor puede siempre)."],
        "metodo": "POST",
        "ruta": "/api/crm/pedidos/<id_pedido>/estado",
        "archivo_funcion": "crm.py · api_crm_estado_pedido() / liberar_reservas()",
    },
    {
        "id": "intento_fallido",
        "simbolo": "!",
        "titulo": "Intento fallido",
        "rol": "reparto.nyds",
        "resumen": "Entrega no concretada. Queda la evidencia con la incidencia y el pedido sigue en transito, reservado.",
        "que_hace": "Reparto registra que la entrega no se concreto. Se guarda la evidencia con la incidencia, el pedido pasa a Intento fallido y el producto sigue registrado como reservado en transito: no se libera automaticamente ni se descuenta.",
        "tablas": [
            ("crm_entrega_evidencias", "Registro de evidencia con la incidencia reportada."),
            ("crm_pedidos", "El campo estado pasa a Intento fallido."),
            ("crm_eventos", "Evento Intento fallido."),
        ],
        "reglas": ["No se libera la reserva de producto terminado automaticamente: sigue en transito hasta un nuevo intento o una decision manual."],
        "metodo": "POST",
        "ruta": "/api/crm/pedidos/<id_pedido>/entrega",
        "archivo_funcion": "crm.py · api_crm_entrega_pedido()",
    },
]


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

    @app.route("/admin/operaciones/ciclo-pedido")
    @protegido
    def ciclo_pedido():
        return render_template(
            "ciclo_pedido.html",
            pasos=PASOS_CICLO_PEDIDO,
            paso_lateral=PASO_LATERAL_CICLO_PEDIDO,
            ramas=RAMAS_ALTERNAS_CICLO_PEDIDO,
        )
