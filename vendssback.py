import os
import pandas as pd
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///erp.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
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
    estatus = db.Column(db.String(20), default='Activo', nullable=False)
    codigo_postal = db.Column(db.String(5), nullable=False)
    condicion_pago = db.Column(db.String(50), nullable=False)
    lead_time_habitual = db.Column(db.Numeric(10, 2), nullable=False)
    
    contactos = db.relationship('Contacto', backref='proveedor', lazy=True, cascade="all, delete-orphan")
    materias_primas = db.relationship('ProveedorMateriaPrima', backref='proveedor', lazy=True, cascade="all, delete-orphan")
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

class MateriaPrima(db.Model):
    __tablename__ = 'materias_primas'
    id_materia_prima = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nombre = db.Column(db.String(150), nullable=False)

class ProveedorMateriaPrima(db.Model):
    __tablename__ = 'proveedor_materia_prima'
    id_proveedor_mp = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_proveedor = db.Column(db.Integer, db.ForeignKey('proveedores.id_proveedor'), nullable=False)
    id_materia_prima = db.Column(db.Integer, db.ForeignKey('materias_primas.id_materia_prima'), nullable=False)
    presentacion_compra = db.Column(db.String(100), nullable=False)
    unidad_compra = db.Column(db.String(50), nullable=False)
    contenido_presentacion = db.Column(db.Numeric(10, 2), nullable=False)
    precio_vigente = db.Column(db.Numeric(14, 4), nullable=False)
    moneda = db.Column(db.String(10), nullable=False)
    lead_time = db.Column(db.Numeric(10, 2), nullable=False)
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
# Rutas del Sistema
# ==========================================
@app.route('/', methods=['GET'])
def inicio(): return render_template('catalogo.html')

@app.route('/alta', methods=['GET'])
def vista_alta(): return render_template('index.html')

@app.route('/editar/<int:id_proveedor>', methods=['GET'])
def vista_editar(id_proveedor):
    proveedor = Proveedor.query.get_or_404(id_proveedor)
    return render_template('editar.html', id_proveedor=id_proveedor, codigo=proveedor.codigo_proveedor, razon=proveedor.razon_social)

@app.route('/api/proveedores', methods=['GET', 'POST'])
def manejar_proveedores():
    if request.method == 'GET':
        proveedores = Proveedor.query.all()
        lista = []
        for p in proveedores:
            contacto_prin = Contacto.query.filter_by(id_proveedor=p.id_proveedor, es_principal=True).first()
            lista.append({
                "id_proveedor": p.id_proveedor,
                "codigo_proveedor": p.codigo_proveedor,
                "razon_social": p.razon_social,
                "rfc": p.rfc,
                "estatus": p.estatus,
                "contacto": contacto_prin.nombre if contacto_prin else "Sin contacto",
                "telefono": contacto_prin.telefono if contacto_prin else "N/A"
            })
        return jsonify(lista), 200

    if request.method == 'POST':
        data = request.json
        rfc_normalizado = data.get('rfc', '').strip().upper().replace(" ", "")
        if Proveedor.query.filter_by(rfc=rfc_normalizado).first():
            return jsonify({"error": f"Ya existe un proveedor con el RFC {rfc_normalizado}."}), 400
            
        nuevo_codigo = f"PROV-{int(datetime.utcnow().timestamp())}" 
        nuevo_proveedor = Proveedor(
            codigo_proveedor=nuevo_codigo,
            razon_social=data['razon_social'].strip(),
            rfc=rfc_normalizado,
            tipo_proveedor=data['tipo_proveedor'],
            codigo_postal=data['codigo_postal'],
            condicion_pago=data['condicion_pago'],
            lead_time_habitual=data['lead_time_habitual']
        )
        db.session.add(nuevo_proveedor)
        db.session.flush() 
        
        for c_data in data.get('contactos', []):
            db.session.add(Contacto(id_proveedor=nuevo_proveedor.id_proveedor, nombre=c_data['nombre'], telefono=c_data.get('telefono'), correo=c_data['correo'], tipo_contacto=c_data['tipo_contacto'], es_principal=c_data.get('es_principal', False)))

        db.session.add(Bitacora(id_proveedor=nuevo_proveedor.id_proveedor, accion="Alta", descripcion=f"Se creó el proveedor {nuevo_codigo}"))
        db.session.commit()
        return jsonify({"mensaje": "Proveedor creado con éxito", "codigo": nuevo_codigo}), 201

