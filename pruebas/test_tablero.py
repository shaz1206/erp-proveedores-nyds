"""Tablero del ERP: KPIs del mes, listas y graficas."""
import unittest
from datetime import datetime

from test_crm import CRMTest
from proveedores.tablero import barras, escala_agradable
from proveedores.vendssback import MovimientoPT, PedidoCRM, db


class EscalaTest(unittest.TestCase):
    def test_escala_cubre_el_maximo_con_marcas_parejas(self):
        for maximo in (0.5, 7, 99, 120, 1234, 56789, 2_300_000):
            tope, marcas = escala_agradable(maximo)
            self.assertGreaterEqual(tope, maximo)
            self.assertEqual(marcas[0], 0)
            self.assertAlmostEqual(marcas[-1], tope, places=4)
            pasos = {round(b - a, 4) for a, b in zip(marcas, marcas[1:])}
            self.assertEqual(len(pasos), 1)

    def test_escala_sin_datos(self):
        self.assertEqual(escala_agradable(0), (1, [0, 1]))

    def test_barras_proporcionales_y_dentro_del_lienzo(self):
        g = barras(["a", "b", "c"], [0, 50, 100], 2)
        altos = [b["alto"] for b in g["barras"]]
        self.assertEqual(altos[0], 0)
        self.assertAlmostEqual(altos[1] * 2, altos[2], delta=0.3)
        for b in g["barras"]:
            self.assertGreaterEqual(b["y"], 0)
            self.assertLessEqual(b["x"] + b["ancho"], g["der"])
            self.assertAlmostEqual(b["y"] + b["alto"], g["base"], delta=0.2)
        self.assertEqual([b["resaltada"] for b in g["barras"]], [False, False, True])
        self.assertEqual(g["barras"][0]["valor"], "")  # sin etiqueta en barras vacias


class TableroTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown
    crear_pedido = CRMTest.crear_pedido

    def test_exige_login(self):
        anonimo = self.client.application.test_client()
        self.assertEqual(anonimo.get("/admin/tablero").status_code, 302)

    def test_vacio_carga(self):
        r = self.client.get("/admin/tablero")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Aún no hay ventas", html)
        self.assertIn("Aún no hay producción registrada", html)
        self.assertIn('href="/admin/tablero"', html)

    def test_con_ventas_y_produccion(self):
        self.assertEqual(self.crear_pedido(token="a", cantidad="2").status_code, 201)
        self.assertEqual(self.crear_pedido(token="b", cantidad="1").status_code, 201)
        db.session.add(MovimientoPT(producto_id=self.pt.id, tipo="Alta por produccion", cantidad=7, unidad="pieza", ubicacion="Almacen PT",
                                    referencia="OP-000001", usuario="t", token="alta-1"))
        db.session.commit()
        html = self.client.get("/admin/tablero").get_data(as_text=True)
        self.assertIn("$360", html)  # 2 x 120 + 1 x 120
        self.assertIn("2 pedido(s)", html)
        self.assertIn("<svg", html)
        self.assertIn("OP-000001", html)
        self.assertIn("Detergente color 5 L", html)
        self.assertIn("PED-000001", html)
        self.assertNotIn("Aún no hay ventas en estos", html)

    def test_cancelados_no_cuentan_como_venta(self):
        self.crear_pedido(token="a", cantidad="1")
        pedido = PedidoCRM.query.one()
        pedido.estado = "Cancelado"
        db.session.commit()
        html = self.client.get("/admin/tablero").get_data(as_text=True)
        self.assertIn("0 pedido(s)", html)
        self.assertNotIn("PED-000001", html)

    def test_selector_de_mes(self):
        self.crear_pedido(token="a", cantidad="1")
        ahora = datetime.utcnow()
        html = self.client.get("/admin/tablero?mes=2020-01").get_data(as_text=True)
        self.assertIn("enero 2020", html)
        self.assertIn("0 pedido(s)", html)
        actual = self.client.get(f"/admin/tablero?mes={ahora.strftime('%Y-%m')}").get_data(as_text=True)
        self.assertIn("1 pedido(s)", actual)
        # mes invalido cae al mes actual sin romper
        self.assertEqual(self.client.get("/admin/tablero?mes=basura").status_code, 200)

    def test_llenado_de_pedido_cuenta_como_produccion(self):
        db.session.add(MovimientoPT(producto_id=self.pt.id, tipo="Alta por llenado", cantidad=3, unidad="pieza", ubicacion="Almacen PT",
                                    referencia="CRM-LLENADO-1", usuario="t", token="alta-2"))
        db.session.commit()
        html = self.client.get("/admin/tablero").get_data(as_text=True)
        self.assertIn("CRM-LLENADO-1", html)
        self.assertIn("Llenado de pedido", html)


if __name__ == "__main__":
    unittest.main()
