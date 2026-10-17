"""Tablero del ERP: ventas y produccion del mes, ultimos movimientos y graficas.

Solo lee datos de CRM, inventario PT y produccion; no crea tablas ni cambia
reglas. Las graficas se dibujan en el servidor como SVG para que funcionen
sin librerias externas y respeten la escala (una sola escala por grafica).
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from math import ceil, floor, log10

from flask import redirect, render_template, request, session, url_for

if __package__:
    from .crm import (ESTADOS_SIN_VENTA, contexto_mes, dinero, hace_texto, meses_atras, nombre_mes)
else:
    from crm import (ESTADOS_SIN_VENTA, contexto_mes, dinero, hace_texto, meses_atras, nombre_mes)

TIPOS_ALTA_PT = ("Alta por produccion", "Alta por llenado")
ESTADOS_POR_CONFIRMAR = ("Recibido", "Pendiente de pago")
ANCHO, ALTO = 640, 230
MARGEN_IZQ, MARGEN_DER, MARGEN_SUP, MARGEN_INF = 56, 12, 14, 28


def escala_agradable(maximo, pasos=4):
    """Maximo redondeado y marcas parejas (1, 2, 2.5, 5 x 10^n) que cubren el valor."""
    if maximo <= 0:
        return 1, [0, 1]
    bruto = maximo / pasos
    potencia = 10 ** floor(log10(bruto))
    for m in (1, 2, 2.5, 5, 10):
        paso = m * potencia
        if paso * pasos >= maximo:
            break
    tope = paso * ceil(maximo / paso)
    marcas, valor = [], 0
    while valor <= tope + paso / 1000:
        marcas.append(round(valor, 6))
        valor += paso
    return tope, marcas


def formato_corto(valor, dinero_=False):
    prefijo = "$" if dinero_ else ""
    if abs(valor) >= 1_000_000:
        return f"{prefijo}{valor / 1_000_000:g}M"
    if abs(valor) >= 1_000:
        return f"{prefijo}{valor / 1_000:g}k"
    return f"{prefijo}{valor:g}"


def barras(etiquetas, valores, resaltar, dinero_=False):
    """Geometria de una grafica de barras verticales con eje y marcas."""
    tope, marcas = escala_agradable(max(valores) if valores else 0)
    ancho_util, alto_util = ANCHO - MARGEN_IZQ - MARGEN_DER, ALTO - MARGEN_SUP - MARGEN_INF
    ranura = ancho_util / max(len(valores), 1)
    ancho_barra = ranura * 0.62
    base = MARGEN_SUP + alto_util
    y = lambda v: base - alto_util * v / tope
    return {
        "ancho": ANCHO, "alto": ALTO, "izq": MARGEN_IZQ, "der": ANCHO - MARGEN_DER, "base": base,
        "marcas": [{"y": round(y(m), 1), "texto": formato_corto(m, dinero_)} for m in marcas],
        "barras": [{
            "x": round(MARGEN_IZQ + i * ranura + (ranura - ancho_barra) / 2, 1), "y": round(y(v), 1),
            "ancho": round(ancho_barra, 1), "alto": round(max(base - y(v), 0), 1), "etiqueta": etiquetas[i],
            "cx": round(MARGEN_IZQ + i * ranura + ranura / 2, 1), "resaltada": i == resaltar,
            "valor": formato_corto(v, dinero_) if v else "", "titulo": f"{etiquetas[i]}: {(dinero(v) if dinero_ else f'{v:g}')}",
        } for i, v in enumerate(valores)],
    }


def registrar_tablero(app, db, PedidoCRM, PedidoLineaCRM, PagoPedidoCRM, MovimientoPT):
    def autorizado():
        return bool(session.get("admin_logueado"))

    def ventas_entre(inicio, fin):
        return [p for p in PedidoCRM.query.filter(PedidoCRM.fecha >= inicio, PedidoCRM.fecha < fin).all()
                if p.estado not in ESTADOS_SIN_VENTA]

    @app.route("/admin/tablero")
    def tablero():
        if not autorizado():
            return redirect(url_for("login_admin"))
        mes = contexto_mes(request.args.get("mes"))
        inicio, fin = mes["inicio"], mes["fin"]
        ahora = datetime.utcnow()

        # ---- ultimos 12 meses (hasta el mes elegido) para las dos graficas ----
        meses = list(reversed(meses_atras(12, desde=inicio)))
        desde, hasta = meses[0], fin
        ventas_mes = defaultdict(Decimal)
        for p in PedidoCRM.query.filter(PedidoCRM.fecha >= desde, PedidoCRM.fecha < hasta).all():
            if p.estado not in ESTADOS_SIN_VENTA:
                ventas_mes[(p.fecha.year, p.fecha.month)] += p.importe_total or 0
        unidades_mes = defaultdict(Decimal)
        for m in MovimientoPT.query.filter(MovimientoPT.tipo.in_(TIPOS_ALTA_PT), MovimientoPT.fecha >= desde, MovimientoPT.fecha < hasta).all():
            unidades_mes[(m.fecha.year, m.fecha.month)] += m.cantidad or 0
        etiquetas = [f"{nombre_mes(m)[:3].capitalize()} {str(m.year)[2:]}" for m in meses]
        grafica_ventas = barras(etiquetas, [float(ventas_mes[(m.year, m.month)]) for m in meses], len(meses) - 1, dinero_=True)
        grafica_produccion = barras(etiquetas, [float(unidades_mes[(m.year, m.month)]) for m in meses], len(meses) - 1)

        # ---- mes elegido ----
        ventas = ventas_entre(inicio, fin)
        facturado = sum((p.importe_total or 0 for p in ventas), Decimal(0))
        cobrado = sum((x.monto for x in PagoPedidoCRM.query.filter(PagoPedidoCRM.fecha >= inicio, PagoPedidoCRM.fecha < fin).all()), Decimal(0))
        altas = (MovimientoPT.query.filter(MovimientoPT.tipo.in_(TIPOS_ALTA_PT), MovimientoPT.fecha >= inicio, MovimientoPT.fecha < fin)
                 .order_by(MovimientoPT.fecha.desc(), MovimientoPT.id.desc()).all())
        unidades = sum((m.cantidad for m in altas), Decimal(0))

        por_producto, por_canal = defaultdict(lambda: [Decimal(0), Decimal(0)]), defaultdict(Decimal)
        for p in ventas:
            por_canal[p.canal] += p.importe_total or 0
            for l in p.lineas:
                dato = por_producto[l.producto.nombre]
                dato[0] += l.cantidad
                dato[1] += l.subtotal
        top = sorted(por_producto.items(), key=lambda kv: kv[1][1], reverse=True)[:6]
        mayor = max((v[1] for _, v in top), default=Decimal(1)) or Decimal(1)
        top_productos = [{"nombre": n, "unidades": v[0], "importe": v[1], "pct": round(float(v[1] / mayor) * 100)} for n, v in top]
        mayor_canal = max(por_canal.values(), default=Decimal(1)) or Decimal(1)
        canales = [{"nombre": n, "importe": v, "pct": round(float(v / mayor_canal) * 100)}
                   for n, v in sorted(por_canal.items(), key=lambda kv: kv[1], reverse=True)]

        ultimas_ventas = [{"id": p.id, "folio": p.folio, "cliente": p.cliente.nombre if p.cliente else "Cliente", "total": p.importe_total,
                           "estado": p.estado, "hace": hace_texto(p.fecha, ahora)}
                          for p in PedidoCRM.query.filter(PedidoCRM.estado.notin_(list(ESTADOS_SIN_VENTA))).order_by(PedidoCRM.fecha.desc(), PedidoCRM.id.desc()).limit(6)]
        ultimas_producciones = [{"referencia": m.referencia, "producto": m.producto.nombre if m.producto else "Producto", "cantidad": m.cantidad,
                                 "origen": "Orden de producción" if m.tipo == "Alta por produccion" else "Llenado de pedido", "hace": hace_texto(m.fecha, ahora)}
                                for m in MovimientoPT.query.filter(MovimientoPT.tipo.in_(TIPOS_ALTA_PT)).order_by(MovimientoPT.fecha.desc(), MovimientoPT.id.desc()).limit(6)]

        resumen = {
            "facturado": facturado, "pedidos": len(ventas), "ticket": (facturado / len(ventas)) if ventas else Decimal(0), "cobrado": cobrado,
            "unidades": unidades, "producciones": len(altas),
            "por_confirmar": PedidoCRM.query.filter(PedidoCRM.estado.in_(ESTADOS_POR_CONFIRMAR)).count(),
            "sin_insumos": PedidoCRM.query.filter_by(estado="Pendiente de produccion").count(),
        }
        return render_template("tablero.html", mes=mes, resumen=resumen, dinero=dinero, grafica_ventas=grafica_ventas,
                               grafica_produccion=grafica_produccion, top_productos=top_productos, canales=canales,
                               ultimas_ventas=ultimas_ventas, ultimas_producciones=ultimas_producciones, pdf_url=None,
                               sin_datos_ventas=not any(ventas_mes.values()), sin_datos_produccion=not any(unidades_mes.values()))

    return tablero
