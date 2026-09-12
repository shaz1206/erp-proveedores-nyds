"""Compras y entradas de MP. Tablas nuevas, sin alterar catálogos existentes."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
import secrets
from flask import request, session, redirect, render_template, abort, url_for
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, OperationalError


def registrar_abastecimiento(app, db, MP, Proveedor, Condicion, usuario_actual):
    class Orden(db.Model):
        __tablename__ = 'compra_ordenes'
        id = db.Column(db.Integer, primary_key=True)
        proveedor_id = db.Column(db.Integer, db.ForeignKey('proveedores.id_proveedor'), nullable=False)
        proveedor = db.relationship(Proveedor)
        proveedor_nombre = db.Column(db.String(180), nullable=False)
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        entrega = db.Column(db.Date, nullable=False)
        estado = db.Column(db.String(25), default='Emitida', nullable=False)
        moneda = db.Column(db.String(10), nullable=False)
        notas = db.Column(db.Text)
        usuario = db.Column(db.String(100), nullable=False)
        token = db.Column(db.String(80), unique=True, nullable=False)
        version = db.Column(db.Integer, default=0, nullable=False)
        lineas = db.relationship('CompraLinea', backref='orden', lazy=True)
        @property
        def total(self):
            return sum((l.cantidad*l.precio*(1+l.iva/100) for l in self.lineas), Decimal(0))

    class CompraLinea(db.Model):
        __tablename__ = 'compra_lineas'
        id = db.Column(db.Integer, primary_key=True)
        orden_id = db.Column(db.Integer, db.ForeignKey('compra_ordenes.id'), nullable=False)
        mp_id = db.Column(db.Integer, db.ForeignKey('materias_primas.id_mp'), nullable=False)
        mp = db.relationship(MP)
        nombre = db.Column(db.String(150), nullable=False)
        presentacion = db.Column(db.String(100), nullable=False)
        unidad = db.Column(db.String(20), nullable=False)
        cantidad = db.Column(db.Numeric(18,6), nullable=False)
        factor = db.Column(db.Numeric(18,8), nullable=False)
        precio = db.Column(db.Numeric(18,6), nullable=False)
        iva = db.Column(db.Numeric(8,4), nullable=False)
        control_lote = db.Column(db.Boolean, nullable=False)
        control_caducidad = db.Column(db.Boolean, nullable=False)
        requiere_coa = db.Column(db.Boolean, nullable=False)
        criterio = db.Column(db.Text)
        recepciones = db.relationship('RecepcionMP', backref='linea', lazy=True)
        @property
        def pendiente(self):
            return self.cantidad-sum((r.aceptada+r.rechazada for r in self.recepciones),Decimal(0))

    class RecepcionMP(db.Model):
        __tablename__ = 'compra_recepciones'
        id = db.Column(db.Integer, primary_key=True)
        linea_id = db.Column(db.Integer, db.ForeignKey('compra_lineas.id'), nullable=False)
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        aceptada = db.Column(db.Numeric(18,6), nullable=False)
        rechazada = db.Column(db.Numeric(18,6), nullable=False)
        lote = db.Column(db.String(100))
        caducidad = db.Column(db.Date)
        ubicacion = db.Column(db.String(220), nullable=False)
        referencia = db.Column(db.String(160), nullable=False)
        coa = db.Column(db.String(160))
        observaciones = db.Column(db.Text)
        usuario = db.Column(db.String(100), nullable=False)
        token = db.Column(db.String(80), nullable=False, unique=True)

    class MovimientoMP(db.Model):
        __tablename__ = 'inventario_mp_entradas'
        id = db.Column(db.Integer, primary_key=True)
        recepcion_id = db.Column(db.Integer, db.ForeignKey('compra_recepciones.id'), unique=True, nullable=False)
        recepcion = db.relationship(RecepcionMP)
        mp_id = db.Column(db.Integer, db.ForeignKey('materias_primas.id_mp'), nullable=False)
        mp = db.relationship(MP)
        cantidad = db.Column(db.Numeric(18,6), nullable=False)
        unidad = db.Column(db.String(20), nullable=False)

    def protegido(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not session.get('admin_logueado'):
                return redirect('/login')
            if request.method == 'POST' and not secrets.compare_digest(request.form.get('csrf',''), session.get('compras_csrf','!')):
                abort(400, 'Sesión de formulario vencida. Recarga la página.')
            session.setdefault('compras_csrf', secrets.token_hex(24))
            return fn(*a, **kw)
        return wrapper

    def numero(value, positivo=False):
        try:
            n = Decimal(str(value))
            if not n.is_finite() or n < 0 or n >= Decimal('1000000000') or n.as_tuple().exponent < -6 or (positivo and n == 0):
                raise ValueError
            return n
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError('Usa cantidades válidas, con máximo seis decimales.')

    def texto(campo, maximo, obligatorio=False):
        v = request.form.get(campo,'').strip()
        if len(v)>maximo or (obligatorio and not v):
            raise ValueError(f'Revisa el campo {campo}: es obligatorio y admite hasta {maximo} caracteres.' if obligatorio else f'{campo}: máximo {maximo} caracteres.')
        return v

    def vigentes():
        hoy = date.today()
        return Condicion.query.join(Proveedor).join(MP).filter(Condicion.activo.is_(True), Proveedor.estatus=='Activo', MP.activo.is_(True), Condicion.vigencia_desde<=hoy, db.or_(Condicion.vigencia_hasta.is_(None), Condicion.vigencia_hasta>=hoy)).all()

    def precio_vista(c):
        try: return precio_presentacion(c)
        except ValueError: return None

    def precio_presentacion(c):
        if c.tipo_precio == 'Por presentación': return c.precio_vigente
        unidad = c.tipo_precio.removeprefix('Por ')
        if unidad == c.unidad_contenido: return c.precio_vigente*c.contenido_presentacion
        if unidad == c.materia_prima.unidad_base: return c.precio_vigente*c.factor_a_unidad_base
        raise ValueError('La unidad del precio no coincide con el contenido ni con la unidad base. Corrige la condición de compra.')

    @app.route('/admin/compras')
    @protegido
    def compras():
        return render_template('compras.html', ordenes=Orden.query.order_by(Orden.id.desc()).all())

    @app.route('/admin/compras/nueva', methods=['GET','POST'])
    @protegido
    def compra_nueva():
        error = None
        opciones = vigentes()
        if request.method == 'POST':
            try:
                token=texto('token',80,True)
                existente=Orden.query.filter_by(token=token).first()
                if existente: return redirect(url_for('compra_detalle',id_orden=existente.id))
                entrega=date.fromisoformat(request.form.get('entrega',''))
                if entrega<date.today(): raise ValueError('La fecha de entrega no puede ser anterior a hoy.')
                ids=request.form.getlist('condicion'); cantidades=request.form.getlist('cantidad')
                if not ids or len(ids)!=len(cantidades) or len(ids)>100 or len(set(ids))!=len(ids): raise ValueError('Agrega materiales distintos y sus cantidades.')
                elegidas=[]
                for cid, qty in zip(ids,cantidades):
                    c=next((c for c in opciones if str(c.id_relacion)==cid),None)
                    if c is None: raise ValueError('Selecciona una condición vigente de un proveedor y materia prima activos.')
                    q=numero(qty,True)
                    minimo=c.compra_minima or 0
                    if c.unidad_compra_minima=='Presentación': cumple=q>=minimo
                    elif c.unidad_compra_minima==c.unidad_contenido: cumple=q*c.contenido_presentacion>=minimo
                    elif c.unidad_compra_minima==c.materia_prima.unidad_base: cumple=q*c.factor_a_unidad_base>=minimo
                    else: raise ValueError('Corrige la unidad de compra mínima del proveedor.')
                    if not cumple: raise ValueError(f'{c.materia_prima.nombre_oficial}: no cumple la compra mínima de {minimo} {c.unidad_compra_minima}.')
                    if c.factor_a_unidad_base<=0: raise ValueError('El factor de conversión debe ser positivo.')
                    if not c.materia_prima.permite_fraccion and q*c.factor_a_unidad_base % 1: raise ValueError('La materia prima no permite fracciones de unidad base.')
                    precio=precio_presentacion(c).quantize(Decimal('.000001'))
                    if precio < 0 or precio >= Decimal('1000000000000') or q*c.factor_a_unidad_base >= Decimal('1000000000000'):
                        raise ValueError('El importe o la cantidad convertida supera el límite permitido.')
                    elegidas.append((c,q,precio))
                if len({(c.id_proveedor,c.moneda) for c,q,p in elegidas})!=1: raise ValueError('Cada orden debe tener un solo proveedor y una sola moneda.')
                c=elegidas[0][0]
                o=Orden(proveedor_id=c.id_proveedor,proveedor_nombre=c.proveedor.razon_social,entrega=entrega,moneda=c.moneda,notas=texto('notas',2000),usuario=usuario_actual(),token=token)
                db.session.add(o); db.session.flush()
                for c,q,p in elegidas:
                    cfg=c.materia_prima.inventario_config
                    db.session.add(CompraLinea(orden_id=o.id,mp_id=c.id_mp,nombre=c.materia_prima.nombre_oficial,presentacion=c.presentacion_compra,unidad=c.materia_prima.unidad_base,cantidad=q,factor=c.factor_a_unidad_base,precio=p,iva=c.iva_tasa,control_lote=bool(cfg and cfg.control_lote),control_caducidad=bool(cfg and cfg.control_caducidad),requiere_coa=c.materia_prima.requiere_coa,criterio=c.materia_prima.criterio_recepcion))
                db.session.commit()
                return redirect(url_for('compra_detalle',id_orden=o.id))
            except (ValueError, IntegrityError) as e:
                db.session.rollback(); error=str(e) if isinstance(e,ValueError) else 'La orden ya fue registrada. Consulta el catálogo.'
        return render_template('compra_nueva.html',opciones=opciones,error=error,token=request.form.get('token') or secrets.token_hex(24),hoy=date.today(),precio_presentacion=precio_vista), (400 if error else 200)

    @app.route('/admin/compras/<int:id_orden>',methods=['GET','POST'])
    @protegido
    def compra_detalle(id_orden):
        o=db.get_or_404(Orden,id_orden); error=None
        if request.method=='POST':
            try:
                token=texto('token',80,True)
                if RecepcionMP.query.filter_by(token=token).first(): return redirect(url_for('compra_detalle',id_orden=o.id))
                if o.estado not in ('Emitida','Parcial'): raise ValueError('Esta orden ya está cerrada.')
                version=o.version
                if request.form.get('accion')=='cancelar':
                    if any(l.recepciones for l in o.lineas): raise ValueError('No se puede cancelar una orden con recepciones.')
                    estado='Cancelada'
                else:
                    l=next((l for l in o.lineas if str(l.id)==request.form.get('linea')),None)
                    if not l: raise ValueError('Selecciona una partida de esta orden.')
                    db.session.execute(update(MP).where(MP.id_mp==l.mp_id).values(fecha_actualizacion=MP.fecha_actualizacion))
                    a=numero(request.form.get('aceptada','0')); r=numero(request.form.get('rechazada','0'))
                    if a+r==0 or a+r>l.pendiente: raise ValueError('La recepción debe ser mayor que cero y no superar lo pendiente.')
                    obs=texto('observaciones',2000)
                    if r and not obs: raise ValueError('Indica el motivo del rechazo.')
                    lote=texto('lote',100); coa=texto('coa',160)
                    venc=date.fromisoformat(request.form['caducidad']) if request.form.get('caducidad') else None
                    cfg=l.mp.inventario_config
                    if a and (l.control_lote or (cfg and cfg.control_lote)) and not lote: raise ValueError('La materia prima requiere lote.')
                    if a and (l.control_caducidad or (cfg and cfg.control_caducidad)) and not venc: raise ValueError('La materia prima requiere caducidad.')
                    if a and venc and venc<=date.today(): raise ValueError('No se puede aceptar material vencido.')
                    if a and (l.requiere_coa or l.mp.requiere_coa) and not coa: raise ValueError('Registra la referencia del certificado de análisis (COA).')
                    if a and request.form.get('conforme')!='si': raise ValueError('Confirma la revisión de calidad del material aceptado.')
                    cantidad_base=(a*l.factor).quantize(Decimal('.000001'))
                    if a and cantidad_base<=0: raise ValueError('La entrada es menor a la precisión permitida.')
                    if not l.mp.permite_fraccion and cantidad_base%1: raise ValueError('Esta materia prima no permite fracciones.')
                    rec=RecepcionMP(linea_id=l.id,aceptada=a,rechazada=r,lote=lote,caducidad=venc,ubicacion=texto('ubicacion',220,True),referencia=texto('referencia',160,True),coa=coa,observaciones=obs,usuario=usuario_actual(),token=token)
                    completa=all(x.pendiente-(a+r if x.id==l.id else 0)==0 for x in o.lineas)
                    rechazos=bool(r or any(rec.rechazada for x in o.lineas for rec in x.recepciones))
                    estado=('Cerrada con rechazo' if rechazos else 'Recibida') if completa else 'Parcial'
                # Actualización optimista: una segunda recepción simultánea debe recargar.
                resultado=db.session.execute(update(Orden).where(Orden.id==o.id,Orden.version==version).values(version=version+1,estado=estado).execution_options(synchronize_session=False))
                if resultado.rowcount!=1: raise ValueError('Otra persona actualizó esta orden. Recarga antes de recibir.')
                if request.form.get('accion')!='cancelar':
                    db.session.add(rec); db.session.flush()
                    if a: db.session.add(MovimientoMP(recepcion_id=rec.id,mp_id=l.mp_id,cantidad=cantidad_base,unidad=l.unidad))
                db.session.commit()
                return redirect(url_for('compra_detalle',id_orden=o.id))
            except (ValueError,IntegrityError,OperationalError) as e:
                db.session.rollback(); error=str(e) if isinstance(e,ValueError) else 'No se pudo guardar: la orden fue actualizada o la recepción ya existe. Recarga y verifica el historial.'
        return render_template('compra_detalle.html',o=o,error=error,token=secrets.token_hex(24)), (400 if error else 200)

    return Orden,CompraLinea,RecepcionMP,MovimientoMP
