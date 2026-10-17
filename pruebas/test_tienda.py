"""Tienda en linea: cuenta de cliente (no hay compra como invitado), catalogo
publico y carrito en sesion. El flujo real de negocio es: el carrito se
arma aqui pero quien recibe, atiende, confirma y cierra el pedido es ventas
por WhatsApp -- "Enviar pedido" NO crea un PedidoCRM solo, arma un resumen
y un enlace wa.me; ventas lo captura despues a mano en el CRM (como ya
probaba test_crm.py), y ahi es donde aparece en "Mis pedidos" del cliente.

Reglas de negocio reforzadas que siguen vigentes:
- El precio del resumen SIEMPRE sale del catalogo (ProductoTerminado.precio)
  al momento de armar el mensaje, nunca de algo cacheado en el carrito.
- No se deja pedir mas de lo que hay fisicamente disponible en almacen (el
  carrito se ajusta solo).
- Un domicilio fuera de cobertura ya no bloquea el envio (eso lo decide
  ventas en la conversacion), pero se avisa con una nota en el mensaje."""
import secrets
import unittest
from decimal import Decimal

from test_crm import CRMTest
from proveedores.tienda import UBICACION_PT
from proveedores.vendssback import ClienteCRM, ClienteDomicilio, MovimientoPT, PedidoCRM, db


class TiendaTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown
    crear_pedido = CRMTest.crear_pedido

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

    def salir_sesion_staff(self):
        with self.client.session_transaction() as sesion:
            sesion.clear()

    def configurar_whatsapp(self, numero="529981112233"):
        """Debe llamarse mientras la sesion de staff (admin_logueado) sigue
        activa, tal como la deja CRMTest.setUp por defecto."""
        respuesta = self.client.post("/admin/tienda/configuracion", data={"whatsapp_ventas": numero})
        self.assertEqual(respuesta.status_code, 302)

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

    # ---- Carrito ----

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

    # ---- Enviar pedido por WhatsApp ----

    def test_checkout_exige_cuenta_de_cliente(self):
        self.salir_sesion_staff()
        self.fijar_precio("120.00")
        self.agregar_stock("10")
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "1"})
        respuesta = self.client.get("/tienda/checkout")
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/tienda/login", respuesta.headers["Location"])

    def test_checkout_sin_direccion_da_error(self):
        self.salir_sesion_staff()
        self.fijar_precio("80.00")
        self.agregar_stock("10")
        self.registrar_cliente(telefono="9983334444")
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "1"})
        respuesta = self.client.post("/tienda/checkout", data={"domicilio_id": "", "forma_pago": "Efectivo"})
        self.assertEqual(respuesta.status_code, 400)

    def test_checkout_sin_numero_configurado_muestra_mensaje_para_copiar(self):
        self.salir_sesion_staff()
        self.fijar_precio("120.00")
        self.agregar_stock("10")
        self.registrar_cliente(telefono="9987776655")
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "2"})
        respuesta = self.client.post(
            "/tienda/checkout",
            data={"domicilio_id": "", "direccion": "Calle Falsa 123", "referencias": "Casa azul", "zona": "Cancun", "forma_pago": "Transferencia"},
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotIn(b"wa.me", respuesta.data)
        self.assertIn(b"240.00", respuesta.data)
        self.assertIn(b"Calle Falsa 123", respuesta.data)
        # No se crea ningun pedido solo: eso lo hace ventas despues en el CRM.
        self.assertEqual(PedidoCRM.query.count(), 0)
        # El carrito ya se vacio porque el pedido "se mando".
        self.assertIn(b"carrito est\xc3\xa1 vac\xc3\xado", self.client.get("/tienda/carrito").data)

    def test_checkout_con_numero_configurado_genera_enlace_wa_me(self):
        self.configurar_whatsapp("529981112233")
        self.salir_sesion_staff()
        self.fijar_precio("120.00")
        self.agregar_stock("10")
        self.registrar_cliente(telefono="9981112233")
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "1"})
        respuesta = self.client.post(
            "/tienda/checkout",
            data={"domicilio_id": "", "direccion": "Otra calle 55", "zona": "Cancun", "forma_pago": "Efectivo"},
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b"https://wa.me/529981112233?text=", respuesta.data)

    def test_checkout_usa_precio_vigente_en_el_mensaje(self):
        self.salir_sesion_staff()
        self.fijar_precio("100.00")
        self.agregar_stock("10")
        self.registrar_cliente(telefono="9981112299")
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "1"})
        # Ventas cambia el precio de lista despues de que el cliente agrego al carrito.
        self.fijar_precio("250.00")
        respuesta = self.client.post(
            "/tienda/checkout",
            data={"domicilio_id": "", "direccion": "Otra calle 77", "zona": "Cancun", "forma_pago": "Efectivo"},
        )
        self.assertIn(b"250.00", respuesta.data)

    def test_checkout_guarda_direccion_nueva_y_la_reutiliza_despues(self):
        self.salir_sesion_staff()
        self.fijar_precio("50.00")
        self.agregar_stock("10")
        self.registrar_cliente(telefono="9980002222")
        cliente = ClienteCRM.query.filter_by(telefono="9980002222").one()
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "1"})
        self.client.post("/tienda/checkout", data={"domicilio_id": "", "direccion": "Av. Siempre Viva 742", "zona": "Cancun", "forma_pago": "Efectivo"})
        self.assertEqual(ClienteDomicilio.query.filter_by(cliente_id=cliente.id).count(), 1)

        domicilio = ClienteDomicilio.query.filter_by(cliente_id=cliente.id).one()
        self.client.post(f"/tienda/catalogo/agregar/{self.pt.id}", data={"cantidad": "1"})
        self.client.post("/tienda/checkout", data={"domicilio_id": str(domicilio.id), "forma_pago": "Efectivo"})
        # Reusar la misma direccion no crea una segunda fila.
        self.assertEqual(ClienteDomicilio.query.filter_by(cliente_id=cliente.id).count(), 1)

    def test_checkout_domicilio_fuera_de_cobertura_no_bloquea_pero_avisa(self):
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
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("fuera de nuestra zona de cobertura".encode("utf-8"), respuesta.data)

    # ---- Seguimiento: lo que ventas captura despues en el CRM ----

    def test_mis_pedidos_solo_lista_pedidos_del_cliente_logueado(self):
        # Ventas captura el pedido en el CRM (como en test_crm.py) despues de
        # cerrarlo por WhatsApp; self.crear_pedido usa el telefono 9981000000.
        self.crear_pedido()

        self.salir_sesion_staff()
        self.registrar_cliente(telefono="9981000000", nombre="Cliente del pedido")
        propios = self.client.get("/tienda/mis-pedidos").data
        self.assertIn(b"PED-", propios)

        self.salir_sesion_staff()
        self.registrar_cliente(telefono="9987778888", nombre="Otro cliente")
        ajenos = self.client.get("/tienda/mis-pedidos").data
        self.assertNotIn(b"PED-", ajenos)

    def test_mis_pedidos_y_pedido_confirmado_requieren_cuenta(self):
        self.salir_sesion_staff()
        self.assertEqual(self.client.get("/tienda/mis-pedidos").status_code, 302)
        self.assertEqual(self.client.get("/tienda/pedido/PED-000001").status_code, 302)

    # ---- Configuracion del numero de WhatsApp (solo staff) ----

    def test_configuracion_requiere_sesion_de_staff(self):
        self.salir_sesion_staff()
        self.assertEqual(self.client.get("/admin/tienda/configuracion").status_code, 302)
        self.assertEqual(self.client.post("/admin/tienda/configuracion", data={"whatsapp_ventas": "529981112233"}).status_code, 302)

    def test_configuracion_guarda_numero_valido(self):
        respuesta = self.client.post("/admin/tienda/configuracion", data={"whatsapp_ventas": "52 998 111 2233"})
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("guardado=1", respuesta.headers["Location"])
        pagina = self.client.get("/admin/tienda/configuracion").data
        self.assertIn(b"529981112233", pagina)

    def test_configuracion_rechaza_numero_demasiado_corto(self):
        respuesta = self.client.post("/admin/tienda/configuracion", data={"whatsapp_ventas": "123"})
        self.assertEqual(respuesta.status_code, 400)


if __name__ == "__main__":
    unittest.main()
