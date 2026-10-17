import unittest

from test_crm import CRMTest
from proveedores.vendssback import db, PedidoCRM, ReservaERP


class OperacionesTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown
    crear_pedido = CRMTest.crear_pedido

    def test_panel_operativo_y_json(self):
        respuesta = self.client.get("/admin/operaciones")
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b"Trabajo pendiente", respuesta.data)
        self.assertNotIn(b"Mapa de interconexion", respuesta.data)

        salud = self.client.get("/api/operaciones/salud")
        self.assertEqual(salud.status_code, 200)
        data = salud.get_json()
        self.assertIn("estado_general", data)
        self.assertIn("crm", [s["clave"] for s in data["secciones"]])
        self.assertGreaterEqual(data["metricas"]["productos_terminados_activos"], 1)

    def test_detecta_reserva_activa_en_pedido_cerrado(self):
        pedido_id = self.crear_pedido().get_json()["id"]
        self.client.post(f"/api/crm/pedidos/{pedido_id}/confirmar", json={})
        pedido = db.session.get(PedidoCRM, pedido_id)
        pedido.estado = "Entregado"
        db.session.commit()

        data = self.client.get("/api/operaciones/salud").get_json()
        titulos = [i["titulo"] for i in data["incidencias"]]
        self.assertIn("Reserva activa en pedido cerrado", titulos)
        self.assertEqual(ReservaERP.query.filter_by(estado="Activa").count(), 2)

    def test_ciclo_del_pedido_ya_no_existe_y_hay_acciones_directas(self):
        # La documentacion tecnica del ciclo del pedido no pertenece al ERP.
        self.assertEqual(self.client.get("/admin/operaciones/ciclo-pedido").status_code, 404)
        cuerpo = self.client.get("/admin/operaciones").get_data(as_text=True)
        self.assertNotIn("Ciclo del pedido", cuerpo)
        self.assertNotIn("Continuar flujo", cuerpo)
        for enlace in ["/crm/pedidos", "/admin/compras/nueva", "/admin/produccion", "/admin/inventario-mp/registrar"]:
            self.assertIn(f'href="{enlace}"', cuerpo)
        for enlace in ["/admin/produccion/lista-compra", "/admin/envases", "/admin/productos-terminados/nuevo", "/admin/materias-primas/nueva", "/admin/alta", "/admin/precios"]:
            self.assertEqual(self.client.get(enlace).status_code, 200, enlace)


if __name__ == "__main__":
    unittest.main()
