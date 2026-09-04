import os
import pandas as pd
from flask import Flask, request, jsonify, render_template, send_from_directory, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///erp.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'clave_secreta_nyds_admin_2026'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'xlsx'}

db = SQLAlchemy(app)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ==========================================
# Modelos de Base de Datos
# ==========================================
class Proveedor(db.Model):
    __tablename__ = 'proveedores'
    id_proveedor = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo_proveedor = db.Column(db.String(20), unique=True, nullable=False)
    razon_social = db.Column(db.String(180), nullable=False)
    rfc = db.Column(db.String(13), unique=True, nullable=False)
    tipo_proveedor = db.Column(db.String(50), nullable=False)
    estatus = db.Column(db.String(20), default='Pendiente', nullable=False)
    
    calle_numero = db.Column(db.String(200), nullable=False)
    colonia = db.Column(db.String(100), nullable=False)
    ciudad = db.Column(db.String(100), nullable=False, default='Cancún')
    estado = db.Column(db.String(100), nullable=False, default='Quintana Roo')
    codigo_postal = db.Column(db.String(5), nullable=False)
    
    condicion_pago = db.Column(db.String(50), nullable=False)
    tiempo_entrega_normal = db.Column(db.Numeric(10, 2), nullable=False)
    
    contactos = db.relationship('Contacto', backref='proveedor', lazy=True, cascade="all, delete-orphan")
    catalogo_items = db.relationship('ProveedorItem', backref='proveedor', lazy=True, cascade="all, delete-orphan")
    documentos = db.relationship('Documento', backref='proveedor', lazy=True, cascade="all, delete-orphan")
    bitacora = db.relationship('Bitacora', backref='proveedor', lazy=True, cascade="all, delete-orphan")

class Contacto(db.Model):
    __tablename__ = 'proveedor_contactos'
    id_contacto = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_proveedor = db.Column(db.Integer, db.ForeignKey('proveedores.id_proveedor'), nullable=False)
    nombre = db.Column(db.String(150), nullable=False)
    telefono = db.Column(db.String(25))
    correo = db.Column(db.String(150), nullable=False)
    tipo_contacto = db.Column(db.String(50), nullable=False)
    es_principal = db.Column(db.Boolean, default=False, nullable=False)

class MaestroItem(db.Model):
    __tablename__ = 'maestro_items'
    id_item = db.Column(db.Integer, primary_key=True, autoincrement=True)
    categoria = db.Column(db.String(50), nullable=False)
    nombre = db.Column(db.String(150), nullable=False)

class ProveedorItem(db.Model):
    __tablename__ = 'proveedor_items'
    id_proveedor_item = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_proveedor = db.Column(db.Integer, db.ForeignKey('proveedores.id_proveedor'), nullable=False)
    id_item = db.Column(db.Integer, db.ForeignKey('maestro_items.id_item'), nullable=False)
    descripcion_comercial = db.Column(db.String(200), nullable=False)
    precio_vigente = db.Column(db.Numeric(14, 4), nullable=False)
    moneda = db.Column(db.String(10), nullable=False)
    tiempo_entrega = db.Column(db.Numeric(10, 2), nullable=False)
    activo = db.Column(db.Boolean, default=True, nullable=False)

class Documento(db.Model):
    __tablename__ = 'proveedor_documentos'
    id_documento = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_proveedor = db.Column(db.Integer, db.ForeignKey('proveedores.id_proveedor'), nullable=False)
    tipo_documento = db.Column(db.String(100), nullable=False)
    nombre_original = db.Column(db.String(255), nullable=False)
    nombre_archivo_guardado = db.Column(db.String(255), nullable=False)
    fecha_carga = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

class Bitacora(db.Model):
    __tablename__ = 'proveedor_bitacora'
    id_bitacora = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_proveedor = db.Column(db.Integer, db.ForeignKey('proveedores.id_proveedor'), nullable=False)
    accion = db.Column(db.String(100), nullable=False)
    descripcion = db.Column(db.String(255), nullable=False)
    fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

# ==========================================
# Rutas del Sistema y Seguridad (Candados)
# ==========================================

@app.route('/', methods=['GET'])
def portal_publico():
    return render_template('portal_registro.html')

@app.route('/login', methods=['GET', 'POST'])
def login_admin():
    if request.method == 'POST':
        usuario = request.form.get('usuario') or (request.json.get('usuario') if request.is_json else None)
        password = request.form.get('password') or (request.json.get('password') if request.is_json else None)
        
        if usuario == 'compras.nyds' and password == 'nyds2026*':
            session['admin_logueado'] = True
            session.permanent = True
            if request.is_json:
                return jsonify({"exito": True, "redirect": "/admin/catalogo"})
            return redirect(url_for('vista_catalogo_admin'))
        else:
            if request.is_json:
                return jsonify({"error": "Credenciales incorrectas"}), 401
            return render_template('login.html', error="Credenciales incorrectas")
            
    return render_template('login.html')

@app.route('/logout', methods=['GET'])
def logout_admin():
    session.pop('admin_logueado', None)
    return redirect(url_for('login_admin'))

@app.route('/admin/catalogo', methods=['GET'])
def vista_catalogo_admin():
    if not session.get('admin_logueado'):
        return redirect(url_for('login_admin'))
    return render_template('catalogo.html')

@app.route('/admin/alta', methods=['GET'])
def vista_alta_admin():
    if not session.get('admin_logueado'):
        return redirect(url_for('login_admin'))
    return render_template('index.html')

@app.route('/admin/editar/<int:id_proveedor>', methods=['GET'])
def vista_editar_admin(id_proveedor):
    if not session.get('admin_logueado'):
        return redirect(url_for('login_admin'))
    proveedor = Proveedor.query.get_or_404(id_proveedor)
    return render_template('editar.html', id_proveedor=id_proveedor, codigo=proveedor.codigo_proveedor, razon=proveedor.razon_social)

