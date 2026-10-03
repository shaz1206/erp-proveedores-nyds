"""Carga de recetas y edición con SQLite en memoria."""
import json, unittest
from pathlib import Path
from decimal import Decimal
from test_proveedores_mp import app,db
from proveedores.vendssback import Receta,RecetaIngrediente,RecetaRevision,MateriaPrima,ProductoTerminado,MovimientoMP,Proveedor,MateriaPrimaProveedor,sincronizar_proveedores_desde_recetario
from proveedores.importar_recetario import cargar

DATOS=json.loads((Path(__file__).resolve().parents[1]/'data'/'recetario'/'Recetario_NYDS.json').read_text(encoding='utf-8'))
class RecetarioTest(unittest.TestCase):
    def setUp(self):
        self.ctx=app.app_context();self.ctx.push();db.drop_all();db.create_all()
        self.result=cargar(DATOS)
        self.c=app.test_client()
        with self.c.session_transaction() as s:s['admin_logueado']=True
    def tearDown(self):db.session.remove();self.ctx.pop()
    def editar(self,r,**kwargs):
        self.c.get(f'/admin/recetas/{r.id}')
        with self.c.session_transaction() as s:csrf=s['recetas_csrf']
        data=dict(csrf=csrf,version=str(r.version),rendimiento='1000',unidad_rendimiento='mL',motivo='Confirmación de prueba',notas='Nota nueva',pendientes='Revisar')
        for l in r.lineas:
            data.update({f'cantidad_{l.id}':str(l.cantidad),f'unidad_{l.id}':l.unidad,f'vinculo_{l.id}':f'mp:{l.mp_id}' if l.mp_id else f'rec:{l.subreceta_id}',f'proveedor_{l.id}':l.proveedor_referencia or ''})
        data.update(kwargs)
        return self.c.post(f'/admin/recetas/{r.id}',data=data)
    def test_conteos_y_datos_literales(self):
        self.assertEqual(Receta.query.count(),41);self.assertEqual(RecetaIngrediente.query.count(),190)
        self.assertEqual(ProductoTerminado.query.count(),33)
        self.assertEqual(Receta.query.filter_by(tipo='Semiterminado').count(),8)
        self.assertEqual(Receta.query.filter(Receta.rendimiento.isnot(None)).count(),11)
        self.assertTrue(all(m.borrador and not m.activo for m in MateriaPrima.query.all()))
        self.assertTrue(all(p.estatus=='Borrador' for p in ProductoTerminado.query.all()))
        self.assertEqual(MovimientoMP.query.count(),0);self.assertEqual(Proveedor.query.count(),0)
        for r in Receta.query.all():
            self.assertEqual(len(r.lineas),len(r.origen['receta']['componentes']))
            for l,c in zip(r.lineas,r.origen['receta']['componentes']):
                self.assertEqual(l.cantidad,Decimal(c['cantidad']));self.assertEqual(l.unidad,c['unidad'])
                self.assertEqual(l.proveedor_referencia,c['proveedor'])
    def test_sincroniza_proveedores_referenciados_sin_duplicar(self):
        resultado=sincronizar_proveedores_desde_recetario(RecetaIngrediente)
        self.assertGreater(resultado['proveedores_creados'],0)
        self.assertGreater(resultado['relaciones_creadas'],0)
        nombres={p.razon_social.upper() for p in Proveedor.query.all()}
        self.assertIn('LA FÓRMULA',nombres)
        self.assertIn('POCHTECA',nombres)
        self.assertIn('FRESNO',nombres)
        self.assertNotIn('PROPIO',nombres)
        self.assertTrue(MateriaPrimaProveedor.query.filter_by(activo=True).count()>0)
        primera_cuenta=(Proveedor.query.count(),MateriaPrimaProveedor.query.count())
        self.assertEqual(sincronizar_proveedores_desde_recetario(RecetaIngrediente),{'proveedores_creados':0,'relaciones_creadas':0})
        self.assertEqual((Proveedor.query.count(),MateriaPrimaProveedor.query.count()),primera_cuenta)
    def test_subrecetas_sin_duplicacion_como_insumos(self):
        dtk=Receta.query.filter(Receta.nombre.like('DTK%')).one()
        self.assertEqual(RecetaIngrediente.query.filter_by(nombre_original='DTK').one().subreceta_id,dtk.id)
        for l in RecetaIngrediente.query.filter(RecetaIngrediente.proveedor_referencia.like('PROPIO%')).all():
            if l.nombre_original!='Agua':self.assertIsNotNone(l.subreceta_id,l.nombre_original)
    def test_reimportacion_no_duplica_ni_sobrescribe(self):
        r=Receta.query.first();self.assertEqual(self.editar(r).status_code,302)
        cuenta=MateriaPrima.query.count()
        self.assertTrue(cargar(DATOS)['ya_cargado'])
        self.assertEqual(Receta.query.count(),41);self.assertEqual(MateriaPrima.query.count(),cuenta)
        self.assertEqual(db.session.get(Receta,r.id).notas,'Nota nueva')
    def test_edicion_historial_y_fuente_inmutable(self):
        r=Receta.query.first();original=r.origen
        self.assertEqual(self.editar(r,**{f'cantidad_{r.lineas[0].id}':'11'}).status_code,302)
        db.session.refresh(r)
        self.assertEqual(r.version,2);self.assertEqual(r.origen,original)
        self.assertEqual(r.lineas[0].cantidad,11)
        rev=RecetaRevision.query.filter_by(receta_id=r.id).order_by(RecetaRevision.id.desc()).first()
        self.assertEqual(Decimal(rev.anterior['lineas'][0]['cantidad']),10)
        self.assertEqual(r.estado,'Borrador')
    def test_validaciones_y_acceso(self):
        r=Receta.query.first()
        for cambios in [{'rendimiento':'NaN'},{'version':'0'},{'motivo':''},{'csrf':'x'},{f'vinculo_{r.lineas[0].id}':f'rec:{r.id}'}]:
            self.assertEqual(self.editar(r,**cambios).status_code,400)
        self.assertEqual(r.version,1)
        self.assertEqual(app.test_client().get('/admin/recetas').status_code,302)
        self.assertEqual(self.c.get('/admin/recetas').status_code,200)
        for r in Receta.query.all():self.assertEqual(self.c.get(f'/admin/recetas/{r.id}').status_code,200)

if __name__=='__main__':unittest.main()
