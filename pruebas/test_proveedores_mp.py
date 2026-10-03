"""Pruebas de integración con SQLite en memoria; no modifica los datos del ERP."""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

PRUEBAS = Path(__file__).resolve().parent
RAIZ = PRUEBAS.parent  # carpeta del repositorio (codigo de la app)
# Las pruebas se importan entre si (test_crm, test_abastecimiento...).
sys.path.insert(0, str(PRUEBAS))
# Dependencias vendorizadas opcionales, para equipos sin acceso a pip.
sys.path.insert(0, str(RAIZ.parent / '.test-deps'))
# La app se importa como paquete "proveedores" sin importar como se llame la carpeta del repo.
if 'proveedores' not in sys.modules:
    paquete = types.ModuleType('proveedores')
    paquete.__path__ = [str(RAIZ)]
    sys.modules['proveedores'] = paquete
# Siempre antes de importar la app: las pruebas usan SQLite en memoria, nunca la base real.
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
from proveedores.vendssback import app, db, Proveedor, MateriaPrima


class ProveedoresMPTest(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        for n in (1, 2):
            db.session.add(Proveedor(codigo_proveedor=f'P{n}', razon_social=f'Proveedor {n}',
                rfc=f'RFC{n}', tipo_proveedor='Materia prima', estatus='Activo',
                calle_numero='Calle', colonia='Centro', codigo_postal='77500',
                condicion_pago='Contado', tiempo_entrega_normal=1))
        db.session.commit()
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['admin_logueado'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def condicion(self, n, precio=100):
        return dict(id_proveedor=n, principal=n == 1, presentacion_compra='Saco',
            contenido_presentacion=25, unidad_contenido='kg', factor_a_unidad_base=25,
            precio_vigente=precio, iva_tasa=16, compra_minima=1, moneda='MXN',
            tipo_precio='Por presentación', lead_time_dias=2)

    def alta(self, condiciones):
        return self.client.post('/api/materias-primas', json=dict(nombre_oficial='Sal',
            familia='Sales', estado_fisico='Sólido', unidad_base='kg', proveedores=condiciones))

    def test_alta_y_precio_independiente_con_historico(self):
        response = self.alta([self.condicion(1), self.condicion(2, 200)])
        self.assertEqual(response.status_code, 201, response.json)
        url = f"/api/materias-primas/{response.json['id_mp']}/proveedores"
        self.assertEqual(len(self.client.get(url).json), 2)
        response = self.client.post(url, json=self.condicion(1, 150))
        self.assertEqual(response.status_code, 201, response.json)
        rows = self.client.get(url).json
        self.assertEqual(len(rows), 3)
        self.assertEqual([(r['id_proveedor'], r['precio_vigente']) for r in rows if r['activo']], [(1, 150), (2, 200)])
        self.assertEqual([r['precio_vigente'] for r in rows if not r['activo']], [100])
        self.assertEqual(sum(r['principal'] for r in rows if r['activo']), 1)

    def test_alta_invalida_no_deja_registros_parciales(self):
        response = self.alta([self.condicion(1), self.condicion(2, -1)])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(MateriaPrima.query.count(), 0)

    def test_rechaza_duplicados_y_numeros_invalidos(self):
        self.assertEqual(self.alta([self.condicion(1), self.condicion(1)]).status_code, 400)
        self.assertEqual(self.alta([self.condicion(1, 'NaN')]).status_code, 400)

    def test_formulario(self):
        response = self.client.get('/admin/materias-primas/nueva')
        self.assertEqual(response.status_code, 200)
        html = response.data.decode()
        script = html.split('<script>')[-1].split('</script>')[0]
        Path(tempfile.gettempdir(), 'nyds-test-form.js').write_text(script, encoding='utf-8')
        self.assertIn('Editar precio y condiciones', html)


if __name__ == '__main__':
    unittest.main()
