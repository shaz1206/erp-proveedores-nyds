"""ERP: inventario de producto terminado, resumen de inventario de materia
prima, balance por cliente, procedimiento de recetas y evidencia de compras."""
import io
import json
import os
import unittest
from datetime import date

# Se importan los modulos (no las clases) para no volver a correr sus pruebas aqui.
import test_abastecimiento
import test_crm
import test_recetario
from proveedores.vendssback import ClienteCRM, CompraDocumento, MovimientoPT, Receta, RecepcionMP, db

PDF = b"%PDF-1.4\n%prueba\n"
PNG = b"\x89PNG\r\n\x1a\nprueba"


class InventarioPTyClientesTest(unittest.TestCase):
    setUp = test_crm.CRMTest.setUp
    tearDown = test_crm.CRMTest.tearDown
    crear_pedido = test_crm.CRMTest.crear_pedido

    def csrf_pt(self):
        self.client.get("/admin/inventario-pt")
        with self.client.session_transaction() as s:
            return s["inventario_pt_csrf"]

    def movimiento_pt(self, tipo, cantidad, token):
        return self.client.post("/admin/inventario-pt", data={
            "csrf": self.csrf_pt(), "token": token, "producto_id": str(self.pt.id), "tipo": tipo,
            "cantidad": cantidad, "referencia": "Conteo", "motivo": "Prueba",
        })

    def test_inventario_pt_saldo_produccion_reservas_y_salidas(self):
        self.assertEqual(self.movimiento_pt("Saldo inicial", "3", "pt-1").status_code, 302)
        self.assertEqual(self.movimiento_pt("Saldo inicial", "3", "pt-2").status_code, 400)
        self.assertEqual(self.movimiento_pt("Produccion", "2", "pt-3").status_code, 302)
        self.movimiento_pt("Produccion", "2", "pt-3")  # mismo token: no duplica
        self.assertEqual(sum(m.cantidad for m in MovimientoPT.query.all()), 5)

        # Un pedido de 4 reserva producto terminado: solo queda 1 libre para sacar.
        pedido_id = self.crear_pedido(token="res-pt", cantidad="4").get_json()["id"]
        self.client.post(f"/api/crm/pedidos/{pedido_id}/confirmar", json={})
        self.assertEqual(self.movimiento_pt("Ajuste de salida", "2", "pt-4").status_code, 400)
        self.assertEqual(self.movimiento_pt("Ajuste de salida", "1", "pt-5").status_code, 302)

        cuerpo = self.client.get("/admin/inventario-pt").get_data(as_text=True)
        self.assertIn("Inventario de producto terminado", cuerpo)
        self.assertIn("Detergente color 5 L", cuerpo)
        self.assertIn("Todo reservado", cuerpo)
        pdf = self.client.get("/admin/inventario-pt/pdf")
        self.assertEqual(pdf.mimetype, "application/pdf")

    def test_inventario_mp_muestra_existencias_y_reservas_por_materia(self):
        pedido_id = self.crear_pedido(token="res-mp").get_json()["id"]
        self.client.post(f"/api/crm/pedidos/{pedido_id}/confirmar", json={})
        cuerpo = self.client.get("/admin/inventario-mp").get_data(as_text=True)
        self.assertIn("Existencias por materia prima", cuerpo)
        self.assertIn("Detergente a granel", cuerpo)
        # 20 L de existencia, 5 L reservados para el llenado del pedido -> 15 disponibles.
        self.assertIn("<strong>15</strong>", cuerpo)
        self.assertTrue(self.client.get("/admin/inventario-mp/pdf").data.startswith(b"%PDF"))

    def test_balance_por_cliente(self):
        db.session.add(MovimientoPT(producto_id=self.pt.id, tipo="Saldo inicial", cantidad=5, ubicacion="Almacen PT", referencia="INI", usuario="t", token="ini"))
        db.session.commit()
        entregado = self.crear_pedido(token="cli-1", cantidad="2").get_json()["id"]
        self.client.post(f"/api/crm/pedidos/{entregado}/confirmar", json={})
        self.client.post(f"/api/crm/pedidos/{entregado}/entrega", json={"entregado": True, "receptor": "Marta"})
        self.client.post(f"/api/crm/pedidos/{entregado}/pagos", json={"monto": "100", "metodo": "Efectivo", "referencia": "Caja"})
        pendiente = self.crear_pedido(token="cli-2", cantidad="1").get_json()["id"]
        cliente = ClienteCRM.query.one()

        lista = self.client.get("/admin/clientes").get_data(as_text=True)
        self.assertIn("Cliente prueba", lista)
        self.assertIn("$260.00", lista)  # 240 + 120 comprado - 100 pagado
        self.assertIn("recibió Marta", lista)

        detalle = self.client.get(f"/admin/clientes/{cliente.id}").get_data(as_text=True)
        for texto in ("Estado de cuenta", "Pedido PED-000001", "Pago Efectivo", "Recibió Marta", "Productos entregados", "Detergente color 5 L", "Pendiente"):
            self.assertIn(texto, detalle)
        self.assertTrue(self.client.get(f"/admin/clientes/{cliente.id}/pdf").data.startswith(b"%PDF"))
        self.assertTrue(self.client.get("/admin/clientes/pdf").data.startswith(b"%PDF"))
        self.assertIsNotNone(pendiente)

        with self.client.session_transaction() as s:
            s.clear()
        self.assertEqual(self.client.get(f"/admin/clientes/{cliente.id}").status_code, 302)


