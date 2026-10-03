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

    def test_ciclo_pedido_documenta_los_pasos_y_exige_login(self):
        with self.client.session_transaction() as sesion:
            sesion.clear()
        self.assertEqual(self.client.get("/admin/operaciones/ciclo-pedido").status_code, 302)

        with self.client.session_transaction() as sesion:
            sesion["admin_logueado"] = True
        respuesta = self.client.get("/admin/operaciones/ciclo-pedido")
        self.assertEqual(respuesta.status_code, 200)
        cuerpo = respuesta.get_data(as_text=True)
        for texto in [
            "Ciclo de un pedido",
            "Ventas crea el pedido",
            "api_crm_confirmar_pedido",
            "crm_pedido_pagos",
            "Intento fallido",
            "Ciclo de vida del pedido",
        ]:
            self.assertIn(texto, cuerpo)

        self.assertIn("Ciclo del pedido", self.client.get("/admin/operaciones").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
