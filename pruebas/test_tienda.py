"""Tienda en linea: cuenta de cliente (no hay compra como invitado), catalogo
publico, carrito en sesion y checkout. Reglas de negocio reforzadas:
- El precio SIEMPRE se cobra desde el catalogo (ProductoTerminado.precio) al
  momento de pagar, nunca el que pudiera quedar cacheado en el carrito.
- No se deja pedir mas de lo que hay fisicamente disponible en almacen (el
  carrito se ajusta solo, y el checkout vuelve a revisar justo antes de
  crear el pedido por si el stock cambio mientras el cliente armaba su
  carrito).
- Un domicilio marcado fuera de cobertura no puede usarse para pagar."""
import secrets
import unittest
from decimal import Decimal

from test_crm import CRMTest
from proveedores.tienda import UBICACION_PT
from proveedores.vendssback import ClienteCRM, ClienteDomicilio, MovimientoPT, PedidoCRM, db


class TiendaTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown

    def fijar_precio(self, valor="120.00"):
        self.pt.precio = Decimal(valor)
        db.session.commit()

    def agregar_stock(self, cantidad="10"):
        """Da de alta existencia fisica de self.pt en el almacen de producto
        terminado, como si ya se hubiera llenado/producido."""
        db.session.add(MovimientoPT(
            producto_id=self.pt.id,
            tipo="Alta por llenado",
            cantidad=Decimal(cantidad),
            unidad="pieza",
            ubicacion=UBICACION_PT,
            referencia="TEST-STOCK",
            usuario="test",
            token=secrets.token_hex(8),
        ))
        db.session.commit()

    def consumir_stock(self, cantidad="1"):
        """Simula que el stock fisico bajo por otra via (venta de mostrador,
        ajuste, etc.) despues de que el cliente ya armo su carrito."""
        db.session.add(MovimientoPT(
            producto_id=self.pt.id,
            tipo="Ajuste manual",
            cantidad=Decimal(cantidad) * -1,
            unidad="pieza",
            ubicacion=UBICACION_PT,
            referencia="TEST-CONSUMO",
            usuario="test",
            token=secrets.token_hex(8),
        ))
        db.session.commit()

    def salir_sesion_staff(self):
        with self.client.session_transaction() as sesion:
            sesion.clear()

    def registrar_cliente(self, telefono="9981112222", password="secreto1", nombre="Cliente Tienda", correo=""):
        return self.client.post(
            "/tienda/registro",
            data={
                "nombre": nombre,
                "telefono": telefono,
                "correo": correo,
                "password": password,
                "password_confirmar": password,
            },
        )

    # ---- Catalogo ----

    def test_catalogo_solo_muestra_productos_con_precio_y_activos(self):
        self.salir_sesion_staff()
        self.assertNotIn(b"Detergente color 5 L", self.client.get("/tienda").data)
        self.fijar_precio("150.00")
        self.assertIn(b"Detergente color 5 L", self.client.get("/tienda").data)
        self.pt.estatus = "Inactivo"
        db.session.commit()
        self.assertNotIn(b"Detergente color 5 L", self.client.get("/tienda").data)

    def test_catalogo_muestra_agotado_y_oculta_formulario_sin_stock(self):
        self.salir_sesion_staff()
        self.fijar_precio("150.00")
        pagina = self.client.get("/tienda").data
        self.assertIn(b"Agotado", pagina)
        self.assertNotIn(b"/tienda/catalogo/agregar/", pagina)
        self.agregar_stock("5")
        pagina = self.client.get("/tienda").data
        self.assertNotIn(b"Agotado", pagina)
        self.assertIn(b"/tienda/catalogo/agregar/", pagina)

    # ---- Cuentas de cliente ----

    def test_registro_crea_cuenta_y_abre_sesion_de_cliente(self):
        self.salir_sesion_staff()
        respuesta = self.registrar_cliente()
        self.assertEqual(respuesta.status_code, 302)
        cliente = ClienteCRM.query.filter_by(telefono="9981112222").one()
        self.assertIsNotNone(cliente.password_hash)
        with self.client.session_transaction() as sesion:
            self.assertEqual(sesion.get("cliente_id"), cliente.id)

    def test_registro_reclama_cliente_existente_capturado_a_mano(self):
        self.salir_sesion_staff()
        existente = ClienteCRM(nombre="Capturado por ventas", telefono="9980001111", canal_origen="Manual")
        db.session.add(existente)
        db.session.commit()
        id_existente = existente.id
        respuesta = self.registrar_cliente(telefono="9980001111")
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(ClienteCRM.query.count(), 1)
        cliente = db.session.get(ClienteCRM, id_existente)
        self.assertIsNotNone(cliente.password_hash)

    def test_registro_rechaza_telefono_que_ya_tiene_cuenta(self):
        self.salir_sesion_staff()
        self.registrar_cliente(telefono="9981112222")
        self.salir_sesion_staff()
        respuesta = self.registrar_cliente(telefono="9981112222", password="otraclave")
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(ClienteCRM.query.filter_by(telefono="9981112222").count(), 1)

    def test_registro_rechaza_contrasenas_que_no_coinciden(self):
        self.salir_sesion_staff()
        respuesta = self.client.post(
            "/tienda/registro",
            data={"nombre": "X", "telefono": "9980000000", "correo": "", "password": "abcdef", "password_confirmar": "distinta"},
        )
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(ClienteCRM.query.count(), 0)

    def test_login_correcto_e_incorrecto(self):
        self.salir_sesion_staff()
        self.registrar_cliente(telefono="9981234567", password="correcta1")
        self.salir_sesion_staff()
        malo = self.client.post("/tienda/login", data={"telefono": "9981234567", "password": "incorrecta"})
        self.assertEqual(malo.status_code, 401)
        bueno = self.client.post("/tienda/login", data={"telefono": "9981234567", "password": "correcta1"})
        self.assertEqual(bueno.status_code, 302)
        with self.client.session_transaction() as sesion:
            self.assertTrue(sesion.get("cliente_id"))

    def test_login_rechaza_cliente_sin_cuenta_tienda(self):
        self.salir_sesion_staff()
        db.session.add(ClienteCRM(nombre="Solo manual", telefono="9989999999", canal_origen="Manual"))
        db.session.commit()
        respuesta = self.client.post("/tienda/login", data={"telefono": "9989999999", "password": "lo-que-sea"})
        self.assertEqual(respuesta.status_code, 401)

    # ---- Carrito y checkout ----

    def test_agregar_al_carrito_sin_cuenta_y_ver_total(self):
        self.salir_sesion_staff()
        self.fijar_precio("120.00")
        self.agregar_stock("10")
        respuesta = self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "3"})
        self.assertEqual(respuesta.status_code, 302)
        carrito = self.client.get("/tienda/carrito").data
        self.assertIn(b"360.00", carrito)

    def test_catalogo_agregar_sin_stock_no_mete_nada_al_carrito(self):
        self.salir_sesion_staff()
        self.fijar_precio("120.00")
        respuesta = self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "1"})
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("sin_stock", respuesta.headers["Location"])
        self.assertIn(b"carrito est\xc3\xa1 vac\xc3\xado", self.client.get("/tienda/carrito").data)

    def test_agregar_mas_de_lo_disponible_se_limita_al_stock(self):
        self.salir_sesion_staff()
        self.fijar_precio("10.00")
        self.agregar_stock("5")
        respuesta = self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "20"})
        self.assertIn("limitado=1", respuesta.headers["Location"])
        carrito = self.client.get("/tienda/carrito").data
        self.assertIn(b"50.00", carrito)  # 5 piezas (todo el stock) x $10.00

    def test_actualizar_cantidad_en_carrito_se_limita_al_stock(self):
        self.salir_sesion_staff()
        self.fijar_precio("10.00")
        self.agregar_stock("3")
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "2"})
        respuesta = self.client.post(f"/tienda/carrito/actualizar/{self.pt.id}", data={"cantidad": "100"})
        self.assertIn("limitado=1", respuesta.headers["Location"])
        carrito = self.client.get("/tienda/carrito").data
        self.assertIn(b"30.00", carrito)  # se limito a las 3 piezas disponibles

    def test_checkout_exige_cuenta_de_cliente(self):
        self.salir_sesion_staff()
        self.fijar_precio("120.00")
        self.agregar_stock("10")
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "1"})
        respuesta = self.client.get("/tienda/checkout")
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/tienda/login", respuesta.headers["Location"])

    def test_checkout_completo_crea_pedido_y_vacia_carrito(self):
        self.salir_sesion_staff()
        self.fijar_precio("120.00")
        self.agregar_stock("10")
        self.registrar_cliente(telefono="9987776655")
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "2"})
        respuesta = self.client.post(
            "/tienda/checkout",
            data={"domicilio_id": "", "direccion": "Calle Falsa 123", "referencias": "Casa azul", "zona": "Cancun", "forma_pago": "Transferencia"},
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/tienda/pedido/", respuesta.headers["Location"])

        pedido = PedidoCRM.query.one()
        self.assertEqual(pedido.canal, "Tienda en línea")
        self.assertEqual(pedido.forma_pago, "Transferencia")
        self.assertEqual(pedido.estado, "Recibido")
        self.assertEqual(float(pedido.importe_total), 240.00)
        self.assertEqual(pedido.domicilio.direccion, "Calle Falsa 123")

        carrito_vacio = self.client.get("/tienda/carrito").data
        self.assertIn(b"carrito est\xc3\xa1 vac\xc3\xado", carrito_vacio)

    def test_checkout_cobra_el_precio_vigente_del_catalogo_no_uno_viejo(self):
        self.salir_sesion_staff()
        self.fijar_precio("100.00")
        self.agregar_stock("10")
        self.registrar_cliente(telefono="9981112233")
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "1"})
        # Ventas cambia el precio de lista despues de que el cliente agrego al carrito.
        self.fijar_precio("250.00")
        self.client.post(
            "/tienda/checkout",
            data={"domicilio_id": "", "direccion": "Otra calle 55", "zona": "Cancun", "forma_pago": "Efectivo"},
        )
        pedido = PedidoCRM.query.one()
        self.assertEqual(float(pedido.importe_total), 250.00)

    def test_checkout_usa_domicilio_guardado_del_cliente(self):
        self.salir_sesion_staff()
        self.fijar_precio("50.00")
        self.agregar_stock("10")
        self.registrar_cliente(telefono="9980002222")
        cliente = ClienteCRM.query.filter_by(telefono="9980002222").one()
        domicilio = ClienteDomicilio(cliente_id=cliente.id, alias="Casa", direccion="Av. Siempre Viva 742", zona="Cancun")
        db.session.add(domicilio)
        db.session.commit()
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "1"})
        respuesta = self.client.post("/tienda/checkout", data={"domicilio_id": str(domicilio.id), "forma_pago": "Efectivo"})
        self.assertEqual(respuesta.status_code, 302)
        pedido = PedidoCRM.query.one()
        self.assertEqual(pedido.domicilio_id, domicilio.id)

    def test_checkout_bloquea_domicilio_fuera_de_cobertura(self):
        self.salir_sesion_staff()
        self.fijar_precio("70.00")
        self.agregar_stock("5")
        self.registrar_cliente(telefono="9980004444")
        cliente = ClienteCRM.query.filter_by(telefono="9980004444").one()
        domicilio = ClienteDomicilio(cliente_id=cliente.id, alias="Lejos", direccion="Zona foranea 1", zona="Foranea", cobertura=False)
        db.session.add(domicilio)
        db.session.commit()
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "1"})
        respuesta = self.client.post("/tienda/checkout", data={"domicilio_id": str(domicilio.id), "forma_pago": "Efectivo"})
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn(b"fuera de nuestra zona de cobertura", respuesta.data)
        self.assertEqual(PedidoCRM.query.count(), 0)

    def test_checkout_revalida_stock_si_bajo_entre_carrito_y_pago(self):
        self.salir_sesion_staff()
        self.fijar_precio("60.00")
        self.agregar_stock("2")
        self.registrar_cliente(telefono="9980003333")
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "2"})
        # El stock fisico baja (otra venta, ajuste, etc.) despues de armado el carrito.
        self.consumir_stock("1")
        respuesta = self.client.post(
            "/tienda/checkout",
            data={"domicilio_id": "", "direccion": "Calle X 1", "zona": "Cancun", "forma_pago": "Efectivo"},
        )
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn(b"Ya no hay suficiente", respuesta.data)
        self.assertEqual(PedidoCRM.query.count(), 0)

    def test_checkout_sin_direccion_da_error_y_no_crea_pedido(self):
        self.salir_sesion_staff()
        self.fijar_precio("80.00")
        self.agregar_stock("10")
        self.registrar_cliente(telefono="9983334444")
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "1"})
        respuesta = self.client.post("/tienda/checkout", data={"domicilio_id": "", "forma_pago": "Efectivo"})
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(PedidoCRM.query.count(), 0)

    def test_mis_pedidos_solo_lista_pedidos_del_cliente_logueado(self):
        self.salir_sesion_staff()
        self.fijar_precio("90.00")
        self.agregar_stock("10")
        self.registrar_cliente(telefono="9985556666")
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "1"})
        self.client.post("/tienda/checkout", data={"domicilio_id": "", "direccion": "Mi calle 1", "zona": "Cancun", "forma_pago": "Efectivo"})

        self.salir_sesion_staff()
        self.registrar_cliente(telefono="9987778888", nombre="Otro cliente")
        propios = self.client.get("/tienda/mis-pedidos").data
        self.assertNotIn(b"PED-", propios)

    def test_mis_pedidos_y_pedido_confirmado_requieren_cuenta(self):
        self.salir_sesion_staff()
        self.assertEqual(self.client.get("/tienda/mis-pedidos").status_code, 302)
        self.assertEqual(self.client.get("/tienda/pedido/PED-000001").status_code, 302)


if __name__ == "__main__":
    unittest.main()
