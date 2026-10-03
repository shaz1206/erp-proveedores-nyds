"""Clientes en el ERP: balance completo por cliente.

Sustituye la vista de "productos entregados" (organizada por producto) por una
organizada por cliente: cuanto ha pedido, cuanto ha pagado, cuanto debe, que
se le entrego y quien lo recibio, sus pagos y su saldo de trueque. Todo se
calcula con los mismos datos que registra el CRM."""
from decimal import Decimal
from functools import wraps

from flask import redirect, render_template, request, session, url_for

if __package__:
    from .reportes_pdf import celda, generar_pdf, respuesta_pdf
else:
    from reportes_pdf import celda, generar_pdf, respuesta_pdf

ESTADOS_SIN_VENTA = {"Cancelado", "Devuelto"}


def _dinero(valor):
    return "${:,.2f}".format(float(valor or 0))


def registrar_clientes_erp(app, db, ClienteCRM, PedidoCRM, PagoPedidoCRM, MovimientoPT, EvidenciaEntregaCRM, MovimientoTruequeCRM):
    def protegido(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not session.get("admin_logueado"):
                return redirect(url_for("login_admin"))
            return fn(*a, **kw)

        return wrapper

    def entregas_de(pedido_ids):
        """pedido_id -> {fecha, receptor} de la entrega confirmada."""
        if not pedido_ids:
            return {}
        entregas = {}
        movimientos = (
            MovimientoPT.query.filter(MovimientoPT.tipo == "Entrega confirmada", MovimientoPT.pedido_id.in_(pedido_ids))
            .order_by(MovimientoPT.fecha)
            .all()
        )
        for mov in movimientos:
            entregas.setdefault(mov.pedido_id, {"fecha": mov.fecha, "receptor": ""})
        evidencias = (
            EvidenciaEntregaCRM.query.filter(EvidenciaEntregaCRM.pedido_id.in_(list(entregas)))
            .order_by(EvidenciaEntregaCRM.fecha)
            .all()
        ) if entregas else []
        for ev in evidencias:
            if ev.receptor and not ev.incidencia:
                entregas[ev.pedido_id]["receptor"] = ev.receptor
        return entregas

    def saldo_trueque(cliente_id):
        total = Decimal(0)
        for mov in MovimientoTruequeCRM.query.filter_by(cliente_id=cliente_id).all():
            total += mov.monto if mov.tipo == "credito" else -mov.monto
        return total

    def productos_texto(pedido):
        return ", ".join(f"{float(l.cantidad):g} {l.producto.nombre if l.producto else 'Producto'}" for l in pedido.lineas)

    def resumen_clientes(consulta=""):
        q = consulta.lower()
        filas = []
        for cliente in ClienteCRM.query.order_by(ClienteCRM.nombre).all():
            texto = " ".join(filter(None, [cliente.nombre, cliente.apellidos, cliente.telefono, cliente.rfc, cliente.correo])).lower()
            if q and q not in texto:
                continue
            pedidos = PedidoCRM.query.filter_by(cliente_id=cliente.id).order_by(PedidoCRM.fecha).all()
            ventas = [p for p in pedidos if p.estado not in ESTADOS_SIN_VENTA]
            entregas = entregas_de([p.id for p in pedidos])
            ultima = None
            for p in pedidos:
                if p.id in entregas and (not ultima or entregas[p.id]["fecha"] > ultima["fecha"]):
                    ultima = {"pedido": p, "productos": productos_texto(p), **entregas[p.id]}
            comprado = sum((p.importe_total for p in ventas), Decimal(0))
            # Igual que en el estado de cuenta: todo lo pagado abona, aunque el pedido se haya cancelado.
            pagado = sum((p.importe_pagado for p in pedidos), Decimal(0))
            filas.append({
                "cliente": cliente,
                "pedidos": len(ventas),
                "entregados": sum(1 for p in ventas if p.id in entregas),
                "comprado": comprado,
                "pagado": pagado,
                "debe": comprado - pagado,
                "trueque": saldo_trueque(cliente.id),
                "ultima": ultima,
            })
        return filas

    def balance_cliente(cliente):
        pedidos = PedidoCRM.query.filter_by(cliente_id=cliente.id).order_by(PedidoCRM.fecha).all()
        ids = [p.id for p in pedidos]
        pagos = (
            PagoPedidoCRM.query.filter(PagoPedidoCRM.pedido_id.in_(ids)).order_by(PagoPedidoCRM.fecha).all()
            if ids else []
        )
        entregas = entregas_de(ids)
        # Estado de cuenta: cada pedido vendido es un cargo y cada pago un abono.
        movimientos = []
        for p in pedidos:
            if p.estado not in ESTADOS_SIN_VENTA:
                concepto = f"Pedido {p.folio}: {productos_texto(p)}"
                if p.urgente:
                    concepto += f" (incluye entrega urgente {_dinero(p.cargo_urgente)})"
                movimientos.append({"fecha": p.fecha, "concepto": concepto, "cargo": p.importe_total, "abono": Decimal(0)})
        for pago in pagos:
            concepto = f"Pago {pago.metodo} · {pago.pedido.folio if pago.pedido else ''} · {pago.referencia}"
            movimientos.append({"fecha": pago.fecha, "concepto": concepto, "cargo": Decimal(0), "abono": pago.monto})
        movimientos.sort(key=lambda m: m["fecha"])
        saldo = Decimal(0)
        for m in movimientos:
            saldo += m["cargo"] - m["abono"]
            m["saldo"] = saldo

        entregados = {}
        for p in pedidos:
            if p.id not in entregas:
                continue
            for linea in p.lineas:
                nombre = linea.producto.nombre if linea.producto else "Producto"
                fila = entregados.setdefault(nombre, {"producto": nombre, "cantidad": Decimal(0), "veces": 0, "ultima": None})
                fila["cantidad"] += linea.cantidad
                fila["veces"] += 1
                fecha = entregas[p.id]["fecha"]
                fila["ultima"] = max(fila["ultima"], fecha) if fila["ultima"] else fecha

        ventas = [p for p in pedidos if p.estado not in ESTADOS_SIN_VENTA]
        return {
            "pedidos": list(reversed(pedidos)),
            "pagos": list(reversed(pagos)),
            "entregas": entregas,
            "movimientos": movimientos,
            "entregados": sorted(entregados.values(), key=lambda f: f["producto"]),
            "trueque": MovimientoTruequeCRM.query.filter_by(cliente_id=cliente.id).order_by(MovimientoTruequeCRM.fecha.desc()).all(),
            "saldo_trueque": saldo_trueque(cliente.id),
            "comprado": sum((p.importe_total for p in ventas), Decimal(0)),
            "pagado": sum((pg.monto for pg in pagos), Decimal(0)),
            "debe": saldo,
            "pendientes_entrega": sum(1 for p in ventas if p.id not in entregas),
        }

    @app.route("/admin/clientes")
    @app.route("/admin/productos-entregados")
    @protegido
    def clientes_erp():
        q = request.args.get("q", "").strip()
        filas = resumen_clientes(q)
        totales = {k: sum((f[k] for f in filas), Decimal(0)) for k in ("comprado", "pagado", "debe", "trueque")}
        return render_template("erp_clientes.html", filas=filas, q=q, totales=totales)

    @app.route("/admin/clientes/pdf")
    @app.route("/admin/productos-entregados/pdf")
    @protegido
    def clientes_erp_pdf():
        filas = resumen_clientes(request.args.get("q", "").strip())
        buffer = generar_pdf(
            titulo="Clientes",
            subtitulo=f"Balance por cliente · {len(filas)} cliente(s) · deben en total {_dinero(sum((f['debe'] for f in filas), Decimal(0)))}",
            secciones=[{
                "columnas": ["Cliente", "Pedidos", "Comprado", "Pagado", "Debe", "Última entrega"],
                "filas": [
                    [
                        celda(f["cliente"].nombre_completo),
                        celda(f"{f['entregados']}/{f['pedidos']} entregados"),
                        celda(_dinero(f["comprado"])),
                        celda(_dinero(f["pagado"])),
                        celda(_dinero(f["debe"]), negrita=f["debe"] > 0),
                        celda(
                            f"{f['ultima']['fecha'].strftime('%d/%m/%Y')} · {f['ultima']['productos']}"
                            + (f" · recibió {f['ultima']['receptor']}" if f["ultima"]["receptor"] else "")
                            if f["ultima"] else "Sin entregas",
                            muted=True,
                        ),
                    ]
                    for f in filas
                ],
                "anchos": [3.8, 2.4, 2.4, 2.4, 2.2, 5.2],
                "vacio": "No hay clientes registrados.",
            }],
        )
        return respuesta_pdf(buffer, "clientes-balance.pdf")

    @app.route("/admin/clientes/<int:id_cliente>")
    @protegido
    def cliente_erp(id_cliente):
        cliente = db.get_or_404(ClienteCRM, id_cliente)
        return render_template("erp_cliente.html", cliente=cliente, **balance_cliente(cliente))

    @app.route("/admin/clientes/<int:id_cliente>/pdf")
    @protegido
    def cliente_erp_pdf(id_cliente):
        cliente = db.get_or_404(ClienteCRM, id_cliente)
        b = balance_cliente(cliente)
        contacto = " · ".join(filter(None, [cliente.telefono, cliente.correo, cliente.rfc] + [d.direccion for d in cliente.domicilios]))
        buffer = generar_pdf(
            titulo=f"Estado de cuenta · {cliente.nombre_completo}",
            subtitulo=contacto or "Sin datos de contacto",
            secciones=[
                {
                    "titulo": "Resumen",
                    "columnas": ["Comprado", "Pagado", "Debe", "Trueque a favor", "Pendientes de entrega"],
                    "filas": [[
                        celda(_dinero(b["comprado"])), celda(_dinero(b["pagado"])),
                        celda(_dinero(b["debe"]), negrita=True), celda(_dinero(b["saldo_trueque"])),
                        celda(b["pendientes_entrega"]),
                    ]],
                    "anchos": [3.6, 3.6, 3.6, 3.6, 4],
                },
                {
                    "titulo": "Estado de cuenta",
                    "columnas": ["Fecha", "Concepto", "Cargo", "Abono", "Saldo"],
                    "filas": [
                        [
                            celda(m["fecha"].strftime("%d/%m/%Y")),
                            celda(m["concepto"]),
                            celda(_dinero(m["cargo"]) if m["cargo"] else ""),
                            celda(_dinero(m["abono"]) if m["abono"] else ""),
                            celda(_dinero(m["saldo"]), negrita=True),
                        ]
                        for m in b["movimientos"]
                    ],
                    "anchos": [2.2, 8.4, 2.6, 2.6, 2.6],
                    "vacio": "Sin movimientos.",
                },
                {
                    "titulo": "Pedidos",
                    "columnas": ["Fecha", "Folio", "Productos", "Estado", "Entrega", "Total", "Saldo"],
                    "filas": [
                        [
                            celda(p.fecha.strftime("%d/%m/%Y")),
                            celda(p.folio),
                            celda(productos_texto(p)),
                            celda(p.estado, muted=True),
                            celda(
                                (b["entregas"][p.id]["fecha"].strftime("%d/%m/%Y") + (f"\n{b['entregas'][p.id]['receptor']}" if b["entregas"][p.id]["receptor"] else ""))
                                if p.id in b["entregas"] else "—",
                                muted=True,
                            ),
                            celda(_dinero(p.importe_total)),
                            celda(_dinero(p.saldo)),
                        ]
                        for p in b["pedidos"]
                    ],
                    "anchos": [2, 2.2, 4.6, 2.4, 2.6, 2.3, 2.3],
                    "vacio": "Sin pedidos.",
                },
                {
                    "titulo": "Productos entregados",
                    "columnas": ["Producto", "Cantidad total", "Entregas", "Última entrega"],
                    "filas": [
                        [celda(f["producto"]), celda(f"{f['cantidad']:g}"), celda(f["veces"]), celda(f["ultima"].strftime("%d/%m/%Y"))]
                        for f in b["entregados"]
                    ],
                    "anchos": [8.4, 3.4, 2.6, 4],
                    "vacio": "Aún no se le ha entregado nada.",
                },
            ],
        )
        return respuesta_pdf(buffer, f"estado-de-cuenta-cliente-{cliente.id}.pdf")
