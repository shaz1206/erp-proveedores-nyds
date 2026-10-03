"""Busqueda de clientes del CRM por nombre, apellido, RFC, direccion o
telefono, tanto en el directorio (/crm/clientes) como en el buscador del
formulario de nuevo pedido (/api/crm/clientes)."""
import unittest

from test_crm import CRMTest
from proveedores.vendssback import ClienteCRM


class ClientesCRMTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown

    def alta(self, token, nombre, apellidos, telefono, rfc, direccion):
        respuesta = self.client.post(
            "/api/crm/pedidos",
            json={
                "token": token,
                "canal": "WhatsApp",
                "forma_pago": "Contra entrega",
                "entrega_propia": True,
                "cliente": {"nombre": nombre, "apellidos": apellidos, "telefono": telefono, "rfc": rfc},
                "domicilio": {"direccion": direccion, "zona": "Cancun"},
                "lineas": [{"producto_id": self.pt.id, "cantidad": "1", "precio_unitario": "120.00"}],
            },
        )
        self.assertEqual(respuesta.status_code, 201, respuesta.get_json())
        return respuesta.get_json()

    def buscar(self, q):
        respuesta = self.client.get("/api/crm/clientes", query_string={"q": q})
        self.assertEqual(respuesta.status_code, 200)
        return [c["nombre_completo"] for c in respuesta.get_json()]

    def cargar_clientes(self):
        self.alta("cli-1", "Juan", "Pérez López", "998 123 4567", "pelj800101ab1", "Av. Tulum 45, SM 22")
        self.alta("cli-2", "María", "Gómez", "9987654321", "", "Calle Nichupté 8, Zona Hotelera")

    def test_guarda_apellidos_y_rfc_en_mayusculas(self):
        self.cargar_clientes()
        cliente = ClienteCRM.query.filter_by(nombre="Juan").one()
        self.assertEqual(cliente.apellidos, "Pérez López")
        self.assertEqual(cliente.rfc, "PELJ800101AB1")
        self.assertIsNone(ClienteCRM.query.filter_by(nombre="María").one().rfc)

    def test_rfc_invalido_se_rechaza(self):
        respuesta = self.client.post(
            "/api/crm/pedidos",
            json={
                "token": "cli-mal",
                "cliente": {"nombre": "Ana", "telefono": "9980000001", "rfc": "123"},
                "domicilio": {"direccion": "Calle 1"},
                "lineas": [{"producto_id": self.pt.id, "cantidad": "1", "precio_unitario": "1"}],
            },
        )
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(ClienteCRM.query.filter_by(nombre="Ana").count(), 0)

    def test_busca_por_cada_campo(self):
        self.cargar_clientes()
        self.assertEqual(self.buscar("juan"), ["Juan Pérez López"])
        self.assertEqual(self.buscar("lopez"), ["Juan Pérez López"])  # sin acento
        self.assertEqual(self.buscar("PELJ800101"), ["Juan Pérez López"])
        self.assertEqual(self.buscar("tulum"), ["Juan Pérez López"])
        self.assertEqual(self.buscar("zona hotelera"), ["María Gómez"])
        self.assertEqual(self.buscar("9981234567"), ["Juan Pérez López"])  # con espacios guardado
        self.assertEqual(self.buscar("765-4321"), ["María Gómez"])

    def test_combina_palabras_de_distintos_campos(self):
        self.cargar_clientes()
        self.assertEqual(self.buscar("juan tulum"), ["Juan Pérez López"])
        self.assertEqual(self.buscar("juan nichupte"), [])

    def test_devuelve_domicilios_para_reusar_en_el_pedido(self):
        self.cargar_clientes()
        cliente = self.client.get("/api/crm/clientes", query_string={"q": "gomez"}).get_json()[0]
        self.assertEqual(cliente["domicilios"][0]["direccion"], "Calle Nichupté 8, Zona Hotelera")

        repetido = self.client.post(
            "/api/crm/pedidos",
            json={
                "token": "cli-3",
                "cliente_id": cliente["id"],
                "domicilio_id": cliente["domicilios"][0]["id"],
                "lineas": [{"producto_id": self.pt.id, "cantidad": "1", "precio_unitario": "120.00"}],
            },
        )
        self.assertEqual(repetido.status_code, 201, repetido.get_json())
        self.assertEqual(ClienteCRM.query.count(), 2)

    def test_directorio_filtra_y_muestra_rfc_y_direccion(self):
        self.cargar_clientes()
        cuerpo = self.client.get("/crm/clientes").get_data(as_text=True)
        self.assertIn("Juan Pérez López", cuerpo)
        self.assertIn("María Gómez", cuerpo)
        self.assertIn("PELJ800101AB1", cuerpo)

        cuerpo = self.client.get("/crm/clientes", query_string={"q": "tulum"}).get_data(as_text=True)
        self.assertIn("Juan Pérez López", cuerpo)
        self.assertNotIn("María Gómez", cuerpo)

    def test_directorio_exige_login(self):
        with self.client.session_transaction() as sesion:
            sesion.clear()
        self.assertEqual(self.client.get("/crm/clientes").status_code, 302)
        self.assertEqual(self.client.get("/api/crm/clientes?q=juan").status_code, 403)


if __name__ == "__main__":
    unittest.main()