class ProcedimientoRecetaTest(unittest.TestCase):
    setUp = test_recetario.RecetarioTest.setUp
    tearDown = test_recetario.RecetarioTest.tearDown
    editar = test_recetario.RecetarioTest.editar

    def test_procedimiento_por_bloques_en_el_orden_enviado(self):
        r = Receta.query.first()
        # El orden de los bloques es el orden en que llegan del formulario; los bloques vacios se ignoran.
        respuesta = self.editar(r, paso_titulo=["Envasado", "", "Mezclado"], paso_texto=["Llenar los botes", "", "Disolver la sal\n5 minutos"])
        self.assertEqual(respuesta.status_code, 302)
        db.session.refresh(r)
        self.assertEqual(
            json.loads(r.procedimiento),
            [{"titulo": "Envasado", "texto": "Llenar los botes"}, {"titulo": "Mezclado", "texto": "Disolver la sal\n5 minutos"}],
        )
        cuerpo = self.c.get(f"/admin/recetas/{r.id}").get_data(as_text=True)
        self.assertLess(cuerpo.index('value="Envasado"'), cuerpo.index('value="Mezclado"'))
        self.assertNotIn("&#34;version&#34;", cuerpo)  # ya no se muestra el JSON crudo

        # Reordenar: se envian al reves.
        self.editar(r, paso_titulo=["Mezclado", "Envasado"], paso_texto=["Disolver la sal", "Llenar los botes"], motivo="Reordenar")
        db.session.refresh(r)
        self.assertEqual([b["titulo"] for b in json.loads(r.procedimiento)], ["Mezclado", "Envasado"])
        cuerpo = self.c.get(f"/admin/recetas/{r.id}").get_data(as_text=True)
        self.assertIn("Ver cómo estaba la revisión 2", cuerpo)
        self.assertIn("<li><strong>Envasado:</strong> Llenar los botes</li>", cuerpo)
        self.assertIn("Revisión 3 · 2 pasos", self.c.get("/admin/recetas").get_data(as_text=True))
        self.assertTrue(self.c.get(f"/admin/recetas/{r.id}/pdf").data.startswith(b"%PDF"))

        # Un bloque con titulo pero sin instrucciones no se guarda, y el formulario conserva lo escrito.
        error = self.editar(r, paso_titulo=["Solo título"], paso_texto=[""], motivo="x")
        self.assertEqual(error.status_code, 400)
        self.assertIn('value="Solo título"', error.get_data(as_text=True))

    def test_procedimiento_en_texto_anterior_se_lee_como_bloques(self):
        r = Receta.query.first()
        r.procedimiento = "1. Agregar el agua\n2) Disolver la sal\n2-3 gotas de color"
        db.session.commit()
        cuerpo = self.c.get(f"/admin/recetas/{r.id}").get_data(as_text=True)
        for texto in (">Agregar el agua</textarea>", ">Disolver la sal</textarea>", ">2-3 gotas de color</textarea>"):
            self.assertIn(texto, cuerpo)


