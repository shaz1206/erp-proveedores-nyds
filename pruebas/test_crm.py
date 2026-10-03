import unittest
from decimal import Decimal

from test_proveedores_mp import app, db
from proveedores.vendssback import (
    AjusteInventario,
    EventoCRM,
    MateriaPrima,
    MovimientoPT,
    OrdenLlenadoCRM,
    PedidoCRM,
    ProductoTerminado,
    Receta,
    RecetaIngrediente,
    ReservaERP,
)


class CRMTest(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion["admin_logueado"] = True
            sesion["usuario"] = "ventas.nyds"
        self.pt = ProductoTerminado(sku="DET-5L", nombre="Detergente color 5 L", categoria="Detergentes", estatus="Activo")
        self.liquido = MateriaPrima(
            sku_mp="MP-LIQ",
            nombre_oficial="Detergente a granel",
            nombre_normalizado="detergente a granel",
            familia="Liquidos",
            estado_fisico="Liquido",
            unidad_base="L",
            activo=True,
            borrador=False,
        )
        self.envase = MateriaPrima(
            sku_mp="MP-BOTE",
            nombre_oficial="Bote 5 L",
            nombre_normalizado="bote 5 l",
            familia="Envases",
            estado_fisico="Solido",
            unidad_base="pieza",
            activo=True,
            borrador=False,
            permite_fraccion=False,
        )
        db.session.add_all([self.pt, self.liquido, self.envase])
        db.session.flush()
        receta = Receta(
            clave_origen="DET-5L",
            nombre="Llenado detergente color 5 L",
            tipo="Producto terminado",
            estado="Activo",
            producto_id=self.pt.id,
            rendimiento=1,
            unidad_rendimiento="pieza",
            origen={},
        )
        db.session.add(receta)
        db.session.flush()
        db.session.add_all(
            [
                RecetaIngrediente(receta_id=receta.id, mp_id=self.liquido.id_mp, nombre_original="Detergente a granel", cantidad=5, unidad="L"),
                RecetaIngrediente(receta_id=receta.id, mp_id=self.envase.id_mp, nombre_original="Bote 5 L", cantidad=1, unidad="pieza"),
                AjusteInventario(mp_id=self.liquido.id_mp, tipo="Saldo inicial", cantidad=20, unidad="L", ubicacion="Produccion", lote="", demo=False, motivo="Inicial", referencia="INI-LIQ", usuario="test", token="liq"),
                AjusteInventario(mp_id=self.envase.id_mp, tipo="Saldo inicial", cantidad=4, unidad="pieza", ubicacion="Produccion", lote="", demo=False, motivo="Inicial", referencia="INI-BOTE", usuario="test", token="bote"),
            ]
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def crear_pedido(self, token="pedido-1", cantidad="1"):
        return self.client.post(
            "/api/crm/pedidos",
            json={
                "token": token,
                "canal": "WhatsApp",
                "forma_pago": "Contra entrega",
                "entrega_propia": True,
                "cliente": {"nombre": "Cliente prueba", "telefono": "9981000000"},
                "domicilio": {"direccion": "Av. Talleres 123", "zona": "Cancun"},
                "lineas": [{"producto_id": self.pt.id, "cantidad": cantidad, "precio_unitario": "120.00"}],
            },
        )

    def test_reserva_componentes_llenado_ruta_y_entrega(self):
        respuesta = self.crear_pedido()
        self.assertEqual(respuesta.status_code, 201)
        pedido_id = respuesta.get_json()["id"]

        confirmacion = self.client.post(f"/api/crm/pedidos/{pedido_id}/confirmar", json={})
        self.assertEqual(confirmacion.status_code, 200)
        self.assertEqual(confirmacion.get_json()["estado"], "Confirmado")
        self.assertEqual(ReservaERP.query.filter_by(tipo="Materia prima", estado="Activa").count(), 2)
        self.assertEqual(OrdenLlenadoCRM.query.count(), 1)

        orden = OrdenLlenadoCRM.query.one()
        cierre = self.client.post(f"/api/crm/ordenes-llenado/{orden.id}/completar", json={"token": "llenado-1", "ubicacion_mp": "Produccion"})
        self.assertEqual(cierre.status_code, 200)
        self.assertEqual(sum(a.cantidad for a in AjusteInventario.query.filter(AjusteInventario.referencia == f"CRM-LLENADO-{orden.id}")), Decimal("-6.000000"))
        self.assertEqual(MovimientoPT.query.filter_by(tipo="Alta por llenado").one().cantidad, Decimal("1.000000"))
        self.assertEqual(ReservaERP.query.filter_by(tipo="Producto terminado", estado="Activa").count(), 1)

        estado = self.client.post(f"/api/crm/pedidos/{pedido_id}/estado", json={"estado": "En ruta"})
        self.assertEqual(estado.status_code, 200)
        self.assertEqual(MovimientoPT.query.filter_by(ubicacion="En transito").one().cantidad, Decimal("1.000000"))

        entrega = self.client.post(f"/api/crm/pedidos/{pedido_id}/entrega", json={"entregado": True, "receptor": "Ana"})
        self.assertEqual(entrega.status_code, 200)
        self.assertEqual(entrega.get_json()["estado"], "Entregado")
        self.assertEqual(ReservaERP.query.filter_by(tipo="Producto terminado", estado="Consumida").count(), 1)
        self.assertGreaterEqual(EventoCRM.query.count(), 4)
        entregados = self.client.get("/admin/productos-entregados")
        self.assertEqual(entregados.status_code, 200)
        self.assertIn(b"Detergente color 5 L", entregados.data)
        self.assertIn(b"Ana", entregados.data)

    def test_reserva_producto_terminado_existente_no_descuenta_componentes(self):
        db.session.add(MovimientoPT(producto_id=self.pt.id, tipo="Saldo inicial", cantidad=2, ubicacion="Almacen PT", referencia="PT-INICIAL", usuario="test", token="pt-inicial"))
        db.session.commit()
        pedido_id = self.crear_pedido(token="pedido-pt").get_json()["id"]
        respuesta = self.client.post(f"/api/crm/pedidos/{pedido_id}/confirmar", json={})
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.get_json()["lineas"][0]["modo_reserva"], "Producto terminado")
        self.assertEqual(OrdenLlenadoCRM.query.count(), 0)
        self.assertEqual(AjusteInventario.query.count(), 2)

    def test_entrega_directa_desde_listo_actualiza_productos_entregados(self):
        db.session.add(MovimientoPT(producto_id=self.pt.id, tipo="Saldo inicial", cantidad=2, ubicacion="Almacen PT", referencia="PT-INICIAL", usuario="test", token="pt-inicial"))
        db.session.commit()
        pedido_id = self.crear_pedido(token="pedido-entrega-directa").get_json()["id"]
        self.client.post(f"/api/crm/pedidos/{pedido_id}/confirmar", json={})
        self.client.post(f"/api/crm/pedidos/{pedido_id}/estado", json={"estado": "Listo para entrega"})

        entrega = self.client.post(f"/api/crm/pedidos/{pedido_id}/entrega", json={"entregado": True, "receptor": "Luis"})
        self.assertEqual(entrega.status_code, 200, entrega.get_json())
        self.assertEqual(entrega.get_json()["estado"], "Entregado")
        self.assertEqual(MovimientoPT.query.filter_by(tipo="Salida a ruta").count(), 1)
        self.assertEqual(MovimientoPT.query.filter_by(tipo="Entrada a transito").count(), 1)
        self.assertEqual(MovimientoPT.query.filter_by(tipo="Entrega confirmada").count(), 1)
        self.assertIn(b"Luis", self.client.get("/admin/productos-entregados").data)

    def test_sin_componentes_suficientes_queda_pendiente_sin_negativos(self):
        respuesta = self.crear_pedido(token="pedido-faltante", cantidad="5")
        pedido_id = respuesta.get_json()["id"]
        confirmacion = self.client.post(f"/api/crm/pedidos/{pedido_id}/confirmar", json={})
        self.assertEqual(confirmacion.status_code, 200)
        self.assertEqual(confirmacion.get_json()["estado"], "Pendiente de produccion")
        self.assertTrue(confirmacion.get_json()["faltantes"])
        self.assertEqual(ReservaERP.query.count(), 0)
        self.assertEqual(PedidoCRM.query.get(pedido_id).estado, "Pendiente de produccion")

    def test_portal_crm_y_permisos_separados_del_erp(self):
        from proveedores.vendssback import Persona
        from werkzeug.security import generate_password_hash

        db.session.add(Persona(
            nombre="Reparto de prueba",
            usuario="reparto.nyds",
            password_hash=generate_password_hash("reparto2026*"),
            rol="reparto",
            activo=True,
        ))
        db.session.commit()

        with self.client.session_transaction() as sesion:
            sesion.clear()
        self.assertEqual(self.client.get("/crm/pedidos").status_code, 302)
        self.assertEqual(self.client.get("/admin/catalogo").status_code, 302)

        login = self.client.post("/crm/login", data={"usuario": "reparto.nyds", "password": "reparto2026*"})
        self.assertEqual(login.status_code, 302)
        self.assertEqual(self.client.get("/crm/pedidos").status_code, 200)
        self.assertEqual(self.client.get("/admin/catalogo").status_code, 302)
        self.assertEqual(self.crear_pedido(token="pedido-reparto").status_code, 403)

        with self.client.session_transaction() as sesion:
            sesion.clear()
            sesion["crm_logueado"] = True
            sesion["crm_usuario"] = "ventas.nyds"
            sesion["crm_rol"] = "ventas"
        pedido_id = self.crear_pedido(token="pedido-ventas").get_json()["id"]
        self.client.post(f"/api/crm/pedidos/{pedido_id}/confirmar", json={})
        orden = OrdenLlenadoCRM.query.one()
        self.assertEqual(self.client.post(f"/api/crm/ordenes-llenado/{orden.id}/completar", json={"token": "no-ventas"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
