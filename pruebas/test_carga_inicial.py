"""La carga inicial solo agrega, no duplica y no toca lo que ya existe."""
import json
import unittest

from test_crm import CRMTest
from proveedores.cargar_datos_iniciales import RUTA_JSON, cargar
from proveedores.vendssback import ClienteCRM, MateriaPrima, PedidoCRM, ProductoTerminado, db


def cargar_y_cerrar(datos, aplicar):
    db.session.commit()          # sin transacciones abiertas en la sesion de la prueba
    return cargar(db.engine, datos, aplicar=aplicar)


class CargaInicialTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown

    def datos(self):
        return json.loads(RUTA_JSON.read_text(encoding="utf-8"))

    def test_en_seco_no_guarda_nada(self):
        antes = MateriaPrima.query.count()
        resumen, _ = cargar_y_cerrar(self.datos(), False)
        db.session.remove()
        self.assertGreater(resumen["materias_primas"][0], 0)
        self.assertEqual(MateriaPrima.query.count(), antes)

    def test_aplicar_agrega_sin_tocar_lo_existente_y_no_duplica(self):
        mp_antes = MateriaPrima.query.count()
        previos = {p.sku: (p.nombre, p.estatus) for p in ProductoTerminado.query.all()}
        clientes, pedidos = ClienteCRM.query.count(), PedidoCRM.query.count()
        resumen, _ = cargar_y_cerrar(self.datos(), True)
        db.session.remove()
        self.assertEqual(MateriaPrima.query.count(), mp_antes + resumen["materias_primas"][0])
        self.assertGreaterEqual(ProductoTerminado.query.count(), len(previos) + resumen["productos_terminados"][0])
        for sku, valores in previos.items():                       # lo que ya estaba no cambia
            p = ProductoTerminado.query.filter_by(sku=sku).one()
            self.assertEqual((p.nombre, p.estatus), valores)
        self.assertEqual((ClienteCRM.query.count(), PedidoCRM.query.count()), (clientes, pedidos))
        total = MateriaPrima.query.count()
        otra, _ = cargar_y_cerrar(self.datos(), True)    # segunda corrida: nada nuevo
        db.session.remove()
        self.assertEqual(sum(n for n, _, _ in otra.values()), 0)
        self.assertEqual(MateriaPrima.query.count(), total)

    def test_no_copia_datos_demo(self):
        cargar_y_cerrar(self.datos(), True)
        db.session.remove()
        self.assertFalse(any(p.sku.upper().startswith("DEMO") and p.sku != "DEMO-CRM-ERP"
                             for p in ProductoTerminado.query.all()))


if __name__ == "__main__":
    unittest.main()
