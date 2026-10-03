import io
import unittest
from PIL import Image
from test_proveedores_mp import app, db
from proveedores.vendssback import ProductoTerminado


class ProductoTerminadoTest(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['admin_logueado'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def datos(self):
        return dict(sku='a-001', nombre='Limpiador', categoria='Limpieza', estatus='Activo',
            stock_minimo='10.5', color='Azul', olor='Lavanda', viscosidad='500 cP', ppm='100',
            biodegradabilidad='Sí', forma_uso='Dilución', puntos_venta=['Ruta', 'Arpelab'])

    def test_alta_edicion_catalogo_imagen(self):
        datos = self.datos()
        imagen = io.BytesIO()
        Image.new('RGB', (10, 10), 'blue').save(imagen, format='PNG')
        imagen.seek(0)
        datos['imagen'] = (imagen, 'producto.png')
        respuesta = self.client.post('/admin/productos-terminados/nuevo', data=datos)
        self.assertEqual(respuesta.status_code, 302)
        p = ProductoTerminado.query.one()
        self.assertEqual(p.sku, 'A-001')
        self.assertEqual(p.puntos_venta, ['Ruta', 'Arpelab'])
        self.assertEqual(self.client.get(f'/admin/productos-terminados/{p.id}/imagen').mimetype, 'image/png')
        self.assertEqual(self.client.get(f'/admin/productos-terminados/{p.id}').status_code, 200)
        datos = self.datos()
        datos.update(nombre='Limpiador nuevo', estatus='Inactivo', puntos_venta=['Vending'])
        self.assertEqual(self.client.post(f'/admin/productos-terminados/{p.id}', data=datos).status_code, 302)
        self.assertTrue(p.imagen)
        self.assertEqual(p.puntos_venta, ['Vending'])
        self.assertIn(b'Limpiador nuevo', self.client.get('/admin/productos-terminados?q=Limpiador&estatus=Inactivo').data)
        datos['quitar_imagen'] = '1'
        self.client.post(f'/admin/productos-terminados/{p.id}', data=datos)
        self.assertIsNone(p.imagen)

    def test_validaciones_y_autorizacion(self):
        datos = self.datos()
        datos['stock_minimo'] = '-1'
        self.assertEqual(self.client.post('/admin/productos-terminados/nuevo', data=datos).status_code, 400)
        datos = self.datos()
        datos['imagen'] = (io.BytesIO(b'no es imagen'), 'foto.png')
        self.assertEqual(self.client.post('/admin/productos-terminados/nuevo', data=datos).status_code, 400)
        self.assertEqual(ProductoTerminado.query.count(), 0)
        self.client.post('/admin/productos-terminados/nuevo', data=self.datos())
        self.assertEqual(self.client.post('/admin/productos-terminados/nuevo', data=self.datos()).status_code, 400)
        self.assertEqual(ProductoTerminado.query.count(), 1)
        with self.client.session_transaction() as sesion:
            sesion.clear()
        for url in ['/admin/productos-terminados', '/admin/productos-terminados/nuevo', '/admin/productos-terminados/1/imagen']:
            self.assertEqual(self.client.get(url).status_code, 302)


if __name__ == '__main__':
    unittest.main()
