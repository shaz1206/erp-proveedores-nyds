"""Ordenes de produccion y lista de compra automatica."""
import unittest
from datetime import date
from decimal import Decimal

from test_crm import CRMTest
from proveedores.vendssback import (AjusteInventario, MateriaPrimaProveedor, MovimientoPT, OrdenCompra, CompraLinea,
                                    OrdenProduccion, Proveedor, ProduccionConsumo, Receta, db)


class ProduccionTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown

    def csrf(self, cliente=None):
        cliente = cliente or self.client
        cliente.get("/admin/produccion")
        with cliente.session_transaction() as s:
            return s["produccion_csrf"]

    def planear(self, cantidad="3", token="op1", producto=None, **extra):
        datos = dict(csrf=self.csrf(), token=token, producto_id=producto or self.pt.id, cantidad=cantidad)
        datos.update(extra)
        return self.client.post("/admin/produccion", data=datos)

    def accion(self, orden, nombre, **extra):
        return self.client.post(f"/admin/produccion/{orden.id}/{nombre}", data=dict(csrf=self.csrf(), **extra))

    def stock_pt(self):
        return sum((m.cantidad for m in MovimientoPT.query.filter_by(producto_id=self.pt.id, ubicacion="Almacen PT")), Decimal(0))

    def saldo(self, mp):
        return sum((a.cantidad for a in AjusteInventario.query.filter_by(mp_id=mp.id_mp)), Decimal(0))

    def proveedor(self, razon="Quimica SA", moneda="MXN", precio=100):
        p = Proveedor(codigo_proveedor=f"P-{razon[:3]}", razon_social=razon, rfc=f"RFC-{razon[:3]}", tipo_proveedor="Materia prima",
                      estatus="Activo", calle_numero="Calle", colonia="Centro", codigo_postal="77500", condicion_pago="Contado",
                      tiempo_entrega_normal=2)
        db.session.add(p)
        db.session.flush()
        c = MateriaPrimaProveedor(id_mp=self.liquido.id_mp, id_proveedor=p.id_proveedor, presentacion_compra="Garrafa 20 L",
                                  contenido_presentacion=20, unidad_contenido="L", factor_a_unidad_base=20, precio_vigente=precio,
                                  tipo_precio="Por presentación", moneda=moneda, iva_tasa=16, compra_minima=1,
                                  unidad_compra_minima="Presentación", lead_time_dias=3, vigencia_desde=date.today(), activo=True,
                                  principal=True)
        db.session.add(c)
        db.session.commit()
        return c

    # ---- acceso ----
    def test_exige_login(self):
        anonimo = self.client.application.test_client()
        for ruta in ("/admin/produccion", "/admin/produccion/lista-compra", "/admin/produccion/1"):
            self.assertEqual(anonimo.get(ruta).status_code, 302, ruta)
        self.assertEqual(anonimo.post("/admin/produccion", data={}).status_code, 302)

    def test_paginas_cargan(self):
        for ruta in ("/admin/produccion", "/admin/produccion/lista-compra"):
            r = self.client.get(ruta)
            self.assertEqual(r.status_code, 200, ruta)
            self.assertIn("Producción".encode(), r.data)

    def test_post_sin_csrf_rechazado(self):
        r = self.client.post("/admin/produccion", data=dict(token="x", producto_id=self.pt.id, cantidad="1"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(OrdenProduccion.query.count(), 0)

    # ---- crear ----
    def test_crear_orden_calcula_materia_prima(self):
        r = self.planear("3")
        self.assertEqual(r.status_code, 302)
        orden = OrdenProduccion.query.one()
        self.assertEqual(orden.estado, "Planeada")
        self.assertEqual(orden.folio, f"OP-{orden.id:06d}")
        consumos = {c.mp_id: c.cantidad for c in orden.consumos}
        self.assertEqual(consumos[self.liquido.id_mp], Decimal("15"))
        self.assertEqual(consumos[self.envase.id_mp], Decimal("3"))
        self.assertEqual(self.client.get(f"/admin/produccion/{orden.id}").status_code, 200)

    def test_crear_es_idempotente_por_token(self):
        self.planear("3", token="igual")
        self.planear("3", token="igual")
        self.assertEqual(OrdenProduccion.query.count(), 1)

    def test_cantidades_invalidas(self):
        for i, valor in enumerate(["0", "-2", "abc", "", "1e3", "0.0000001", "99999999999"]):
            self.planear(valor, token=f"t{i}")
        self.assertEqual(OrdenProduccion.query.count(), 0)

    def test_producto_sin_receta_o_inactivo(self):
        Receta.query.delete()
        db.session.commit()
        self.planear("1", token="a")
        self.assertEqual(OrdenProduccion.query.count(), 0)
        self.pt.estatus = "Inactivo"
        db.session.commit()
        self.planear("1", token="b")
        self.assertEqual(OrdenProduccion.query.count(), 0)

    def test_subreceta_se_expande(self):
        from proveedores.vendssback import RecetaIngrediente
        base = Receta(clave_origen="BASE", nombre="Base", tipo="Subreceta", estado="Activo", rendimiento=10, unidad_rendimiento="L", origen={})
        db.session.add(base)
        db.session.flush()
        db.session.add(RecetaIngrediente(receta_id=base.id, mp_id=self.liquido.id_mp, nombre_original="Detergente a granel", cantidad=4, unidad="L"))
        principal = Receta.query.filter_by(clave_origen="DET-5L").one()
        for linea in list(principal.lineas):
            if linea.mp_id == self.liquido.id_mp:
                db.session.delete(linea)
        db.session.add(RecetaIngrediente(receta_id=principal.id, subreceta_id=base.id, nombre_original="Base", cantidad=10, unidad="L"))
        db.session.commit()
        self.planear("2")
        orden = OrdenProduccion.query.one()
        consumos = {c.mp_id: c.cantidad for c in orden.consumos}
        # 2 piezas -> 20 L de base -> factor 2 -> 8 L de detergente
        self.assertEqual(consumos[self.liquido.id_mp], Decimal("8"))

    # ---- producir ----
    def test_producir_descuenta_mp_y_suma_pt(self):
        self.planear("3")
        orden = OrdenProduccion.query.one()
        r = self.accion(orden, "producir")
        self.assertEqual(r.status_code, 302)
        db.session.refresh(orden)
        self.assertEqual(orden.estado, "Producida")
        self.assertEqual(self.stock_pt(), Decimal("3"))
        self.assertEqual(self.saldo(self.liquido), Decimal("5"))
        self.assertEqual(self.saldo(self.envase), Decimal("1"))
        self.assertIsNotNone(orden.cerrada_en)

    def test_no_se_produce_dos_veces(self):
        self.planear("3")
        orden = OrdenProduccion.query.one()
        self.accion(orden, "producir")
        self.accion(orden, "producir")
        self.assertEqual(self.stock_pt(), Decimal("3"))
        self.assertEqual(self.saldo(self.liquido), Decimal("5"))

    def test_faltante_bloquea_produccion(self):
        self.planear("5")  # 25 L pero solo hay 20
        orden = OrdenProduccion.query.one()
        detalle = self.client.get(f"/admin/produccion/{orden.id}")
        self.assertIn(b"Falta materia prima", detalle.data)
        self.accion(orden, "producir")
        db.session.refresh(orden)
        self.assertEqual(orden.estado, "Planeada")
        self.assertEqual(self.stock_pt(), Decimal("0"))
        self.assertEqual(self.saldo(self.liquido), Decimal("20"))

    def test_reservas_de_pedidos_reducen_disponible(self):
        # un pedido confirmado reserva MP/PT; la produccion no debe consumir lo reservado
        resp = CRMTest.crear_pedido(self, cantidad="3")
        self.assertEqual(resp.status_code, 201)
        pid = resp.get_json()["id"]
        self.assertEqual(self.client.post(f"/api/crm/pedidos/{pid}/confirmar", json={}).status_code, 200)
        self.planear("3", token="tras-reserva")
        orden = OrdenProduccion.query.one()
        self.accion(orden, "producir")
        db.session.refresh(orden)
        self.assertEqual(orden.estado, "Planeada")

    def test_cancelar(self):
        self.planear("3")
        orden = OrdenProduccion.query.one()
        self.accion(orden, "cancelar")
        db.session.refresh(orden)
        self.assertEqual(orden.estado, "Cancelada")
        self.accion(orden, "producir")
        db.session.refresh(orden)
        self.assertEqual(orden.estado, "Cancelada")
        self.assertEqual(self.stock_pt(), Decimal("0"))

    def test_no_se_cancela_una_producida(self):
        self.planear("3")
        orden = OrdenProduccion.query.one()
        self.accion(orden, "producir")
        self.accion(orden, "cancelar")
        db.session.refresh(orden)
        self.assertEqual(orden.estado, "Producida")

    def test_acciones_sin_csrf_rechazadas(self):
        self.planear("3")
        orden = OrdenProduccion.query.one()
        for nombre in ("producir", "cancelar"):
            self.assertEqual(self.client.post(f"/admin/produccion/{orden.id}/{nombre}", data={}).status_code, 400)
        db.session.refresh(orden)
        self.assertEqual(orden.estado, "Planeada")

    def test_filtro_por_estado(self):
        self.planear("1", token="a")
        self.planear("2", token="b")
        otra = OrdenProduccion.query.filter_by(token="b").one()
        self.accion(otra, "cancelar")
        r = self.client.get("/admin/produccion?estado=Cancelada")
        self.assertIn(otra.folio.encode(), r.data)
        self.assertNotIn(OrdenProduccion.query.filter_by(token="a").one().folio.encode(), r.data)

    # ---- lista de compra ----
    def test_lista_compra_sin_ordenes(self):
        r = self.client.get("/admin/produccion/lista-compra")
        self.assertIn("No hay órdenes planeadas".encode(), r.data)

    def test_lista_compra_suficiente(self):
        self.planear("3")
        r = self.client.get("/admin/produccion/lista-compra")
        self.assertIn(b"suficiente", r.data)

    def test_lista_compra_sin_proveedor(self):
        self.planear("5")
        r = self.client.get("/admin/produccion/lista-compra")
        self.assertIn("Sin proveedor vigente".encode(), r.data)
        self.assertIn(b"Detergente a granel", r.data)

    def test_lista_compra_agrupa_por_proveedor_y_emite_orden(self):
        cond = self.proveedor()
        db.session.add(AjusteInventario(mp_id=self.envase.id_mp, tipo="Saldo inicial", cantidad=10, unidad="pieza", ubicacion="Produccion",
                                        lote="", demo=False, motivo="Extra", referencia="X", usuario="t", token="extra-bote"))
        db.session.commit()
        self.planear("5")  # necesita 25 L, hay 20 -> faltan 5 L -> 1 garrafa de 20 L
        r = self.client.get("/admin/produccion/lista-compra")
        html = r.get_data(as_text=True)
        self.assertIn("Quimica SA", html)
        self.assertIn("Garrafa 20 L", html)
        self.assertIn(f'name="condicion" value="{cond.id_relacion}"', html)
        self.assertNotIn("Sin proveedor vigente", html)
        # el formulario apunta al flujo de compras existente y se acepta tal cual
        with self.client.session_transaction() as s:
            csrf_compras = s["compras_csrf"]
        r = self.client.post("/admin/compras/nueva", data=dict(csrf=csrf_compras, token="lc-1", entrega=str(date.today()),
                                                               condicion=str(cond.id_relacion), cantidad="1"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(CompraLinea.query.one().cantidad, 1)
        self.assertEqual(OrdenCompra.query.one().proveedor_nombre, "Quimica SA")

    def test_lista_compra_respeta_compra_minima(self):
        cond = self.proveedor()
        cond.compra_minima = 4
        db.session.commit()
        self.planear("5")
        html = self.client.get("/admin/produccion/lista-compra").get_data(as_text=True)
        self.assertIn('name="cantidad" class="form-control form-control-sm text-end" value="4"', html)

    def test_lista_compra_ignora_ordenes_no_planeadas(self):
        self.proveedor()
        self.planear("5")
        orden = OrdenProduccion.query.one()
        self.accion(orden, "cancelar")
        html = self.client.get("/admin/produccion/lista-compra").get_data(as_text=True)
        self.assertIn("No hay órdenes planeadas", html)

    def test_lista_compra_suma_varias_ordenes(self):
        self.proveedor()
        self.planear("3", token="a")  # 15 L
        self.planear("3", token="b")  # 15 L -> 30 L, hay 20 -> faltan 10 -> 1 garrafa
        html = self.client.get("/admin/produccion/lista-compra").get_data(as_text=True)
        self.assertIn("Quimica SA", html)
        self.assertIn("OP-", html)

    def test_menu_muestra_produccion(self):
        self.assertIn(b'href="/admin/produccion"', self.client.get("/admin/produccion").data)


if __name__ == "__main__":
    unittest.main()
