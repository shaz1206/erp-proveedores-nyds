"""Reportes descargables del ERP: PDF de inventario, hoja de ruta, productos
entregados, y la vista de detalle de producto terminado (solo lectura)."""
import unittest

from test_crm import CRMTest


class ReportesTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown
    crear_pedido = CRMTest.crear_pedido

    def test_inventario_pdf_exige_login_y_regresa_pdf_valido(self):
        with self.client.session_transaction() as sesion:
            sesion.clear()
        self.assertEqual(self.client.get("/admin/inventario-mp/pdf").status_code, 302)

        with self.client.session_transaction() as sesion:
            sesion["admin_logueado"] = True
        respuesta = self.client.get("/admin/inventario-mp/pdf")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.mimetype, "application/pdf")
        self.assertTrue(respuesta.data.startswith(b"%PDF"))

    def test_productos_entregados_pdf_exige_login_y_regresa_pdf_valido(self):
        with self.client.session_transaction() as sesion:
            sesion.clear()
        self.assertEqual(self.client.get("/admin/productos-entregados/pdf").status_code, 302)

        with self.client.session_transaction() as sesion:
            sesion["admin_logueado"] = True
        respuesta = self.client.get("/admin/productos-entregados/pdf")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.mimetype, "application/pdf")
        self.assertTrue(respuesta.data.startswith(b"%PDF"))

    def test_rutas_exige_login_y_muestra_vacio_sin_pedidos_en_ruta(self):
        with self.client.session_transaction() as sesion:
            sesion.clear()
        self.assertEqual(self.client.get("/crm/rutas").status_code, 302)

        with self.client.session_transaction() as sesion:
            sesion["admin_logueado"] = True
        cuerpo = self.client.get("/crm/rutas").get_data(as_text=True)
        self.assertIn("No hay pedidos en ruta", cuerpo)

    def test_rutas_agrupa_por_zona_e_incluye_pdf_valido(self):
        pedido_id = self.crear_pedido(token="ruta-1", cantidad="1").get_json()["id"]
        self.client.post(f"/api/crm/pedidos/{pedido_id}/confirmar", json={})
        from proveedores.vendssback import OrdenLlenadoCRM

        orden = OrdenLlenadoCRM.query.filter_by(pedido_id=pedido_id).one()
        self.client.post(f"/api/crm/ordenes-llenado/{orden.id}/completar", json={})
        estado = self.client.post(f"/api/crm/pedidos/{pedido_id}/estado", json={"estado": "En ruta"})
        self.assertEqual(estado.status_code, 200)

        cuerpo = self.client.get("/crm/rutas").get_data(as_text=True)
        self.assertIn("Zona: Cancun", cuerpo)
        self.assertIn("Cliente prueba", cuerpo)

        respuesta = self.client.get("/crm/rutas/pdf")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.mimetype, "application/pdf")
        self.assertTrue(respuesta.data.startswith(b"%PDF"))

    def test_detalle_producto_terminado_exige_login_y_muestra_datos(self):
        with self.client.session_transaction() as sesion:
            sesion.clear()
        self.assertEqual(
            self.client.get(f"/admin/productos-terminados/{self.pt.id}/ver").status_code, 302
        )

        with self.client.session_transaction() as sesion:
            sesion["admin_logueado"] = True
        respuesta = self.client.get(f"/admin/productos-terminados/{self.pt.id}/ver")
        self.assertEqual(respuesta.status_code, 200)
        cuerpo = respuesta.get_data(as_text=True)
        self.assertIn(self.pt.nombre, cuerpo)
        self.assertIn(self.pt.sku, cuerpo)

    def test_detalle_producto_terminado_inexistente_da_404(self):
        with self.client.session_transaction() as sesion:
            sesion["admin_logueado"] = True
        self.assertEqual(self.client.get("/admin/productos-terminados/999999/ver").status_code, 404)

    def test_hoja_de_conteo_materias_primas_exige_login_agrupa_y_deja_conteo_en_blanco(self):
        with self.client.session_transaction() as sesion:
            sesion.clear()
        self.assertEqual(self.client.get("/admin/materias-primas/conteo/pdf").status_code, 302)

        with self.client.session_transaction() as sesion:
            sesion["admin_logueado"] = True
        respuesta = self.client.get("/admin/materias-primas/conteo/pdf")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.mimetype, "application/pdf")
        self.assertTrue(respuesta.data.startswith(b"%PDF"))
        self.assertGreater(len(respuesta.data), 1000)

    def test_recetas_pdf_exige_login_y_lista_recetas(self):
        with self.client.session_transaction() as sesion:
            sesion.clear()
        self.assertEqual(self.client.get("/admin/recetas/pdf").status_code, 302)

        with self.client.session_transaction() as sesion:
            sesion["admin_logueado"] = True
        respuesta = self.client.get("/admin/recetas/pdf")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.mimetype, "application/pdf")
        self.assertTrue(respuesta.data.startswith(b"%PDF"))

    def test_receta_detalle_pdf_exige_login_e_incluye_ingredientes(self):
        from proveedores.vendssback import Receta

        receta = Receta.query.filter_by(clave_origen="DET-5L").one()

        with self.client.session_transaction() as sesion:
            sesion.clear()
        self.assertEqual(self.client.get(f"/admin/recetas/{receta.id}/pdf").status_code, 302)

        with self.client.session_transaction() as sesion:
            sesion["admin_logueado"] = True
        respuesta = self.client.get(f"/admin/recetas/{receta.id}/pdf")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.mimetype, "application/pdf")
        self.assertTrue(respuesta.data.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
