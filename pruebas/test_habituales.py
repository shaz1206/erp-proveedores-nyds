"""Productos habituales de un cliente (para armar el pedido con un toque)."""
import unittest

from test_crm import CRMTest
from proveedores.habituales import presentacion_de
from proveedores.vendssback import PedidoCRM, ProductoTerminado, db


class HabitualesTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown

    def pedido(self, token, lineas, telefono="9981000000", estado=None):
        resp = self.client.post("/api/crm/pedidos", json={
            "token": token, "canal": "WhatsApp", "forma_pago": "Contra entrega", "entrega_propia": True,
            "cliente": {"nombre": "Cliente prueba", "telefono": telefono},
            "domicilio": {"direccion": "Av. Talleres 123", "zona": "Cancun"}, "lineas": lineas})
        self.assertEqual(resp.status_code, 201, resp.get_json())
        pedido = db.session.get(PedidoCRM, resp.get_json()["id"])
        if estado:
            pedido.estado = estado
            db.session.commit()
        return pedido

    def linea(self, producto=None, cantidad="2", precio="120.00", presentacion=None):
        d = {"producto_id": (producto or self.pt).id, "cantidad": cantidad, "precio_unitario": precio}
        if presentacion:
            d["notas"] = f"Presentación: {presentacion}"
        return d

    def test_presentacion_en_notas(self):
        self.assertEqual(presentacion_de("Presentación: 5 L"), "5 L")
        self.assertEqual(presentacion_de("presentacion: 20 L "), "20 L")
        self.assertEqual(presentacion_de("otra nota"), "")
        self.assertEqual(presentacion_de(None), "")

    def test_exige_sesion(self):
        anonimo = self.client.application.test_client()
        self.assertEqual(anonimo.get("/api/crm/clientes/1/habituales").status_code, 403)

    def test_cliente_sin_historial(self):
        data = self.client.get("/api/crm/clientes/999/habituales").get_json()
        self.assertEqual(data, {"pedidos": 0, "productos": [], "ultimo_pedido": None})

    def test_agrupa_por_producto_y_presentacion_y_ordena_por_frecuencia(self):
        otro = ProductoTerminado(sku="JAB-1", nombre="Jabon liquido", categoria="Jabones", estatus="Activo")
        db.session.add(otro)
        db.session.commit()
        p1 = self.pedido("a", [self.linea(cantidad="2", presentacion="5 L"), self.linea(otro, cantidad="1", precio="45.00")])
        self.pedido("b", [self.linea(cantidad="3", precio="130.00", presentacion="5 L")])
        self.pedido("c", [self.linea(cantidad="1", precio="40.00", presentacion="1 L")])
        cid = p1.cliente_id
        data = self.client.get(f"/api/crm/clientes/{cid}/habituales").get_json()
        self.assertEqual(data["pedidos"], 3)
        claves = [(h["nombre"], h["presentacion"], h["veces"]) for h in data["productos"]]
        self.assertEqual(claves[0], ("Detergente color 5 L", "5 L", 2))
        self.assertEqual(sorted(claves[1:]), sorted([("Detergente color 5 L", "1 L", 1), ("Jabon liquido", "", 1)]))
        cinco = data["productos"][0]
        self.assertEqual(cinco["cantidad_total"], 5.0)
        # "ultima" corresponde al pedido mas reciente que lo incluye (b)
        self.assertEqual((cinco["ultima_cantidad"], cinco["ultimo_precio"]), (3.0, 130.0))
        # el ultimo pedido completo es "c"
        self.assertEqual([(l["presentacion"], l["cantidad"]) for l in data["ultimo_pedido"]["lineas"]], [("1 L", 1.0)])

    def test_ignora_cancelados_y_devueltos_y_otros_clientes(self):
        p1 = self.pedido("a", [self.linea()])
        self.pedido("b", [self.linea(cantidad="9")], estado="Cancelado")
        self.pedido("c", [self.linea(cantidad="8")], estado="Devuelto")
        self.pedido("d", [self.linea(cantidad="7")], telefono="9982222222")
        data = self.client.get(f"/api/crm/clientes/{p1.cliente_id}/habituales").get_json()
        self.assertEqual(data["pedidos"], 1)
        self.assertEqual(data["productos"][0]["veces"], 1)
        self.assertEqual(data["productos"][0]["cantidad_total"], 2.0)
        self.assertEqual(data["ultimo_pedido"]["folio"], p1.folio)

    def test_misma_linea_repetida_en_un_pedido_cuenta_una_vez(self):
        p = self.pedido("a", [self.linea(cantidad="1"), self.linea(cantidad="2")])
        h = self.client.get(f"/api/crm/clientes/{p.cliente_id}/habituales").get_json()["productos"][0]
        self.assertEqual((h["veces"], h["cantidad_total"]), (1, 3.0))

    def test_maximo_veinticuatro_productos(self):
        extras = []
        for i in range(30):
            extra = ProductoTerminado(sku=f"X-{i}", nombre=f"Producto {i}", categoria="Varios", estatus="Activo")
            db.session.add(extra)
            extras.append(extra)
        db.session.commit()
        p = self.pedido("a", [self.linea(e) for e in extras])
        self.assertEqual(len(self.client.get(f"/api/crm/clientes/{p.cliente_id}/habituales").get_json()["productos"]), 24)

    def test_pantalla_incluye_panel(self):
        html = self.client.get("/crm/pedidos").data
        for marca in (b"mostrarProductos", b"Mostrar productos", b"nuevoClientePedido", b"dlgClienteNuevo", b"dlgProducto", b"dlgCarrito", b"abrirCarrito", b"Finalizar venta", b"repetirUltimo", b"/api/crm/clientes/"):
            self.assertIn(marca, html)


if __name__ == "__main__":
    unittest.main()
