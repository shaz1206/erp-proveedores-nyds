"""Pruebas de saldos y ajustes con base en memoria."""
import unittest
from test_abastecimiento import ComprasTest
from proveedores.vendssback import db,AjusteInventario,MateriaPrima,MovimientoMP,OrdenCompra

class InventarioTest(unittest.TestCase):
    setUp=ComprasTest.setUp
    tearDown=ComprasTest.tearDown
    emitir=ComprasTest.emitir
    recibir=ComprasTest.recibir
    def mover(self,**kw):
        self.c.get('/admin/inventario-mp/registrar')
        with self.c.session_transaction() as s:csrf=s['inventario_csrf']
        data=dict(csrf=csrf,token='inicio',mp_id=str(self.mid),unidad='kg',tipo='Saldo inicial',cantidad='10',ubicacion='Central / A1',lote='',ambiente='real',referencia='Conteo 001',motivo='Conteo inicial físico',confirmado='si')
        data.update(kw)
        return self.c.post('/admin/inventario-mp/registrar',data=data)
    def test_inicial_ajuste_idempotencia_y_salida_excesiva(self):
        self.assertEqual(self.mover().status_code,302)
        self.assertEqual(self.mover().status_code,302)
        self.assertEqual(AjusteInventario.query.count(),1)
        self.assertEqual(self.mover(token='otro').status_code,400)
        self.assertEqual(self.mover(token='salida',tipo='Ajuste de salida',cantidad='3').status_code,302)
        self.assertEqual(sum(a.cantidad for a in AjusteInventario.query.all()),7)
        self.assertEqual(self.mover(token='exceso',tipo='Ajuste de salida',cantidad='8').status_code,400)
        self.assertEqual(AjusteInventario.query.count(),2)
    def test_ambientes_y_ubicaciones_no_comparten_saldo(self):
        self.mover(ambiente='demo',referencia='SOLO-DEMO')
        self.assertEqual(self.mover(tipo='Ajuste de salida',token='fuera',cantidad='1').status_code,400)
        self.assertEqual(self.mover(tipo='Ajuste de salida',token='otra',ambiente='demo',ubicacion='Otra',cantidad='1').status_code,400)
        self.assertNotIn(b'SOLO-DEMO',self.c.get('/admin/inventario-mp').data)
        self.assertIn(b'SOLO-DEMO',self.c.get('/admin/inventario-mp?ambiente=demo').data)
    def test_recepcion_y_ajuste_comparten_saldo(self):
        self.emitir();self.recibir(aceptada='3',rechazada='0')
        self.assertEqual(MovimientoMP.query.one().cantidad,75)
        self.assertEqual(self.mover(token='inicial').status_code,400)
        self.assertEqual(self.mover(token='salida',tipo='Ajuste de salida',cantidad='70').status_code,302)
        self.assertEqual(self.mover(token='mas',tipo='Ajuste de salida',cantidad='6').status_code,400)
    def test_demo_recepcion_no_aparece_en_real(self):
        self.emitir();o=OrdenCompra.query.one();o.proveedor_nombre='DEMO - Proveedor ficticio';db.session.commit()
        self.recibir(referencia='REC-DEMO')
        self.assertNotIn(b'OC-00001 / REC-1',self.c.get('/admin/inventario-mp').data)
        self.assertIn(b'OC-00001 / REC-1',self.c.get('/admin/inventario-mp?ambiente=demo').data)
    def test_validaciones(self):
        for kw in [dict(cantidad='NaN'),dict(cantidad='-2'),dict(unidad='L'),dict(motivo=''),dict(confirmado=''),dict(csrf='bad'),dict(ambiente='otro')]:
            self.assertEqual(self.mover(**kw).status_code,400,kw)
        self.assertEqual(AjusteInventario.query.count(),0)
        m=db.session.get(MateriaPrima,self.mid);m.activo=False;db.session.commit()
        self.assertEqual(self.mover().status_code,400)
        self.assertEqual(self.c.get('/admin/inventario-mp/registrar').status_code,200)
        from test_abastecimiento import app
        self.assertEqual(app.test_client().get('/admin/inventario-mp/registrar').status_code,302)

if __name__=='__main__':unittest.main()
