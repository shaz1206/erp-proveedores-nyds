"""El tablero (/crm/tablero) agrupa los pedidos reales por estado, con avance
de llenado, notas de estados especiales (pendiente de pago, insumos
insuficientes, cancelado/devuelto/fallido) y metricas, todo calculado a
partir de los datos reales del CRM (nada inventado en la plantilla)."""
import unittest

from test_crm import CRMTest
from proveedores.vendssback import db, OrdenLlenadoCRM, PedidoCRM


class TableroCRMTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown
    crear_pedido = CRMTest.crear_pedido

    def test_exige_login(self):
        with self.client.session_transaction() as sesion:
            sesion.clear()
        self.assertEqual(self.client.get("/crm/tablero").status_code, 302)

    def test_pedido_recibido_aparece_con_metricas_reales(self):
        self.crear_pedido(token="tab-1", cantidad="1")
        respuesta = self.client.get("/crm/tablero")
        self.assertEqual(respuesta.status_code, 200)
        cuerpo = respuesta.get_data(as_text=True)
        self.assertIn("Flujo de pedidos", cuerpo)
        self.assertIn("Cliente prueba", cuerpo)
        self.assertIn("$120", cuerpo)
        self.assertIn("WhatsApp", cuerpo)
        self.assertIn("1 pedidos activos", cuerpo)

    def test_confirmado_muestra_avance_de_llenado_real(self):
        pedido_id = self.crear_pedido(token="tab-2", cantidad="1").get_json()["id"]
        confirmacion = self.client.post(f"/api/crm/pedidos/{pedido_id}/confirmar", json={})
        self.assertEqual(confirmacion.get_json()["estado"], "Confirmado")

        orden = OrdenLlenadoCRM.query.filter_by(pedido_id=pedido_id).one()
        cuerpo = self.client.get("/crm/tablero").get_data(as_text=True)
        self.assertIn("LLENADO", cuerpo)
        self.assertIn("0%", cuerpo)

        completar = self.client.post(f"/api/crm/ordenes-llenado/{orden.id}/completar", json={})
        self.assertEqual(completar.status_code, 200, completar.get_json())
        cuerpo = self.client.get("/crm/tablero").get_data(as_text=True)
        self.assertIn("100%", cuerpo)

    def test_pendiente_de_pago_muestra_nota_con_saldo_real(self):
        alta = self.client.post(
            "/api/crm/pedidos",
            json={
                "token": "tab-3",
                "canal": "Manual",
                "forma_pago": "Transferencia",
                "pago_requerido_antes_preparar": True,
                "entrega_propia": True,
                "cliente": {"nombre": "Hotel de prueba", "telefono": "9981111111"},
                "domicilio": {"direccion": "Av. Prueba 1", "zona": "Cancun"},
                "lineas": [{"producto_id": self.pt.id, "cantidad": "1", "precio_unitario": "200.00"}],
            },
        )
        pedido_id = alta.get_json()["id"]
        confirmacion = self.client.post(f"/api/crm/pedidos/{pedido_id}/confirmar", json={})
        self.assertEqual(confirmacion.get_json()["estado"], "Pendiente de pago")

        cuerpo = self.client.get("/crm/tablero").get_data(as_text=True)
        self.assertIn("Hotel de prueba", cuerpo)
        self.assertIn("$200", cuerpo)
        self.assertIn("pendientes de anticipo", cuerpo)

    def test_cerrados_con_incidencia_quedan_marcados_en_entregado(self):
        pedido_id = self.crear_pedido(token="tab-4", cantidad="1").get_json()["id"]
        pedido = db.session.get(PedidoCRM, pedido_id)
        pedido.estado = "Cancelado"
        db.session.commit()

        cuerpo = self.client.get("/crm/tablero").get_data(as_text=True)
        self.assertIn("Pedido cancelado", cuerpo)

    def test_entrada_al_crm_aterriza_en_el_tablero(self):
        # /crm es la ruta a la que apunta el boton "Abrir CRM" del ERP:
        # debe llevar al tablero, no a la lista clasica.
        respuesta = self.client.get("/crm", follow_redirects=False)
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(respuesta.headers["Location"], "/crm/tablero")

    def test_login_del_crm_aterriza_en_el_tablero(self):
        from proveedores.vendssback import Persona
        from werkzeug.security import generate_password_hash

        db.session.add(Persona(
            nombre="Ventas de prueba",
            usuario="ventas.nyds",
            password_hash=generate_password_hash("clave-ventas-prueba"),
            rol="ventas",
            activo=True,
        ))
        db.session.commit()

        with self.client.session_transaction() as sesion:
            sesion.clear()
        respuesta = self.client.post(
            "/crm/login",
            data={"usuario": "ventas.nyds", "password": "clave-ventas-prueba"},
            follow_redirects=False,
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(respuesta.headers["Location"], "/crm/tablero")


if __name__ == "__main__":
    unittest.main()
