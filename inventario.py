"""Saldos iniciales y ajustes auditables; no altera las entradas de compras."""
from datetime import datetime,date
from decimal import Decimal,InvalidOperation
from functools import wraps
import secrets,hashlib,json
from flask import request,session,redirect,render_template,url_for,abort
from sqlalchemy import update,select,func
from sqlalchemy.exc import IntegrityError,OperationalError

if __package__:
    from .reportes_pdf import celda, generar_pdf, respuesta_pdf
else:
    from reportes_pdf import celda, generar_pdf, respuesta_pdf

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

    def reservado_por_mp():
        # Las reservas las crea el CRM (se registra despues); se leen por la tabla para no depender del orden de registro.
        t=db.metadata.tables.get('crm_reservas_erp')
        if t is None:return {}
        filas=db.session.execute(select(t.c.mp_id,func.sum(t.c.cantidad)).where(t.c.tipo=='Materia prima',t.c.estado=='Activa').group_by(t.c.mp_id)).all()
        return {mp_id:Decimal(str(total or 0)) for mp_id,total in filas}

    def resumen_por_mp(filtrados,demo):
        """Una fila por materia prima: existencia total, lo reservado por pedidos, lo disponible y su alerta de reorden."""
        totales={}
        for r in filtrados:totales[r['mp_id']]=totales.get(r['mp_id'],Decimal(0))+r['cantidad']
        reservado={} if demo else reservado_por_mp()
        filas=[]
        for m in MP.query.order_by(MP.nombre_oficial).all():
            if not m.activo and m.id_mp not in totales:continue
            existencia=totales.get(m.id_mp,Decimal(0));res=reservado.get(m.id_mp,Decimal(0));disponible=existencia-res
            cfg=m.inventario_config;minimo=cfg.stock_minimo if cfg else Decimal(0);reorden=cfg.punto_reorden if cfg else Decimal(0)
            if existencia<=0:estado='Sin existencia'
            elif minimo and disponible<minimo:estado='Bajo mínimo'
            elif reorden and disponible<=reorden:estado='Reordenar'
            else:estado='OK'
            filas.append(dict(mp=m,existencia=existencia,reservado=res,disponible=disponible,minimo=minimo or Decimal(0),reorden=reorden or Decimal(0),estado=estado))
        return filas

    @app.route('/admin/inventario-mp')
    @protegido
    def inventario_mp():
        ambiente=request.args.get('ambiente','real');demo=ambiente=='demo'
        todos=movimientos();filtrados=[r for r in todos if r['demo']==demo];saldos={}
        for r in filtrados:
            k=clave(r)
            if k not in saldos:saldos[k]={**r,'cantidad':Decimal(0)}
            saldos[k]['cantidad']+=r['cantidad']
        resumen=resumen_por_mp(filtrados,demo)
        return render_template('inventario_mp.html',saldos=saldos.values(),movimientos=filtrados,demo=demo,hay_demo=any(r['demo'] for r in todos),hoy=date.today(),resumen=resumen,alertas=sum(1 for f in resumen if f['estado']!='OK'))

    @app.route('/admin/inventario-mp/pdf')
    @protegido
    def inventario_mp_pdf():
        ambiente=request.args.get('ambiente','real');demo=ambiente=='demo'
        todos=movimientos();filtrados=[r for r in todos if r['demo']==demo];saldos={}
        for r in filtrados:
            k=clave(r)
            if k not in saldos:saldos[k]={**r,'cantidad':Decimal(0)}
            saldos[k]['cantidad']+=r['cantidad']
        filas=[]
        for s in sorted(saldos.values(),key=lambda s:(s['mp'].nombre_oficial,s['ubicacion'])):
            vencido=' (VENCIDO)' if s['caducidad'] and s['caducidad']<=date.today() else ''
            filas.append([
                celda(s['mp'].nombre_oficial),
                celda(s['ubicacion']),
                celda((s['lote'] or 'Sin lote')+vencido, muted=bool(vencido)),
                celda(f"{s['cantidad']:g} {s['unidad']}"),
            ])
        resumen=resumen_por_mp(filtrados,demo)
        buffer=generar_pdf(
            titulo='Inventario de materia prima'+(' (DEMO)' if demo else ''),
            subtitulo='Existencias por materia prima y saldo por ubicación, lote y caducidad.',
            secciones=[{
                'titulo':'Existencias por materia prima',
                'columnas':['Materia prima','Existencia','Reservado','Disponible','Punto de reorden','Estado'],
                'filas':[[celda(f['mp'].nombre_oficial),celda(f"{f['existencia']:g} {f['mp'].unidad_base}"),celda(f"{f['reservado']:g}",muted=True),celda(f"{f['disponible']:g}",negrita=True),celda(f"{f['reorden']:g}",muted=True),celda(f['estado'],negrita=f['estado']!='OK')] for f in resumen],
                'anchos':[5.6,2.8,2.2,2.4,2.6,2.8],
                'vacio':'No hay materias primas activas.',
            },{
                'titulo':'Saldo por ubicación y lote',
                'columnas':['Materia prima','Ubicación','Lote / caducidad','Saldo'],
                'filas':filas,
                'anchos':[7,4,4,3.7],
                'vacio':'Aún no hay movimientos registrados.',
            }],
        )
        return respuesta_pdf(buffer,f"inventario-mp-{'demo' if demo else 'real'}.pdf")

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
