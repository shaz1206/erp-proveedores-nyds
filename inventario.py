"""Saldos iniciales y ajustes auditables; no altera las entradas de compras."""
from datetime import datetime,date
from decimal import Decimal,InvalidOperation
from functools import wraps
import secrets,hashlib,json
from flask import request,session,redirect,render_template,url_for,abort
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError,OperationalError

def registrar_inventario(app,db,MP,Entrada,usuario_actual):
    class AjusteInventario(db.Model):
        __tablename__='inventario_mp_ajustes'
        id=db.Column(db.Integer,primary_key=True)
        mp_id=db.Column(db.Integer,db.ForeignKey('materias_primas.id_mp'),nullable=False)
        mp=db.relationship(MP)
        fecha=db.Column(db.DateTime,default=datetime.utcnow,nullable=False)
        tipo=db.Column(db.String(25),nullable=False)
        cantidad=db.Column(db.Numeric(18,6),nullable=False)
        unidad=db.Column(db.String(20),nullable=False)
        ubicacion=db.Column(db.String(220),nullable=False)
        lote=db.Column(db.String(100),nullable=False,default='')
        caducidad=db.Column(db.Date)
        demo=db.Column(db.Boolean,nullable=False,default=False)
        motivo=db.Column(db.String(2000),nullable=False)
        referencia=db.Column(db.String(160),nullable=False)
        usuario=db.Column(db.String(100),nullable=False)
        token=db.Column(db.String(80),unique=True,nullable=False)
        inicial_clave=db.Column(db.String(64),unique=True)

    def protegido(fn):
        @wraps(fn)
        def wrapper(*a,**kw):
            if not session.get('admin_logueado'):return redirect('/login')
            session.setdefault('inventario_csrf',secrets.token_hex(24))
            if request.method=='POST' and not secrets.compare_digest(request.form.get('csrf',''),session['inventario_csrf']):abort(400)
            return fn(*a,**kw)
        return wrapper

    def movimientos():
        rows=[]
        for e in Entrada.query.all():
            r=e.recepcion;o=r.linea.orden
            rows.append(dict(mp=e.mp,mp_id=e.mp_id,unidad=e.unidad,ubicacion=r.ubicacion,lote=r.lote or '',caducidad=r.caducidad,demo=o.proveedor_nombre.startswith('DEMO - '),cantidad=e.cantidad,fecha=r.fecha,tipo='Recepción',referencia=f'OC-{o.id:05d} / REC-{r.id}',motivo=r.observaciones or 'Recepción aceptada',usuario=r.usuario,url=f'/admin/compras/{o.id}'))
        for a in AjusteInventario.query.all():
            rows.append(dict(mp=a.mp,mp_id=a.mp_id,unidad=a.unidad,ubicacion=a.ubicacion,lote=a.lote,caducidad=a.caducidad,demo=a.demo,cantidad=a.cantidad,fecha=a.fecha,tipo=a.tipo,referencia=a.referencia,motivo=a.motivo,usuario=a.usuario,url=None))
        return sorted(rows,key=lambda r:r['fecha'],reverse=True)

    def clave(r):return (r['mp_id'],r['unidad'],r['ubicacion'],r['lote'],r['caducidad'],r['demo'])

    @app.route('/admin/inventario-mp')
    @protegido
    def inventario_mp():
        ambiente=request.args.get('ambiente','real');demo=ambiente=='demo'
        todos=movimientos();filtrados=[r for r in todos if r['demo']==demo];saldos={}
        for r in filtrados:
            k=clave(r)
            if k not in saldos:saldos[k]={**r,'cantidad':Decimal(0)}
            saldos[k]['cantidad']+=r['cantidad']
        return render_template('inventario_mp.html',saldos=saldos.values(),movimientos=filtrados,demo=demo,hay_demo=any(r['demo'] for r in todos),hoy=date.today())

    @app.route('/admin/inventario-mp/registrar',methods=['GET','POST'])
    @protegido
    def inventario_registrar():
        error=None
        if request.method=='POST':
            try:
                def texto(k,lim,required=True):
                    v=request.form.get(k,'').strip()
                    if len(v)>lim or (required and not v):raise ValueError(f'Revisa {k}: '+('campo obligatorio; ' if required else '')+f'máximo {lim} caracteres.')
                    return v
                token=texto('token',80)
                previo=AjusteInventario.query.filter_by(token=token).first()
                if previo:return redirect(url_for('inventario_mp',ambiente='demo' if previo.demo else 'real'))
                mid=int(request.form.get('mp_id','0'));m=db.session.get(MP,mid)
                if not m or not m.activo:raise ValueError('Selecciona una materia prima activa.')
                # Serializa cambios por materia prima antes de calcular el saldo.
                db.session.execute(update(MP).where(MP.id_mp==mid).values(fecha_actualizacion=MP.fecha_actualizacion))
                tipo=texto('tipo',25)
                if tipo not in ['Saldo inicial','Ajuste de entrada','Ajuste de salida']:raise ValueError('Tipo de movimiento inválido.')
                ambiente=texto('ambiente',10)
                if ambiente not in ['real','demo']:raise ValueError('Selecciona el ambiente.')
                demo=ambiente=='demo'
                if request.form.get('confirmado')!='si':raise ValueError('Confirma que revisaste el ambiente, la cantidad y la unidad.')
                cantidad=Decimal(request.form.get('cantidad',''))
                if not cantidad.is_finite() or cantidad<=0 or cantidad>=Decimal('1000000000') or cantidad.as_tuple().exponent < -6:raise ValueError('Cantidad positiva, menor a mil millones y con máximo seis decimales.')
                if not m.permite_fraccion and cantidad%1:raise ValueError('Esta materia prima no permite fracciones.')
                if request.form.get('unidad')!=m.unidad_base:raise ValueError('La unidad base cambió. Recarga la página y revisa la cantidad.')
                ubicacion=texto('ubicacion',220);lote=texto('lote',100,False)
                caducidad=date.fromisoformat(request.form['caducidad']) if request.form.get('caducidad') else None
                cfg=m.inventario_config
                if cfg and cfg.control_lote and not lote:raise ValueError('Se requiere lote.')
                if cfg and cfg.control_caducidad and not caducidad:raise ValueError('Se requiere caducidad.')
                if tipo!='Ajuste de salida' and caducidad and caducidad<=date.today():raise ValueError('No registres material vencido como existencia disponible.')
                ref=texto('referencia',160);motivo=texto('motivo',2000)
                k=(mid,m.unidad_base,ubicacion,lote,caducidad,demo)
                relacionados=[r for r in movimientos() if clave(r)==k]
                saldo=sum((r['cantidad'] for r in relacionados),Decimal(0))
                inicial=None
                if tipo=='Saldo inicial':
                    if relacionados:raise ValueError('Esta combinación ya tiene movimientos. Usa un ajuste para corregirla.')
                    inicial=hashlib.sha256(json.dumps(k,default=str).encode()).hexdigest()
                if tipo=='Ajuste de salida':
                    if cantidad>saldo:raise ValueError(f'La salida supera el saldo disponible ({saldo} {m.unidad_base}) para esa ubicación, lote y ambiente.')
                    cantidad=-cantidad
                if saldo+cantidad>=Decimal('1000000000000'):raise ValueError('El saldo supera el límite permitido.')
                db.session.add(AjusteInventario(mp_id=mid,tipo=tipo,cantidad=cantidad,unidad=m.unidad_base,ubicacion=ubicacion,lote=lote,caducidad=caducidad,demo=demo,motivo=motivo,referencia=ref,usuario=usuario_actual(),token=token,inicial_clave=inicial))
                db.session.commit();return redirect(url_for('inventario_mp',ambiente=ambiente))
            except (ValueError,InvalidOperation,IntegrityError,OperationalError) as e:
                db.session.rollback();error=str(e) if isinstance(e,ValueError) else 'No se pudo guardar. Revisa la cantidad y recarga para comprobar si el movimiento ya existe.'
        return render_template('inventario_registrar.html',materias=MP.query.filter_by(activo=True).order_by(MP.nombre_oficial).all(),error=error,token=request.form.get('token') or secrets.token_hex(24)),(400 if error else 200)
    return AjusteInventario