class EvidenciaComprasTest(unittest.TestCase):
    setUp_base = test_abastecimiento.ComprasTest.setUp
    tearDown_base = test_abastecimiento.ComprasTest.tearDown
    emitir = test_abastecimiento.ComprasTest.emitir

    def setUp(self):
        self.setUp_base()
        self.archivos_previos = set(self.listar())

    def tearDown(self):
        for archivo in set(self.listar()) - self.archivos_previos:
            os.remove(os.path.join(self.carpeta(), archivo))
        self.tearDown_base()

    def carpeta(self):
        from proveedores.vendssback import app
        return os.path.join(app.config["UPLOAD_FOLDER"], "compras")

    def listar(self):
        return os.listdir(self.carpeta()) if os.path.isdir(self.carpeta()) else []

    def test_subir_evidencia_a_orden_y_recepcion(self):
        self.emitir()
        subir = self.c.post("/admin/compras/1/evidencia", data={
            "csrf": self.csrf, "tipo": "Factura", "descripcion": "Factura A-1",
            "archivos": [(io.BytesIO(PDF), "factura.pdf"), (io.BytesIO(PNG), "foto.png")],
        }, content_type="multipart/form-data")
        self.assertEqual(subir.status_code, 302)
        self.assertEqual(CompraDocumento.query.count(), 2)

        disfrazado = self.c.post("/admin/compras/1/evidencia", data={
            "csrf": self.csrf, "tipo": "Factura", "archivos": [(io.BytesIO(PNG), "factura.pdf")],
        }, content_type="multipart/form-data")
        self.assertEqual(disfrazado.status_code, 400)
        self.assertEqual(self.c.post("/admin/compras/1/evidencia", data={"csrf": self.csrf, "tipo": "Factura"}, content_type="multipart/form-data").status_code, 400)
        self.assertEqual(CompraDocumento.query.count(), 2)

        doc = CompraDocumento.query.filter_by(nombre_original="factura.pdf").one()
        descarga = self.c.get(f"/admin/compras/evidencia/{doc.id}")
        self.assertEqual(descarga.data, PDF)
        descarga.close()

        from proveedores.vendssback import CompraLinea
        linea = CompraLinea.query.first()
        recibir = self.c.post("/admin/compras/1", data={
            "csrf": self.csrf, "token": "rec-ev", "linea": str(linea.id), "aceptada": "2", "rechazada": "0",
            "ubicacion": "Central", "referencia": "Remisión 9", "conforme": "si", "evidencia_tipo": "Remisión",
            "evidencia": [(io.BytesIO(PDF), "remision.pdf")],
        }, content_type="multipart/form-data")
        self.assertEqual(recibir.status_code, 302)
        rec = RecepcionMP.query.one()
        self.assertEqual(CompraDocumento.query.filter_by(recepcion_id=rec.id).one().tipo, "Remisión")

        detalle = self.c.get("/admin/compras/1").get_data(as_text=True)
        for texto in ("Evidencia de la compra", "factura.pdf", "remision.pdf", f"REC-{rec.id}"):
            self.assertIn(texto, detalle)
        self.assertIn("3 archivo(s)", self.c.get("/admin/compras").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
