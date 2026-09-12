"""Recetas iniciales en borrador, con fuente inmutable y revisión editable."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json, secrets
from flask import request, session, redirect, render_template, abort, url_for
from sqlalchemy import update


def registrar_recetas(app, db, MP, PT, usuario_actual):
    class Receta(db.Model):
        __tablename__='recetas_borrador'
        id=db.Column(db.Integer,primary_key=True)
        clave_origen=db.Column(db.String(100),unique=True,nullable=False)
        nombre=db.Column(db.String(180),nullable=False)
        tipo=db.Column(db.String(30),nullable=False)
        estado=db.Column(db.String(20),nullable=False,default='Borrador')
        producto_id=db.Column(db.Integer,db.ForeignKey('productos_terminados.id'))
        producto=db.relationship(PT)
        rendimiento=db.Column(db.Numeric(18,6))
        unidad_rendimiento=db.Column(db.String(20))
        notas=db.Column(db.Text)
        pendientes=db.Column(db.Text)
        origen=db.Column(db.JSON,nullable=False)
        version=db.Column(db.Integer,nullable=False,default=1)
        lineas=db.relationship('RecetaIngrediente',backref='receta',foreign_keys='RecetaIngrediente.receta_id',order_by='RecetaIngrediente.id')

    class RecetaIngrediente(db.Model):
        __tablename__='receta_ingredientes'
        id=db.Column(db.Integer,primary_key=True)
        receta_id=db.Column(db.Integer,db.ForeignKey('recetas_borrador.id'),nullable=False)
        mp_id=db.Column(db.Integer,db.ForeignKey('materias_primas.id_mp'))
        mp=db.relationship(MP)
        subreceta_id=db.Column(db.Integer,db.ForeignKey('recetas_borrador.id'))
        subreceta=db.relationship(Receta,foreign_keys=[subreceta_id])
        nombre_original=db.Column(db.String(180),nullable=False)
        cantidad=db.Column(db.Numeric(18,6),nullable=False)
        unidad=db.Column(db.String(20),nullable=False)
        proveedor_referencia=db.Column(db.String(250))
        observaciones=db.Column(db.Text)

    class RecetaRevision(db.Model):
        __tablename__='receta_revisiones'
        id=db.Column(db.Integer,primary_key=True)
        receta_id=db.Column(db.Integer,db.ForeignKey('recetas_borrador.id'),nullable=False)
        fecha=db.Column(db.DateTime,default=datetime.utcnow,nullable=False)
        usuario=db.Column(db.String(100),nullable=False)
        motivo=db.Column(db.Text,nullable=False)
        anterior=db.Column(db.JSON,nullable=False)

    def snapshot(r):
        return dict(nombre=r.nombre,rendimiento=str(r.rendimiento) if r.rendimiento is not None else None,unidad=r.unidad_rendimiento,notas=r.notas,pendientes=r.pendientes,version=r.version,lineas=[dict(id=l.id,cantidad=str(l.cantidad),unidad=l.unidad,mp_id=l.mp_id,subreceta_id=l.subreceta_id,proveedor=l.proveedor_referencia,observaciones=l.observaciones) for l in r.lineas])

    @app.route('/admin/recetas')
    def recetas():
        if not session.get('admin_logueado'):return redirect('/login')
        q=request.args.get('q','').strip()
        consulta=Receta.query
        if q:consulta=consulta.filter(Receta.nombre.contains(q,autoescape=True))
        return render_template('recetas.html',recetas=consulta.order_by(Receta.id).all(),q=q)

    @app.route('/admin/recetas/<int:id_receta>',methods=['GET','POST'])
    def receta_detalle(id_receta):
        if not session.get('admin_logueado'):return redirect('/login')
        session.setdefault('recetas_csrf',secrets.token_hex(24))
        r=db.get_or_404(Receta,id_receta);error=None
        unidades=['g','kg','ml','mL','L','pieza']
        if request.method=='POST':
            if not secrets.compare_digest(request.form.get('csrf',''),session['recetas_csrf']):abort(400)
            try:
                anterior=snapshot(r)
                version=int(request.form.get('version','0'))
                if version!=r.version:raise ValueError('La receta cambió. Recarga antes de editar para no perder cambios.')
                motivo=request.form.get('motivo','').strip()
                if not motivo or len(motivo)>1000:raise ValueError('Indica un motivo de actualización de hasta 1000 caracteres.')
                def numero(s):
                    n=Decimal(s)
                    if not n.is_finite() or n<=0 or n>=Decimal('1000000000') or n.as_tuple().exponent < -6:raise ValueError('Revisa las cantidades: positivas y con máximo seis decimales.')
                    return n
                rendimiento=numero(request.form['rendimiento']) if request.form.get('rendimiento') else None
                unidad=request.form.get('unidad_rendimiento','')
                if rendimiento and unidad not in unidades:raise ValueError('Selecciona la unidad del rendimiento.')
                nuevos=[]
                for l in r.lineas:
                    cantidad=numero(request.form.get(f'cantidad_{l.id}',''))
                    u=request.form.get(f'unidad_{l.id}','')
                    if u not in unidades:raise ValueError('Unidad de ingrediente inválida.')
                    vinculo=request.form.get(f'vinculo_{l.id}','')
                    mp_id=None;sub_id=None
                    if vinculo.startswith('mp:'):
                        mp_id=int(vinculo[3:])
                        if not db.session.get(MP,mp_id):raise ValueError('Materia prima no encontrada.')
                    elif vinculo.startswith('rec:'):
                        sub_id=int(vinculo[4:])
                        if not db.session.get(Receta,sub_id):raise ValueError('Subreceta no encontrada.')
                    else:raise ValueError('Selecciona el ingrediente del catálogo o una subreceta.')
                    prov=request.form.get(f'proveedor_{l.id}','').strip()
                    if len(prov)>250:raise ValueError('Referencia de proveedor demasiado larga.')
                    nuevos.append((l,cantidad,u,mp_id,sub_id,prov))
                grafo={x.id:[l.subreceta_id for l in x.lineas if l.subreceta_id] for x in Receta.query.all()}
                grafo[r.id]=[s for l,c,u,m,s,p in nuevos if s]
                def ciclo(n,pila):
                    if n in pila:return True
                    return any(ciclo(h,pila|{n}) for h in grafo.get(n,[]))
                if ciclo(r.id,set()):raise ValueError('Una receta no puede consumirse a sí misma, directa o indirectamente.')
                notas=request.form.get('notas','');pendientes=request.form.get('pendientes','')
                if len(notas)>12000 or len(pendientes)>12000:raise ValueError('Notas demasiado largas.')
                resultado=db.session.execute(update(Receta).where(Receta.id==r.id,Receta.version==version).values(version=version+1,rendimiento=rendimiento,unidad_rendimiento=unidad if rendimiento else None,notas=notas,pendientes=pendientes).execution_options(synchronize_session=False))
                if resultado.rowcount!=1:raise ValueError('Otra persona actualizó la receta. Recarga la página.')
                for l,c,u,m,s,p in nuevos:l.cantidad=c;l.unidad=u;l.mp_id=m;l.subreceta_id=s;l.proveedor_referencia=p
                db.session.add(RecetaRevision(receta_id=r.id,usuario=usuario_actual(),motivo=motivo,anterior=anterior))
                db.session.commit();return redirect(url_for('receta_detalle',id_receta=r.id,guardado=1))
            except (ValueError,InvalidOperation) as e:
                db.session.rollback();error=str(e) if isinstance(e,ValueError) else 'Cantidad inválida.'
        return render_template('receta_detalle.html',r=r,error=error,unidades=unidades,materias=MP.query.order_by(MP.nombre_oficial).all(),subrecetas=Receta.query.filter(Receta.id!=r.id).order_by(Receta.nombre).all(),revisiones=RecetaRevision.query.filter_by(receta_id=r.id).order_by(RecetaRevision.id.desc()).all()),(400 if error else 200)
    return Receta,RecetaIngrediente,RecetaRevision