@app.route('/api/proveedores/<int:id_proveedor>/estatus', methods=['PATCH'])
def cambiar_estatus_proveedor(id_proveedor):
    proveedor = Proveedor.query.get_or_404(id_proveedor)
    
    if proveedor.estatus == 'Activo':
        proveedor.estatus = 'Bloqueado'
        mensaje = "Proveedor bloqueado correctamente. Sus históricos se han conservado."
        accion_bitacora = "Bloqueo corporativo"
    else:
        proveedor.estatus = 'Activo'
        mensaje = "Proveedor reactivado con éxito. Ya puede operar en nuevas compras."
        accion_bitacora = "Reactivación de proveedor"

    db.session.add(Bitacora(id_proveedor=id_proveedor, accion=accion_bitacora, descripcion=f"Cambio de estatus a {proveedor.estatus}"))
    db.session.commit()
    
    return jsonify({"mensaje": mensaje, "nuevo_estatus": proveedor.estatus}), 200

@app.route('/api/materias-primas', methods=['GET'])
def obtener_catalogo_mp():
    return jsonify([{"id_materia_prima": m.id_materia_prima, "nombre": m.nombre} for m in MateriaPrima.query.all()])

@app.route('/api/proveedores/<int:id_proveedor>/materias-primas', methods=['GET', 'POST'])
def manejar_mp_proveedor(id_proveedor):
    if request.method == 'GET':
        relaciones = ProveedorMateriaPrima.query.filter_by(id_proveedor=id_proveedor, activo=True).all()
        resultado = []
        for r in relaciones:
            mp = db.session.get(MateriaPrima, r.id_materia_prima)
            resultado.append({
                "id_proveedor_mp": r.id_proveedor_mp,
                "nombre": mp.nombre,
                "presentacion": r.presentacion_compra,
                "precio_vigente": float(r.precio_vigente),
                "moneda": r.moneda,
                "lead_time": float(r.lead_time)
            })
        return jsonify(resultado), 200

    if request.method == 'POST':
        data = request.json
        nueva = ProveedorMateriaPrima(
            id_proveedor=id_proveedor,
            id_materia_prima=data['id_materia_prima'],
            presentacion_compra=data['presentacion_compra'],
            unidad_compra=data['unidad_compra'],
            contenido_presentacion=data['contenido_presentacion'],
            precio_vigente=data['precio_vigente'],
            moneda=data['moneda'],
            lead_time=data['lead_time']
        )
        db.session.add(nueva)
        db.session.commit()
        return jsonify({"mensaje": "Materia prima asignada correctamente"}), 201

@app.route('/api/proveedores/<int:id_proveedor>/documentos', methods=['GET', 'POST'])
def manejar_documentos(id_proveedor):
    if request.method == 'GET':
        docs = Documento.query.filter_by(id_proveedor=id_proveedor).all()
        return jsonify([{
            "id_documento": d.id_documento,
            "tipo_documento": d.tipo_documento,
            "nombre_original": d.nombre_original,
            "fecha": d.fecha_carga.strftime('%Y-%m-%d %H:%M'),
            "url_descarga": f"/uploads/{d.nombre_archivo_guardado}"
        } for d in docs]), 200

    if request.method == 'POST':
        if 'archivo' not in request.files: return jsonify({"error": "Sin archivo"}), 400
        file = request.files['archivo']
        if file and allowed_file(file.filename):
            nombre_original = secure_filename(file.filename)
            nombre_guardado = f"{id_proveedor}_{int(datetime.utcnow().timestamp())}_{nombre_original}"
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], nombre_guardado))
            db.session.add(Documento(id_proveedor=id_proveedor, tipo_documento=request.form.get('tipo_documento'), nombre_original=nombre_original, nombre_archivo_guardado=nombre_guardado))
            db.session.commit()
            return jsonify({"mensaje": "Documento subido con éxito"}), 201
        return jsonify({"error": "Formato no permitido"}), 400

@app.route('/uploads/<filename>')
def descargar_archivo(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ==========================================
# 4. Carga Automática desde Excel al Iniciar
# ==========================================
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Leer el archivo Excel e inyectar al catálogo si está vacío o para sincronizar
        excel_path = 'insumos_nyds.xlsx'
        if os.path.exists(excel_path):
            df = pd.read_excel(excel_path)
            for nombre_insumo in df['nombre'].dropna():
                existente = MateriaPrima.query.filter_by(nombre=nombre_insumo).first()
                if not existente:
                    db.session.add(MateriaPrima(nombre=nombre_insumo))
            db.session.commit()
            print(">>> Catálogo de materias primas sincronizado exitosamente desde el archivo Excel.")
        else:
            print(">>> ADVERTENCIA: No se encontró el archivo 'insumos_nyds.xlsx'. El catálogo maestro estará vacío.")
            
    app.run(debug=True)