"""Carga explícita del recetario; idempotente y sin inventario ni precios ficticios."""
import json, re, os, sqlite3, argparse, hashlib
from pathlib import Path
from datetime import datetime
from decimal import Decimal
try:
    from .extraer_recetario import key
except ImportError:
    from extraer_recetario import key


def formula_key(s):
    s=key(s).replace('—',' ').replace('–',' ')
    return re.sub(r'\s+',' ',s.replace('(subreceta)','')).strip()


def cargar(datos):
    from .vendssback import (db,MateriaPrima, MateriaPrimaAlias, ProductoTerminado,
        Receta,RecetaIngrediente,RecetaRevision,normalizar_texto)
    fuente='Recetario_NYDS'
    existentes=Receta.query.filter(Receta.clave_origen.like(fuente+':%')).all()
    if existentes:
        if len(existentes)!=len(datos['recetas']) or any(r.origen['sha256']!=datos['sha256'] for r in existentes):
            raise ValueError('Ya existe una carga diferente o incompleta. Revisar antes de importar; no se sobrescribieron datos.')
        return dict(ya_cargado=True,recetas=len(existentes),materias_nuevas=0,productos_nuevos=0)
    materias_nuevas=0;productos_nuevos=0;records={}; refs={}
    def sku(model,campo,prefijo):
        n=1
        while model.query.filter(getattr(model,campo)==f'{prefijo}{n:05d}').first():n+=1
        return f'{prefijo}{n:05d}'
    try:
        for raw in datos['recetas']:
            producto=None
            if raw['tipo']=='Producto terminado':
                matches=[p for p in ProductoTerminado.query.all() if normalizar_texto(p.nombre)==normalizar_texto(raw['nombre'])]
                if len(matches)>1:raise ValueError('Más de un producto coincide con '+raw['nombre'])
                if matches:producto=matches[0]
                else:
                    producto=ProductoTerminado(sku=sku(ProductoTerminado,'sku','PT-R'),nombre=raw['nombre'],categoria='Por clasificar',estatus='Borrador',stock_minimo=0,puntos_venta=[])
                    db.session.add(producto);db.session.flush();productos_nuevos+=1
            pendientes=['Revisión pendiente del químico; fecha y revisión del documento sin completar.','Confirmar equivalencias de nombres, especificaciones y conversiones antes de producir.']
            if raw['rendimiento'] is None:pendientes.append('Falta confirmar el rendimiento del lote y su unidad. No se sumaron gramos y mililitros.')
            if raw['seccion'] in (22,23):pendientes.append('El documento declara equivalencia entre Shampoo Blanco y Multiusos N Zematic, pero difieren en cantidades/unidades. Se mantienen separados.')
            if raw['seccion']==16:pendientes.append('Revisar dosificación y presentación del abrillantador óptico; la nota del documento describe una medición no estandarizada.')
            r=Receta(clave_origen=fuente+':'+raw['clave'],nombre=raw['nombre'],tipo=raw['tipo'],producto_id=producto.id if producto else None,rendimiento=Decimal(raw['rendimiento']) if raw['rendimiento'] else None,unidad_rendimiento=raw['unidad_rendimiento'],notas='\n'.join(raw['notas']),pendientes='\n'.join(pendientes),origen={'archivo':datos['archivo'],'sha256':datos['sha256'],'receta':raw})
            db.session.add(r);db.session.flush();records[raw['clave']]=r
            k=formula_key(raw['nombre'])
            if k in refs:raise ValueError('Dos fórmulas tienen el mismo nombre normalizado.')
            refs[k]=r
        material_cache={};usos={}
        for raw in datos['recetas']:
            r=records[raw['clave']]
            for c in raw['componentes']:
                ref=refs.get(formula_key(c['nombre']))
                m=None
                if not ref:
                    normal=normalizar_texto(c['nombre'])
                    m=material_cache.get(normal)
                    if m is None:
                        m=MateriaPrima.query.filter_by(nombre_normalizado=normal).first()
                        alias=MateriaPrimaAlias.query.filter_by(alias_normalizado=normal,activo=True).first()
                        if alias:
                            if m and m.id_mp!=alias.id_mp:raise ValueError('Conflicto entre nombre y alias: '+c['nombre'])
                            m=db.session.get(MateriaPrima,alias.id_mp)
                        if m is None:
                            unidad={'ml':'mL','g':'g','kg':'kg','l':'L'}.get(c['unidad'].lower())
                            if not unidad:raise ValueError('Unidad no reconocida: '+c['unidad'])
                            m=MateriaPrima(sku_mp=sku(MateriaPrima,'sku_mp','MP-R'),nombre_oficial=c['nombre'],nombre_normalizado=normal,familia='Por clasificar',estado_fisico='Otro',unidad_base=unidad,unidad_preferida_receta=unidad,activo=False,borrador=True,se_compra_externamente=not c['proveedor'].upper().startswith('PROPIO'),creado_por='Carga inicial recetario',descripcion='Importado de Recetario_NYDS.docx como borrador.',observaciones_tecnicas='Pendiente: confirmar clasificación, estado físico, especificaciones, riesgos, unidad base, conversiones, proveedor y condiciones comerciales. La unidad base inicial es la primera unidad observada en el recetario; no representa una conversión validada.')
                            db.session.add(m);db.session.flush();materias_nuevas+=1
                        material_cache[normal]=m
                    usos.setdefault(m.id_mp,set()).add(c['unidad'].lower())
                db.session.add(RecetaIngrediente(receta_id=r.id,mp_id=m.id_mp if m else None,subreceta_id=ref.id if ref else None,nombre_original=c['nombre'],cantidad=Decimal(c['cantidad']),unidad=c['unidad'],proveedor_referencia=c['proveedor'],observaciones=c['observaciones']))
            db.session.flush()
        for r in records.values():
            for l in r.lineas:
                if l.mp_id and len(usos.get(l.mp_id,set()))>1:
                    r.pendientes+='\n'+l.nombre_original+': aparece en distintas unidades; confirmar densidad o equivalencia.'
                if not l.proveedor_referencia:r.pendientes+='\n'+l.nombre_original+': falta referencia de proveedor.'
            db.session.add(RecetaRevision(receta_id=r.id,usuario='Carga inicial recetario',motivo='Importación inicial en borrador; documento original conservado.',anterior={'origen':r.origen}))
        db.session.commit()
        return dict(ya_cargado=False,recetas=len(records),semiterminados=sum(r.tipo=='Semiterminado' for r in records.values()),materias_nuevas=materias_nuevas,productos_nuevos=productos_nuevos,componentes=sum(len(r.lineas) for r in records.values()),enlaces_subrecetas=sum(l.subreceta_id is not None for r in records.values() for l in r.lineas))
    except Exception:
        db.session.rollback();raise


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--database',required=True);args=parser.parse_args()
    target=Path(args.database).resolve()
    root=Path(__file__).resolve().parent.parent
    if target!=root/'instance/erp.db':raise ValueError('La carga inicial está limitada a la base actual instance/erp.db.')
    data_path=Path(__file__).parent/'data/recetario/Recetario_NYDS.json'
    datos=json.loads(data_path.read_text(encoding='utf-8'))
    if hashlib.sha256(data_path.with_suffix('.docx').read_bytes()).hexdigest()!=datos['sha256']:raise ValueError('La fuente no coincide con el archivo extraído.')
    backups=root/'backups';backups.mkdir(exist_ok=True)
    backup=backups/('erp-antes-recetario-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.db')
    with sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True) as src:
        with sqlite3.connect(backup) as dest:src.backup(dest)
    os.environ['DATABASE_URL']='sqlite:///'+target.as_posix()
    from .vendssback import app, db
    with app.app_context():
        result=cargar(datos);result.update(database=str(target),backup=str(backup))
        (data_path.parent/'resultado_carga.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=True))

if __name__=='__main__':main()
