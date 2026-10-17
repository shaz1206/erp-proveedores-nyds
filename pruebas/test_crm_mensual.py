"""CRM por meses: alta manual de clientes (solo nombre obligatorio), ruta con
siguiente ruta o entrega urgente con cargo, trueque como forma de pago con
resumen por negocio, metricas "generado este mes" y PDF de historicos."""
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

import test_crm  # el modulo, no la clase, para no repetir sus pruebas aqui
from proveedores.vendssback import ClienteCRM, MovimientoPT, PedidoCRM, db


class CRMMensualTest(unittest.TestCase):
    setUp_base = test_crm.CRMTest.setUp
    tearDown = test_crm.CRMTest.tearDown
    crear_pedido = test_crm.CRMTest.crear_pedido

    def setUp(self):
        self.setUp_base()
        # Producto terminado en almacen: los pedidos se reservan directo, sin llenado.
        db.session.add(MovimientoPT(producto_id=self.pt.id, tipo="Saldo inicial", cantidad=10, ubicacion="Almacen PT", referencia="PT-INI", usuario="test", token="pt-ini"))
        db.session.commit()

    def pedido_confirmado(self, token, telefono="9981000000", nombre="Cliente prueba"):
        respuesta = self.client.post(
            "/api/crm/pedidos",
            json={
                "token": token,
                "cliente": {"nombre": nombre, "telefono": telefono},
                "domicilio": {"direccion": "Av. Talleres 123", "zona": "Centro"},
                "lineas": [{"producto_id": self.pt.id, "cantidad": "1", "precio_unitario": "120.00"}],
            },
        )
        self.assertEqual(respuesta.status_code, 201, respuesta.get_json())
        pedido_id = respuesta.get_json()["id"]
        confirmacion = self.client.post(f"/api/crm/pedidos/{pedido_id}/confirmar", json={})
        self.assertEqual(confirmacion.get_json()["estado"], "Confirmado")
        return pedido_id

    # --- Clientes ---

    def test_alta_manual_solo_exige_nombre(self):
        # Regla vigente: solo el nombre es obligatorio; telefono, RFC, correo y direccion son opcionales.
        respuesta = self.client.post("/api/crm/clientes", json={"nombre": "Taller Gómez"})
        self.assertEqual(respuesta.status_code, 201, respuesta.get_json())
        self.assertEqual(self.client.post("/api/crm/clientes", json={"nombre": "Otro sin teléfono"}).status_code, 201)
        cliente = ClienteCRM.query.filter_by(nombre="Taller Gómez").one()
        self.assertEqual(cliente.telefono, "")
        self.assertIsNone(cliente.rfc)
        self.assertEqual(cliente.domicilios, [])
        self.assertIn("Taller Gómez", self.client.get("/crm/clientes").get_data(as_text=True))

        self.assertEqual(self.client.post("/api/crm/clientes", json={"telefono": "998"}).status_code, 400)
        self.client.post("/api/crm/clientes", json={"nombre": "Con tel", "telefono": "9982223333", "direccion": "Calle 5"})
        self.assertEqual(self.client.post("/api/crm/clientes", json={"nombre": "Dup", "telefono": "9982223333"}).status_code, 400)
        self.assertEqual(ClienteCRM.query.filter_by(nombre="Con tel").one().domicilios[0].direccion, "Calle 5")

    def test_atajos_vender_y_cobrar(self):
        alta = self.client.post("/api/crm/clientes", json={"nombre": "Taller Atajo"})
        cliente = ClienteCRM.query.filter_by(nombre="Taller Atajo").one()
        uno = self.client.get(f"/api/crm/clientes/{cliente.id}")
        self.assertEqual((uno.status_code, uno.get_json()["nombre_completo"]), (200, "Taller Atajo"))
        self.assertEqual(self.client.get("/api/crm/clientes/99999").status_code, 404)
        self.assertEqual(test_crm.app.test_client().get(f"/api/crm/clientes/{cliente.id}").status_code, 403)
        # Clientes ofrece Vender; Pagos ofrece Cobrar pendientes y enlaza cada folio con su pedido.
        self.assertIn(f"/crm/pedidos?cliente={cliente.id}", self.client.get("/crm/clientes").get_data(as_text=True))
        self.assertIn("/crm/pedidos?filtro=cobrar", self.client.get("/crm/pagos").get_data(as_text=True))
        pedidos = self.client.get("/crm/pedidos?filtro=cobrar&cliente=" + str(cliente.id))
        self.assertEqual(pedidos.status_code, 200)

    def test_pedido_sin_telefono_no_se_mezcla_con_otro_cliente_sin_telefono(self):
        self.client.post("/api/crm/clientes", json={"nombre": "Existente sin tel"})
        respuesta = self.client.post(
            "/api/crm/pedidos",
            json={
                "token": "sin-tel",
                "cliente": {"nombre": "Nuevo sin tel"},
                "domicilio": {"direccion": "Calle 1"},
                "lineas": [{"producto_id": self.pt.id, "cantidad": "1", "precio_unitario": "10"}],
            },
        )
        self.assertEqual(respuesta.status_code, 201, respuesta.get_json())
        self.assertEqual(respuesta.get_json()["cliente"], "Nuevo sin tel")

    # --- Ruta ---

    def test_ruta_aceptar_hoy_siguiente_y_urgente_con_cargo(self):
        primero = self.pedido_confirmado("ruta-1")
        segundo = self.pedido_confirmado("ruta-2", telefono="9981000001", nombre="Segundo")

        # La ruta de Centro aun no pasa: se acepta hoy sin cargo.
        hoy = self.client.post(f"/api/crm/pedidos/{primero}/ruta", json={"accion": "hoy"})
        self.assertEqual(hoy.status_code, 200, hoy.get_json())
        self.assertFalse(hoy.get_json()["urgente"])
        self.assertEqual(hoy.get_json()["fecha_ruta"], datetime.utcnow().date().isoformat())
        self.assertEqual(self.client.post(f"/api/crm/pedidos/{primero}/estado", json={"estado": "En ruta"}).status_code, 200)

        # Ya paso la ruta de Centro: hoy solo como urgente y con cargo.
        sin_cargo = self.client.post(f"/api/crm/pedidos/{segundo}/ruta", json={"accion": "hoy"})
        self.assertEqual(sin_cargo.status_code, 400)
        self.assertIn("ya paso", sin_cargo.get_json()["error"])
        cuerpo = self.client.get("/crm/rutas").get_data(as_text=True)
        self.assertIn("La ruta de Centro ya pasó hoy", cuerpo)

        urgente = self.client.post(f"/api/crm/pedidos/{segundo}/ruta", json={"accion": "hoy", "cargo_urgente": "50"})
        self.assertEqual(urgente.status_code, 200, urgente.get_json())
        self.assertTrue(urgente.get_json()["urgente"])
        self.assertEqual(urgente.get_json()["importe_total"], 170.0)

        # Mandarlo a la siguiente ruta quita el cargo urgente.
        siguiente = self.client.post(f"/api/crm/pedidos/{segundo}/ruta", json={"accion": "siguiente"})
        self.assertEqual(siguiente.status_code, 200, siguiente.get_json())
        self.assertFalse(siguiente.get_json()["urgente"])
        self.assertEqual(siguiente.get_json()["importe_total"], 120.0)
        self.assertEqual(siguiente.get_json()["fecha_ruta"], (datetime.utcnow().date() + timedelta(days=1)).isoformat())

        hoy_txt = datetime.utcnow().date().isoformat()
        self.assertEqual(self.client.post(f"/api/crm/pedidos/{segundo}/ruta", json={"accion": "siguiente", "fecha": hoy_txt}).status_code, 400)
        # Un pedido que ya salio no se reprograma.
        self.assertEqual(self.client.post(f"/api/crm/pedidos/{primero}/ruta", json={"accion": "siguiente"}).status_code, 400)

    # --- Trueque ---

    def test_pago_con_trueque_y_resumen_por_negocio(self):
        pedido_id = self.pedido_confirmado("trueque-1", nombre="Restaurante Sol")
        cliente_id = db.session.get(PedidoCRM, pedido_id).cliente_id
        giro = self.client.post("/api/crm/categorias-trueque", json={"nombre": "Restaurante"}).get_json()
        abono = self.client.post(f"/api/crm/clientes/{cliente_id}/trueque", json={"categoria_id": giro["id"], "monto": "300", "descripcion": "Comidas"})
        self.assertEqual(abono.get_json()["saldo_trueque"], 300.0)

        malo = self.client.post(f"/api/crm/pedidos/{pedido_id}/pagos", json={"monto": "10", "metodo": "Bitcoin", "referencia": "x"})
        self.assertEqual(malo.status_code, 400)

        pago = self.client.post(
            f"/api/crm/pedidos/{pedido_id}/pagos",
            json={"monto": "120", "metodo": "Trueque", "categoria_trueque_id": giro["id"], "porcentaje_trueque": "100", "referencia": "Canje"},
        )
        self.assertEqual(pago.status_code, 201, pago.get_json())
        self.assertEqual(pago.get_json()["saldo_trueque_cliente"], 180.0)

        cuerpo = self.client.get("/crm/pagos").get_data(as_text=True)
        self.assertIn("Trueque por negocio", cuerpo)
        self.assertIn("Restaurante Sol", cuerpo)
        for importe in ("+$300.00", "−$120.00", "$180.00"):
            self.assertIn(importe, cuerpo)

        # Con $180 a favor alcanza para un pedido mas de $120, pero no para otro.
        respuestas = []
        for token in ("trueque-2", "trueque-3"):
            otro = self.pedido_confirmado(token, telefono=f"99810000{token[-1]}", nombre="Sucursal")
            db.session.get(PedidoCRM, otro).cliente_id = cliente_id
            db.session.commit()
            respuestas.append(self.client.post(
                f"/api/crm/pedidos/{otro}/pagos",
                json={"monto": "120", "metodo": "Trueque", "categoria_trueque_id": giro["id"], "porcentaje_trueque": "100", "referencia": "Canje"},
            ))
        self.assertEqual(respuestas[0].status_code, 201)
        self.assertEqual(respuestas[1].status_code, 400)
        self.assertIn("60.00", respuestas[1].get_json()["error"])

    # --- Meses ---

    def test_tablero_generado_este_mes_y_meses_anteriores(self):
        actual = self.pedido_confirmado("mes-1")
        viejo = self.pedido_confirmado("mes-2", telefono="9981000003", nombre="Pedido viejo")
        pedido_viejo = db.session.get(PedidoCRM, viejo)
        pedido_viejo.fecha = datetime.utcnow().replace(day=1) - timedelta(days=20)
        db.session.commit()
        mes_pasado = pedido_viejo.fecha.strftime("%Y-%m")

        cuerpo = self.client.get("/crm/tablero").get_data(as_text=True)
        self.assertIn("Generado este mes", cuerpo)
        self.assertNotIn("Valor en gesti", cuerpo)
        self.assertIn("$120", cuerpo)

        anterior = self.client.get(f"/crm/tablero?mes={mes_pasado}").get_data(as_text=True)
        self.assertIn("Generado en", anterior)

        reporte = self.client.get(f"/crm/reportes?mes={mes_pasado}").get_data(as_text=True)
        self.assertIn("Pedido viejo", reporte)
        self.assertIn("Histórico de los últimos 12 meses", reporte)
        # En pedidos del mes actual sigue apareciendo el pedido viejo porque sigue abierto.
        pedidos = self.client.get("/crm/pedidos").get_data(as_text=True)
        self.assertIn(f"pedido-{viejo}", pedidos)
        self.assertIn(f"pedido-{actual}", pedidos)

    def test_todos_los_pdf_del_crm(self):
        pedido_id = self.pedido_confirmado("pdf-1", nombre="Juan & <Hijos>")
        cliente_id = db.session.get(PedidoCRM, pedido_id).cliente_id
        mes = datetime.utcnow().strftime("%Y-%m")
        rutas = [
            "/crm/tablero/pdf", "/crm/clientes/pdf", f"/crm/clientes/{cliente_id}/pdf", "/crm/pedidos/pdf",
            "/crm/rutas/pdf", "/crm/rutas/historial/pdf", "/crm/pagos/pdf", "/crm/reportes/pdf",
        ]
        for ruta in rutas:
            respuesta = self.client.get(f"{ruta}?mes={mes}")
            self.assertEqual(respuesta.status_code, 200, ruta)
            self.assertEqual(respuesta.mimetype, "application/pdf", ruta)
            self.assertTrue(respuesta.data.startswith(b"%PDF"), ruta)

    def test_mes_invalido_usa_el_actual(self):
        self.assertEqual(self.client.get("/crm/pagos?mes=2026-13").status_code, 200)
        self.assertEqual(self.client.get("/crm/reportes?mes=basura").status_code, 200)


if __name__ == "__main__":
    unittest.main()
