"""Documentos del pedido: hoja de recibido firmada, factura, CXC u otro,
como archivos reales (no solo texto) con historial de quien y cuando los
subio, igual que ya existia para documentos de proveedor."""
import io
import unittest

from test_crm import CRMTest
from proveedores.vendssback import PedidoDocumentoCRM


class PedidoDocumentosTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown
    crear_pedido = CRMTest.crear_pedido

    def test_exige_sesion_para_ver_y_subir_documentos(self):
        pedido_id = self.crear_pedido().get_json()["id"]
        with self.client.session_transaction() as sesion:
            sesion.clear()
        self.assertEqual(self.client.get(f"/api/crm/pedidos/{pedido_id}/documentos").status_code, 403)
        self.assertEqual(self.client.post(f"/api/crm/pedidos/{pedido_id}/documentos").status_code, 403)

    def test_subir_documento_lo_deja_disponible_para_descargar(self):
        pedido_id = self.crear_pedido().get_json()["id"]
        archivo = (io.BytesIO(b"%PDF-1.4 contenido de prueba"), "recibido.pdf")
        respuesta = self.client.post(
            f"/api/crm/pedidos/{pedido_id}/documentos",
            data={"tipo_documento": "Recibido firmado", "archivo": archivo},
            content_type="multipart/form-data",
        )
        self.assertEqual(respuesta.status_code, 201, respuesta.get_json())

        listado = self.client.get(f"/api/crm/pedidos/{pedido_id}/documentos").get_json()
        self.assertEqual(len(listado), 1)
        self.assertEqual(listado[0]["tipo_documento"], "Recibido firmado")
        self.assertEqual(listado[0]["nombre_original"], "recibido.pdf")

        descarga = self.client.get(listado[0]["url_descarga"])
        self.assertEqual(descarga.status_code, 200)
        self.assertIn(b"contenido de prueba", descarga.data)

    def test_tipo_de_documento_desconocido_cae_en_otro(self):
        pedido_id = self.crear_pedido().get_json()["id"]
        archivo = (io.BytesIO(b"contenido"), "cxc.pdf")
        self.client.post(
            f"/api/crm/pedidos/{pedido_id}/documentos",
            data={"tipo_documento": "Tipo inventado", "archivo": archivo},
            content_type="multipart/form-data",
        )
        doc = PedidoDocumentoCRM.query.filter_by(pedido_id=pedido_id).one()
        self.assertEqual(doc.tipo_documento, "Otro")

    def test_rechaza_archivo_sin_extension_permitida(self):
        pedido_id = self.crear_pedido().get_json()["id"]
        archivo = (io.BytesIO(b"echo hola"), "script.sh")
        respuesta = self.client.post(
            f"/api/crm/pedidos/{pedido_id}/documentos",
            data={"tipo_documento": "Factura", "archivo": archivo},
            content_type="multipart/form-data",
        )
        self.assertEqual(respuesta.status_code, 400)

    def test_pedido_inexistente_da_404(self):
        respuesta = self.client.get("/api/crm/pedidos/999999/documentos")
        self.assertEqual(respuesta.status_code, 404)


if __name__ == "__main__":
    unittest.main()
