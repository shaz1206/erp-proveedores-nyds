"""Integración de compras con base SQLite en memoria, sin tocar datos reales."""
import unittest
from datetime import date, timedelta
from decimal import Decimal
from test_proveedores_mp import app, db
from proveedores.vendssback import (Proveedor, MateriaPrima, MateriaPrimaProveedor,
    MateriaPrimaInventarioConfig, OrdenCompra, CompraLinea, RecepcionMP, MovimientoMP)

class ComprasTest(unittest.TestCase):
    def setUp(self):
        self.ctx=app.app_context(); self.ctx.push(); db.drop_all(); db.create_all()
        p=Proveedor(codigo_proveedor='P1',razon_social='Proveedor prueba',rfc='RFC1',tipo_proveedor='Materia prima',estatus='Activo',calle_numero='Calle',colonia='Centro',codigo_postal='77500',condicion_pago='Contado',tiempo_entrega_normal=1)
        m=MateriaPrima(sku_mp='MP1',nombre_oficial='Sal',nombre_normalizado='sal',familia='Sales',estado_fisico='Sólido',unidad_base='kg',activo=True,borrador=False)
        db.session.add_all([p,m]); db.session.flush()
        c=MateriaPrimaProveedor(id_mp=m.id_mp,id_proveedor=p.id_proveedor,presentacion_compra='Saco',contenido_presentacion=25,unidad_contenido='kg',factor_a_unidad_base=25,precio_vigente=100,tipo_precio='Por presentación',moneda='MXN',iva_tasa=16,compra_minima=1,unidad_compra_minima='Presentación',vigencia_desde=date.today(),activo=True)
        db.session.add(c);db.session.commit();self.cid=c.id_relacion;self.mid=m.id_mp
        self.c=app.test_client()
        with self.c.session_transaction() as s:s['admin_logueado']=True
        self.c.get('/admin/compras')
        with self.c.session_transaction() as s:self.csrf=s['compras_csrf']
    def tearDown(self):
        db.session.remove();self.ctx.pop()
    def emitir(self,**kw):
        data=dict(csrf=self.csrf,token='orden1',entrega=str(date.today()),condicion=str(self.cid),cantidad='10')
        data.update(kw);return self.c.post('/admin/compras/nueva',data=data)
    def recibir(self,**kw):
        l=CompraLinea.query.first()
        data=dict(csrf=self.csrf,token='rec1',linea=str(l.id),aceptada='3',rechazada='1',ubicacion='Central / A1',referencia='Remisión 01',observaciones='Saco roto',conforme='si')
        data.update(kw);return self.c.post(f'/admin/compras/{l.orden_id}',data=data)
    def test_parcial_rechazo_conversion_cierre_y_no_duplicados(self):
        self.assertEqual(self.emitir().status_code,302)
        self.emitir();self.assertEqual(OrdenCompra.query.count(),1)
        self.assertEqual(self.recibir().status_code,302)
        self.assertEqual(MovimientoMP.query.one().cantidad,Decimal(75))
        self.assertEqual(CompraLinea.query.one().pendiente,Decimal(6))
        self.assertEqual(OrdenCompra.query.one().estado,'Parcial')
        self.recibir();self.assertEqual(RecepcionMP.query.count(),1)
        self.recibir(token='rec2',aceptada='6',rechazada='0')
        self.assertEqual(OrdenCompra.query.one().estado,'Cerrada con rechazo')
        self.assertEqual(sum(m.cantidad for m in MovimientoMP.query.all()),225)
        self.recibir(token='rec3',aceptada='1',rechazada='0')
        self.assertEqual(RecepcionMP.query.count(),2)
        for url in ['/admin/compras','/admin/compras/nueva','/admin/compras/1','/admin/inventario-mp']:
            self.assertEqual(self.c.get(url).status_code,200,url)
    def test_sobrerecepcion_y_valores_invalidos_atomicos(self):
        self.emitir()
        for kw in [dict(aceptada='11'),dict(aceptada='-1'),dict(aceptada='NaN'),dict(aceptada='0',rechazada='0'),dict(observaciones=''),dict(conforme='')]:
            self.recibir(**kw)
            self.assertEqual(RecepcionMP.query.count(),0,kw)
            self.assertEqual(MovimientoMP.query.count(),0,kw)
        self.assertEqual(OrdenCompra.query.one().estado,'Emitida')
    def test_lote_caducidad_y_coa(self):
        m=db.session.get(MateriaPrima,self.mid);m.requiere_coa=True
        db.session.add(MateriaPrimaInventarioConfig(id_mp=m.id_mp,control_lote=True,control_caducidad=True));db.session.commit()
        self.emitir()
        for kw in [{},dict(lote='L1'),dict(lote='L1',caducidad=str(date.today()),coa='C1')]:
            self.recibir(**kw);self.assertEqual(RecepcionMP.query.count(),0)
        self.recibir(lote='L1',caducidad=str(date.today()+timedelta(days=30)),coa='Certificado C1')
        self.assertEqual(RecepcionMP.query.count(),1)
    def test_precios_congelados_y_por_unidad(self):
        c=db.session.get(MateriaPrimaProveedor,self.cid);c.tipo_precio='Por kg';c.precio_vigente=2;db.session.commit()
        self.emitir();self.assertEqual(CompraLinea.query.one().precio,50)
        c.precio_vigente=20;c.factor_a_unidad_base=100;db.session.commit()
        self.assertEqual(OrdenCompra.query.one().total,580)
        self.recibir();self.assertEqual(MovimientoMP.query.one().cantidad,75)
    def test_inactivos_minimo_y_cancelacion(self):
        c=db.session.get(MateriaPrimaProveedor,self.cid);c.compra_minima=20;db.session.commit()
        self.emitir();self.assertEqual(OrdenCompra.query.count(),0)
        c.compra_minima=1;c.activo=False;db.session.commit()
        self.emitir();self.assertEqual(OrdenCompra.query.count(),0)
        c.activo=True;db.session.commit();self.emitir()
        self.c.post('/admin/compras/1',data=dict(csrf=self.csrf,token='cancel1',accion='cancelar'))
        self.assertEqual(OrdenCompra.query.one().estado,'Cancelada')
        self.recibir();self.assertEqual(MovimientoMP.query.count(),0)
    def test_recepcion_completa_sin_rechazo(self):
        self.emitir();self.recibir(aceptada='10',rechazada='0')
        self.assertEqual(OrdenCompra.query.one().estado,'Recibida')
        self.assertEqual(MovimientoMP.query.one().cantidad,250)

    def test_varias_partidas_y_moneda_consistente(self):
        original=db.session.get(MateriaPrimaProveedor,self.cid)
        c=MateriaPrimaProveedor(id_mp=self.mid,id_proveedor=original.id_proveedor,presentacion_compra='Bolsa',contenido_presentacion=1,unidad_contenido='kg',factor_a_unidad_base=1,precio_vigente=5,tipo_precio='Por presentación',moneda='USD',iva_tasa=16,compra_minima=1,unidad_compra_minima='Presentación',vigencia_desde=date.today(),activo=True)
        db.session.add(c);db.session.commit()
        self.assertEqual(self.emitir(condicion=[str(self.cid),str(c.id_relacion)],cantidad=['10','2']).status_code,400)
        self.assertEqual(OrdenCompra.query.count(),0)
        c.moneda='MXN';db.session.commit()
        self.assertEqual(self.emitir(condicion=[str(self.cid),str(c.id_relacion)],cantidad=['10','2']).status_code,302)
        self.assertEqual(CompraLinea.query.count(),2)
        self.recibir(aceptada='10',rechazada='0')
        self.assertEqual(OrdenCompra.query.one().estado,'Parcial')
        segunda=CompraLinea.query.order_by(CompraLinea.id.desc()).first()
        self.recibir(token='rec2',linea=str(segunda.id),aceptada='2',rechazada='0')
        self.assertEqual(OrdenCompra.query.one().estado,'Recibida')
        self.assertEqual(sum(m.cantidad for m in MovimientoMP.query.all()),252)

    def test_seguridad(self):
        anon=app.test_client()
        self.assertEqual(anon.get('/admin/inventario-mp').status_code,302)
        self.assertEqual(self.emitir(csrf='incorrecto').status_code,400)
        self.assertEqual(OrdenCompra.query.count(),0)
    def test_rechazo_total_no_genera_stock(self):
        self.emitir();self.recibir(aceptada='0',rechazada='10',conforme='')
        self.assertEqual(OrdenCompra.query.one().estado,'Cerrada con rechazo')
        self.assertEqual(MovimientoMP.query.count(),0)
        self.assertEqual(RecepcionMP.query.one().rechazada,10)

if __name__=='__main__':unittest.main()
