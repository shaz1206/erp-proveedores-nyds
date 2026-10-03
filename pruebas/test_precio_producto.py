"""Precio de lista del producto terminado: lo usa la tienda para mostrar
y cobrar automaticamente, asi que debe guardarse correcto o quedar vacio
(nunca un valor invalido ni negativo)."""
import unittest

from test_productos_terminados import ProductoTerminadoTest
from proveedores.vendssback import ProductoTerminado


class PrecioProductoTest(unittest.TestCase):
    setUp = ProductoTerminadoTest.setUp
    tearDown = ProductoTerminadoTest.tearDown
    datos = ProductoTerminadoTest.datos

    def test_guarda_precio_valido(self):
        datos = self.datos()
        datos['precio'] = '129.90'
        respuesta = self.client.post('/admin/productos-terminados/nuevo', data=datos)
        self.assertEqual(respuesta.status_code, 302)
        p = ProductoTerminado.query.one()
        self.assertEqual(float(p.precio), 129.90)

    def test_precio_en_blanco_queda_sin_definir(self):
        datos = self.datos()
        respuesta = self.client.post('/admin/productos-terminados/nuevo', data=datos)
        self.assertEqual(respuesta.status_code, 302)
        p = ProductoTerminado.query.one()
        self.assertIsNone(p.precio)

    def test_rechaza_precio_negativo_o_cero(self):
        datos = self.datos()
        datos['precio'] = '0'
        respuesta = self.client.post('/admin/productos-terminados/nuevo', data=datos)
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(ProductoTerminado.query.count(), 0)

    def test_rechaza_mas_de_dos_decimales(self):
        datos = self.datos()
        datos['precio'] = '10.999'
        respuesta = self.client.post('/admin/productos-terminados/nuevo', data=datos)
        self.assertEqual(respuesta.status_code, 400)


if __name__ == '__main__':
    unittest.main()