# ==========================================
# API de Verificación en Tiempo Real
# ==========================================
@app.route('/api/verificar-duplicado', methods=['POST'])
def verificar_duplicado():
    data = request.json or {}
    campo = data.get('campo') 
    valor = data.get('valor', '').strip().lower()
    
    if not campo or not valor:
        return jsonify({"existe": False})

    if campo == 'rfc':
        val_limpio = valor.upper().replace(" ", "")
        p = Proveedor.query.filter_by(rfc=val_limpio).first()
        if p:
            return jsonify({"existe": True, "mensaje": "Este RFC ya se encuentra registrado en el sistema."})
            
    elif campo == 'razon_social':
        p = Proveedor.query.filter(db.func.lower(Proveedor.razon_social) == valor).first()
        if p:
            return jsonify({"existe": True, "mensaje": "Esta Razón Social ya se encuentra registrada en el sistema."})
            
    elif campo == 'domicilio':
        cp = data.get('codigo_postal', '').strip()
        p = Proveedor.query.filter(
            db.and_(
                db.func.lower(Proveedor.calle_numero) == valor,
                Proveedor.codigo_postal == cp
            )
        ).first()
        if p:
            return jsonify({"existe": True, "mensaje": "Este domicilio ya se encuentra registrado en el sistema."})

    return jsonify({"existe": False})

# ==========================================
# APIs Generales de Proveedores
# ==========================================
@app.route('/api/proveedores', methods=['GET', 'POST'])
def manejar_proveedores():
    if request.method == 'GET':
        busqueda = request.args.get('q', '').strip()
        query = Proveedor.query
        if busqueda:
            query = query.filter(
                db.or_(
                    Proveedor.razon_social.ilike(f"%{busqueda}%"),
                    Proveedor.rfc.ilike(f"%{busqueda}%"),
                    Proveedor.codigo_proveedor.ilike(f"%{busqueda}%")
                )
            )
        proveedores = query.all()
        lista = []
        for p in proveedores:
            contacto_prin = Contacto.query.filter_by(id_proveedor=p.id_proveedor, es_principal=True).first()
            lista.append({
                "id_proveedor": p.id_proveedor,
                "codigo_proveedor": p.codigo_proveedor,
                "razon_social": p.razon_social,
                "rfc": p.rfc,
                "tipo_proveedor": p.tipo_proveedor,
                "estatus": p.estatus,
                "domicilio": f"{p.calle_numero}, Col. {p.colonia}, {p.ciudad}, C.P. {p.codigo_postal}",
                "contacto": contacto_prin.nombre if contacto_prin else "Sin contacto",
                "telefono": contacto_prin.telefono if contacto_prin else "N/A"
            })
        return jsonify(lista), 200

    if request.method == 'POST':
        data = request.json
        rfc_normalizado = data.get('rfc', '').strip().upper().replace(" ", "")
        
        if Proveedor.query.filter_by(rfc=rfc_normalizado).first():
            return jsonify({"error": f"El RFC {rfc_normalizado} ya se encuentra registrado."}), 400
            
        nuevo_codigo = f"PROV-{int(datetime.utcnow().timestamp())}" 
        estatus_inicial = data.get('estatus_inicial', 'Pendiente')

        nuevo_proveedor = Proveedor(
            codigo_proveedor=nuevo_codigo,
            razon_social=data['razon_social'].strip(),
            rfc=rfc_normalizado,
            tipo_proveedor=data['tipo_proveedor'],
            estatus=estatus_inicial,
            calle_numero=data.get('calle_numero', 'S/N'),
            colonia=data.get('colonia', 'Centro'),
            ciudad=data.get('ciudad', 'Cancún'),
            estado=data.get('estado', 'Quintana Roo'),
            codigo_postal=data['codigo_postal'],
            condicion_pago=data['condicion_pago'],
            tiempo_entrega_normal=data['tiempo_entrega_normal']
        )
        db.session.add(nuevo_proveedor)
        db.session.flush() 
        
        for c_data in data.get('contactos', []):
            db.session.add(Contacto(id_proveedor=nuevo_proveedor.id_proveedor, nombre=c_data['nombre'], telefono=c_data.get('telefono'), correo=c_data['correo'], tipo_contacto=c_data['tipo_contacto'], es_principal=c_data.get('es_principal', False)))

        db.session.add(Bitacora(id_proveedor=nuevo_proveedor.id_proveedor, accion="Alta", descripcion=f"Registro inicial de proveedor ({estatus_inicial})"))
        db.session.commit()
        return jsonify({"mensaje": "Registro de proveedor exitoso", "codigo": nuevo_codigo}), 201

@app.route('/api/proveedores/<int:id_proveedor>/estatus', methods=['PATCH'])
def cambiar_estatus_proveedor(id_proveedor):
    if not session.get('admin_logueado'):
        return jsonify({"error": "No autorizado"}), 403
    proveedor = Proveedor.query.get_or_404(id_proveedor)
    data = request.json or {}
    nuevo_estatus = data.get('estatus')

    if not nuevo_estatus:
        if proveedor.estatus == 'Activo': nuevo_estatus = 'Bloqueado'
        else: nuevo_estatus = 'Activo'

    proveedor.estatus = nuevo_estatus
    db.session.add(Bitacora(id_proveedor=id_proveedor, accion="Cambio de Estatus", descripcion=f"Estatus actualizado a {nuevo_estatus}"))
    db.session.commit()
    
    return jsonify({"mensaje": f"Estatus actualizado a {nuevo_estatus}", "nuevo_estatus": proveedor.estatus}), 200

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)