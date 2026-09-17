import os
import re
import secrets
import unicodedata
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import (
    Flask,
    has_request_context,
    request,
    jsonify,
    render_template,
    send_from_directory,
    session,
    redirect,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, inspect
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
MP_UPLOAD_FOLDER = os.path.join(UPLOAD_FOLDER, "materias_primas")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(MP_UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
# Misma base al iniciar como script o como módulo de Flask.
DEFAULT_DATABASE = os.path.abspath(os.path.join(BASE_DIR, "..", "instance", "erp.db"))
os.makedirs(os.path.dirname(DEFAULT_DATABASE), exist_ok=True)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///" + DEFAULT_DATABASE.replace("\\", "/"))
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.urandom(32).hex()
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "xlsx", "docx"}

db = SQLAlchemy(app)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


RFC_REGEX = re.compile(r"^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$")

ESTATUS_PROVEEDOR_VALIDOS = {"Borrador", "Activo", "Bloqueado", "Inactivo", "Pendiente"}
ESTADOS_OC_ABIERTOS = ("Emitida", "Parcial")


def rfc_valido(rfc):
    return bool(RFC_REGEX.match(rfc or ""))


def normalizar_rfc(valor):
    return re.sub(r"\s+", "", (valor or "").strip().upper())


def siguiente_codigo_proveedor():
    """Genera PROV-0001, PROV-0002, ... sin reutilizar códigos."""
    ultimo = 0
    for (codigo,) in db.session.query(Proveedor.codigo_proveedor).all():
        m = re.match(r"^PROV-(\d+)$", codigo or "")
        if m:
            ultimo = max(ultimo, int(m.group(1)))
    return f"PROV-{ultimo + 1:04d}"


def migrar_columnas_faltantes():
    """Migración ligera: agrega columnas nuevas a tablas ya existentes sin borrar datos.

    db.create_all() solo crea tablas que faltan; no altera tablas existentes. Como esta
    versión agrega columnas a 'proveedores', 'proveedor_contactos' y 'proveedor_documentos'
    (tablas que ya existían en instalaciones previas), se revisan aquí con ALTER TABLE ...
    ADD COLUMN. Todas las columnas se agregan NULL-ables a nivel de base de datos; las
    reglas de obligatoriedad funcional se validan en el código de las rutas.
    """
    inspector = inspect(db.engine)
    nombres_tablas_existentes = set(inspector.get_table_names())
    for tabla in db.metadata.sorted_tables:
        if tabla.name not in nombres_tablas_existentes:
            continue  # tabla nueva: la crea db.create_all()
        columnas_actuales = {c["name"] for c in inspector.get_columns(tabla.name)}
        for columna in tabla.columns:
            if columna.name in columnas_actuales:
                continue
            tipo_columna = columna.type.compile(db.engine.dialect)
            with db.engine.begin() as conexion:
                conexion.exec_driver_sql(
                    f'ALTER TABLE "{tabla.name}" ADD COLUMN "{columna.name}" {tipo_columna}'
                )


def normalizar_texto(valor):
    """Normaliza texto para comparación de duplicados sin alterar el texto mostrado."""
    valor = (valor or "").strip().upper()
    valor = unicodedata.normalize("NFKD", valor)
    valor = "".join(c for c in valor if not unicodedata.combining(c))
    valor = re.sub(r"\s+", " ", valor)
    return valor


def decimal_o_none(valor):
    if valor in (None, ""):
        return None
    try:
        numero = Decimal(str(valor))
        return numero if numero.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def bool_value(valor, default=False):
    if valor is None:
        return default
    if isinstance(valor, bool):
        return valor
    return str(valor).strip().lower() in {"1", "true", "si", "sí", "on", "yes"}


def fecha_o_none(valor):
    if not valor:
        return None
    if isinstance(valor, date):
        return valor
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def admin_required_json(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logueado"):
            return jsonify({"error": "No autorizado"}), 403
        return fn(*args, **kwargs)

    return wrapper


def usuario_actual():
    if not has_request_context():
        return "sistema"
    return session.get("usuario", "compras.nyds")


def dividir_proveedores_recetario(referencia):
    """Convierte la referencia textual del recetario en nombres de proveedor externos."""
    texto = (referencia or "").strip()
    if not texto:
        return []
    partes = re.split(r"\s*/\s*|\s+/\s+", texto)
    proveedores = []
    vistos = set()
    for parte in partes:
        nombre = re.sub(r"\s+", " ", parte).strip(" .,-")
        if not nombre:
            continue
        normal = normalizar_texto(nombre)
        if normal.startswith("PROPIO") or normal.startswith("SUBRECETA"):
            continue
        if normal not in vistos:
            proveedores.append(nombre)
            vistos.add(normal)
    return proveedores


# ==========================================
# Modelos existentes: Proveedores
# ==========================================
class Proveedor(db.Model):
    __tablename__ = "proveedores"
    id_proveedor = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo_proveedor = db.Column(db.String(20), unique=True, nullable=False)
    razon_social = db.Column(db.String(180), nullable=False)
    nombre_comercial = db.Column(db.String(180))
    rfc = db.Column(db.String(13), unique=True, nullable=False)
    tipo_proveedor = db.Column(db.String(50), nullable=False)
    estatus = db.Column(db.String(20), default="Borrador", nullable=False)
    sitio_web = db.Column(db.String(255))
    notas_generales = db.Column(db.Text)
    motivo_bloqueo = db.Column(db.String(300))

    calle_numero = db.Column(db.String(200), nullable=False)
    colonia = db.Column(db.String(100), nullable=False)
    ciudad = db.Column(db.String(100), nullable=False, default="Cancún")
    estado = db.Column(db.String(100), nullable=False, default="Quintana Roo")
    codigo_postal = db.Column(db.String(5), nullable=False)

    # Datos fiscales y administrativos (sección 4.2 de la especificación).
    regimen_fiscal = db.Column(db.String(150))
    correo_facturacion = db.Column(db.String(150))
    forma_pago_habitual = db.Column(db.String(50))
    metodo_pago_habitual = db.Column(db.String(10))
    banco = db.Column(db.String(100))
    beneficiario = db.Column(db.String(180))
    cuenta_bancaria = db.Column(db.String(30))
    clabe = db.Column(db.String(18))

    # Condiciones comerciales y logísticas generales (sección 4.4).
    condicion_pago = db.Column(db.String(50), nullable=False)
    dias_credito = db.Column(db.Integer, default=0)
    tiempo_entrega_normal = db.Column(db.Numeric(10, 2), nullable=False)
    lead_time_maximo = db.Column(db.Numeric(10, 2))
    politica_flete = db.Column(db.String(60))
    zona_entrega = db.Column(db.String(200))
    horario_atencion = db.Column(db.String(150))
    compra_minima_general = db.Column(db.Numeric(18, 6))
    moneda_compra_minima = db.Column(db.String(10))
    vigencia_cotizacion_dias = db.Column(db.Integer)

    creado_en = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    creado_por = db.Column(db.String(100))
    actualizado_en = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    actualizado_por = db.Column(db.String(100))

    contactos = db.relationship("Contacto", backref="proveedor", lazy=True, cascade="all, delete-orphan")
    catalogo_items = db.relationship("ProveedorItem", backref="proveedor", lazy=True, cascade="all, delete-orphan")
    documentos = db.relationship("Documento", backref="proveedor", lazy=True, cascade="all, delete-orphan")
    bitacora = db.relationship("Bitacora", backref="proveedor", lazy=True, cascade="all, delete-orphan")
    evaluaciones = db.relationship("ProveedorEvaluacion", backref="proveedor", lazy=True, cascade="all, delete-orphan")
    materias_primas = db.relationship("MateriaPrimaProveedor", back_populates="proveedor", lazy=True)

    def contacto_principal(self):
        return next((c for c in self.contactos if c.es_principal and c.activo), None)

    def campos_minimos_faltantes(self):
        """Regresa la lista de requisitos incumplidos para poder quedar Activo (sección 12)."""
        faltantes = []
        if not self.razon_social:
            faltantes.append("razón social")
        if not self.rfc or not rfc_valido(self.rfc):
            faltantes.append("RFC válido")
        if not self.tipo_proveedor:
            faltantes.append("tipo de proveedor")
        if not self.codigo_postal or len(self.codigo_postal) != 5:
            faltantes.append("código postal fiscal (5 dígitos)")
        if not self.condicion_pago:
            faltantes.append("condiciones de pago")
        if self.tiempo_entrega_normal is None:
            faltantes.append("tiempo de entrega habitual")
        principal = self.contacto_principal()
        if not principal or not principal.correo or not (principal.telefono or principal.whatsapp):
            faltantes.append("un contacto principal activo con correo y teléfono o WhatsApp")
        if (self.tipo_proveedor or "").strip().lower() in ("materia prima", "mixto"):
            relaciones_activas = [r for r in self.materias_primas if r.activo]
            if not relaciones_activas:
                faltantes.append("al menos una materia prima relacionada y activa")
        return faltantes


class Contacto(db.Model):
    __tablename__ = "proveedor_contactos"
    id_contacto = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_proveedor = db.Column(db.Integer, db.ForeignKey("proveedores.id_proveedor"), nullable=False)
    nombre = db.Column(db.String(150), nullable=False)
    puesto_area = db.Column(db.String(100))
    telefono = db.Column(db.String(25))
    whatsapp = db.Column(db.String(25))
    correo = db.Column(db.String(150), nullable=False)
    tipo_contacto = db.Column(db.String(50), nullable=False)
    es_principal = db.Column(db.Boolean, default=False, nullable=False)
    activo = db.Column(db.Boolean, default=True, nullable=False)


class MaestroItem(db.Model):
    __tablename__ = "maestro_items"
    id_item = db.Column(db.Integer, primary_key=True, autoincrement=True)
    categoria = db.Column(db.String(50), nullable=False)
    nombre = db.Column(db.String(150), nullable=False)


class ProveedorItem(db.Model):
    __tablename__ = "proveedor_items"
    id_proveedor_item = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_proveedor = db.Column(db.Integer, db.ForeignKey("proveedores.id_proveedor"), nullable=False)
    id_item = db.Column(db.Integer, db.ForeignKey("maestro_items.id_item"), nullable=False)
    descripcion_comercial = db.Column(db.String(200), nullable=False)
    precio_vigente = db.Column(db.Numeric(14, 4), nullable=False)
    moneda = db.Column(db.String(10), nullable=False)
    tiempo_entrega = db.Column(db.Numeric(10, 2), nullable=False)
    activo = db.Column(db.Boolean, default=True, nullable=False)


class Documento(db.Model):
    __tablename__ = "proveedor_documentos"
    id_documento = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_proveedor = db.Column(db.Integer, db.ForeignKey("proveedores.id_proveedor"), nullable=False)
    tipo_documento = db.Column(db.String(100), nullable=False)
    nombre_original = db.Column(db.String(255), nullable=False)
    nombre_archivo_guardado = db.Column(db.String(255), nullable=False)
    tamano_bytes = db.Column(db.Integer)
    usuario = db.Column(db.String(100))
    vigencia_desde = db.Column(db.Date)
    vigencia_hasta = db.Column(db.Date)
    version = db.Column(db.Integer, default=1, nullable=False)
    activo = db.Column(db.Boolean, default=True, nullable=False)
    fecha_carga = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Bitacora(db.Model):
    __tablename__ = "proveedor_bitacora"
    id_bitacora = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_proveedor = db.Column(db.Integer, db.ForeignKey("proveedores.id_proveedor"), nullable=False)
    accion = db.Column(db.String(100), nullable=False)
    descripcion = db.Column(db.String(255), nullable=False)
    usuario = db.Column(db.String(100))
    fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ProveedorEvaluacion(db.Model):
    """Evaluación manual del proveedor (sección 4.8): ponderación configurable, escala 1-5."""
    __tablename__ = "proveedor_evaluaciones"
    id_evaluacion = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_proveedor = db.Column(db.Integer, db.ForeignKey("proveedores.id_proveedor"), nullable=False)
    periodo = db.Column(db.String(20), nullable=False)
    calif_precio = db.Column(db.Numeric(3, 1), nullable=False)
    calif_calidad = db.Column(db.Numeric(3, 1), nullable=False)
    calif_entrega = db.Column(db.Numeric(3, 1), nullable=False)
    calif_atencion = db.Column(db.Numeric(3, 1), nullable=False)
    ponderacion_precio = db.Column(db.Numeric(5, 2), nullable=False, default=Decimal("25"))
    ponderacion_calidad = db.Column(db.Numeric(5, 2), nullable=False, default=Decimal("30"))
    ponderacion_entrega = db.Column(db.Numeric(5, 2), nullable=False, default=Decimal("30"))
    ponderacion_atencion = db.Column(db.Numeric(5, 2), nullable=False, default=Decimal("15"))
    comentarios = db.Column(db.String(500))
    usuario = db.Column(db.String(100), nullable=False)
    fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def calificacion_final(self):
        total_ponderacion = (
            self.ponderacion_precio + self.ponderacion_calidad + self.ponderacion_entrega + self.ponderacion_atencion
        )
        if not total_ponderacion:
            return Decimal("0")
        suma = (
            self.calif_precio * self.ponderacion_precio
            + self.calif_calidad * self.ponderacion_calidad
            + self.calif_entrega * self.ponderacion_entrega
            + self.calif_atencion * self.ponderacion_atencion
        )
        return (suma / total_ponderacion).quantize(Decimal("0.01"))


# ==========================================
# Modelos: Maestro de Materias Primas
# ==========================================
class MateriaPrima(db.Model):
    __tablename__ = "materias_primas"

    id_mp = db.Column(db.Integer, primary_key=True, autoincrement=True)
    sku_mp = db.Column(db.String(20), unique=True, nullable=False, index=True)
    nombre_oficial = db.Column(db.String(150), nullable=False)
    nombre_normalizado = db.Column(db.String(160), unique=True, nullable=False, index=True)
    nombre_corto = db.Column(db.String(60))
    descripcion = db.Column(db.String(500))

    familia = db.Column(db.String(100), nullable=False)
    subfamilia = db.Column(db.String(100))
    estado_fisico = db.Column(db.String(30), nullable=False)
    grado_concentracion = db.Column(db.String(100))
    cas = db.Column(db.String(80))

    unidad_base = db.Column(db.String(20), nullable=False)
    unidad_preferida_receta = db.Column(db.String(20))
    decimales_inventario = db.Column(db.Integer, nullable=False, default=3)
    densidad_kg_l = db.Column(db.Numeric(14, 6))
    fuente_densidad = db.Column(db.String(180))
    fecha_densidad = db.Column(db.Date)
    factor_conversion_manual = db.Column(db.Numeric(18, 8))
    permite_fraccion = db.Column(db.Boolean, default=True, nullable=False)

    condicion_almacenamiento = db.Column(db.String(250))
    temperatura_min_c = db.Column(db.Numeric(8, 2))
    temperatura_max_c = db.Column(db.Numeric(8, 2))
    incompatibilidades = db.Column(db.String(500))
    requiere_area_especial = db.Column(db.Boolean, default=False, nullable=False)

    fabricante_marca_referencia = db.Column(db.String(150))
    pureza_especificacion = db.Column(db.String(100))
    ph_referencia = db.Column(db.String(50))
    peligroso = db.Column(db.Boolean, default=False, nullable=False)
    clasificacion_riesgo = db.Column(db.String(150))
    criterio_recepcion = db.Column(db.String(500))
    requiere_coa = db.Column(db.Boolean, default=False, nullable=False)
    observaciones_tecnicas = db.Column(db.Text)

    se_compra_externamente = db.Column(db.Boolean, default=True, nullable=False)
    activo = db.Column(db.Boolean, default=False, nullable=False)
    borrador = db.Column(db.Boolean, default=True, nullable=False)

    creado_por = db.Column(db.String(100), nullable=False, default="sistema")
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    fecha_actualizacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    aliases = db.relationship("MateriaPrimaAlias", back_populates="materia_prima", cascade="all, delete-orphan", lazy=True)
    inventario_config = db.relationship("MateriaPrimaInventarioConfig", back_populates="materia_prima", uselist=False, cascade="all, delete-orphan")
    ubicaciones = db.relationship("MateriaPrimaUbicacion", back_populates="materia_prima", cascade="all, delete-orphan", lazy=True)
    proveedores = db.relationship("MateriaPrimaProveedor", back_populates="materia_prima", lazy=True)
    documentos = db.relationship("MateriaPrimaDocumento", back_populates="materia_prima", cascade="all, delete-orphan", lazy=True)
    costos_hist = db.relationship("MateriaPrimaCostoHist", back_populates="materia_prima", cascade="all, delete-orphan", lazy=True)
    bitacora = db.relationship("MateriaPrimaBitacora", back_populates="materia_prima", cascade="all, delete-orphan", lazy=True)

    @property
    def estatus(self):
        if self.activo:
            return "Activo"
        if self.borrador:
            return "Borrador"
        return "Inactivo"


class MateriaPrimaAlias(db.Model):
    __tablename__ = "materia_prima_aliases"
    id_alias = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_mp = db.Column(db.Integer, db.ForeignKey("materias_primas.id_mp"), nullable=False, index=True)
    alias = db.Column(db.String(150), nullable=False)
    alias_normalizado = db.Column(db.String(160), unique=True, nullable=False, index=True)
    tipo_alias = db.Column(db.String(50), default="Sinónimo")
    observacion = db.Column(db.String(250))
    activo = db.Column(db.Boolean, default=True, nullable=False)
    materia_prima = db.relationship("MateriaPrima", back_populates="aliases")


class MateriaPrimaInventarioConfig(db.Model):
    __tablename__ = "materia_prima_inventario_config"
    id_config = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_mp = db.Column(db.Integer, db.ForeignKey("materias_primas.id_mp"), unique=True, nullable=False)
    stock_minimo = db.Column(db.Numeric(18, 6), nullable=False, default=0)
    stock_seguridad = db.Column(db.Numeric(18, 6))
    punto_reorden = db.Column(db.Numeric(18, 6), nullable=False, default=0)
    stock_objetivo_maximo = db.Column(db.Numeric(18, 6), nullable=False, default=0)
    lead_time_reabasto_dias = db.Column(db.Integer, nullable=False, default=0)
    control_lote = db.Column(db.Boolean, default=False, nullable=False)
    control_caducidad = db.Column(db.Boolean, default=False, nullable=False)
    permite_stock_negativo = db.Column(db.Boolean, default=False, nullable=False)
    metodo_salida = db.Column(db.String(20), nullable=False, default="FIFO")
    materia_prima = db.relationship("MateriaPrima", back_populates="inventario_config")


class MateriaPrimaUbicacion(db.Model):
    __tablename__ = "materia_prima_ubicaciones"
    id_ubicacion_mp = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_mp = db.Column(db.Integer, db.ForeignKey("materias_primas.id_mp"), nullable=False, index=True)
    almacen = db.Column(db.String(100), nullable=False)
    ubicacion = db.Column(db.String(120), nullable=False)
    principal = db.Column(db.Boolean, default=False, nullable=False)
    activo = db.Column(db.Boolean, default=True, nullable=False)
    vigencia_desde = db.Column(db.Date, default=date.today, nullable=False)
    vigencia_hasta = db.Column(db.Date)
    materia_prima = db.relationship("MateriaPrima", back_populates="ubicaciones")


class MateriaPrimaProveedor(db.Model):
    __tablename__ = "materia_prima_proveedor"
    id_relacion = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_mp = db.Column(db.Integer, db.ForeignKey("materias_primas.id_mp"), nullable=False, index=True)
    id_proveedor = db.Column(db.Integer, db.ForeignKey("proveedores.id_proveedor"), nullable=False, index=True)
    principal = db.Column(db.Boolean, default=False, nullable=False)
    sku_proveedor = db.Column(db.String(60))
    marca_fabricante = db.Column(db.String(100))
    presentacion_compra = db.Column(db.String(100), nullable=False)
    contenido_presentacion = db.Column(db.Numeric(18, 6), nullable=False)
    unidad_contenido = db.Column(db.String(20), nullable=False)
    factor_a_unidad_base = db.Column(db.Numeric(18, 8), nullable=False)
    precio_vigente = db.Column(db.Numeric(14, 4), nullable=False)
    tipo_precio = db.Column(db.String(30), nullable=False, default="Por presentación")
    moneda = db.Column(db.String(10), nullable=False, default="MXN")
    iva_tasa = db.Column(db.Numeric(8, 4), nullable=False, default=16)
    flete_estimado = db.Column(db.Numeric(14, 4))
    modalidad_flete = db.Column(db.String(40))
    compra_minima = db.Column(db.Numeric(18, 6), nullable=False, default=1)
    unidad_compra_minima = db.Column(db.String(20), nullable=False, default="Presentación")
    lead_time_dias = db.Column(db.Integer, nullable=False, default=0)
    vigencia_desde = db.Column(db.Date, default=date.today, nullable=False)
    vigencia_hasta = db.Column(db.Date)
    activo = db.Column(db.Boolean, default=True, nullable=False)
    creado_por = db.Column(db.String(100), nullable=False, default="sistema")
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    materia_prima = db.relationship("MateriaPrima", back_populates="proveedores")
    proveedor = db.relationship("Proveedor", back_populates="materias_primas")


class MateriaPrimaDocumento(db.Model):
    __tablename__ = "materia_prima_documentos"
    id_documento = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_mp = db.Column(db.Integer, db.ForeignKey("materias_primas.id_mp"), nullable=False, index=True)
    tipo_documento = db.Column(db.String(100), nullable=False)
    nombre_original = db.Column(db.String(255), nullable=False)
    nombre_archivo_guardado = db.Column(db.String(255), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    vigencia_desde = db.Column(db.Date)
    vigencia_hasta = db.Column(db.Date)
    usuario = db.Column(db.String(100), nullable=False, default="sistema")
    fecha_carga = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    materia_prima = db.relationship("MateriaPrima", back_populates="documentos")


class MateriaPrimaCostoHist(db.Model):
    __tablename__ = "materia_prima_costos_hist"
    id_costo = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_mp = db.Column(db.Integer, db.ForeignKey("materias_primas.id_mp"), nullable=False, index=True)
    tipo_costo = db.Column(db.String(40), nullable=False)
    costo_unidad_base = db.Column(db.Numeric(14, 6), nullable=False)
    moneda = db.Column(db.String(10), nullable=False, default="MXN")
    origen = db.Column(db.String(150))
    fecha_vigencia = db.Column(db.Date, default=date.today, nullable=False)
    usuario = db.Column(db.String(100), nullable=False, default="sistema")
    materia_prima = db.relationship("MateriaPrima", back_populates="costos_hist")


class MateriaPrimaBitacora(db.Model):
    __tablename__ = "materia_prima_bitacora"
    id_bitacora = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_mp = db.Column(db.Integer, db.ForeignKey("materias_primas.id_mp"), nullable=False, index=True)
    campo_evento = db.Column(db.String(100), nullable=False)
    valor_anterior = db.Column(db.Text)
    valor_nuevo = db.Column(db.Text)
    usuario = db.Column(db.String(100), nullable=False, default="sistema")
    fecha_hora = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    motivo = db.Column(db.String(300))
    materia_prima = db.relationship("MateriaPrima", back_populates="bitacora")


# ==========================================
# Utilidades de Materias Primas
# ==========================================
def registrar_bitacora_mp(mp, evento, anterior=None, nuevo=None, motivo=None):
    db.session.add(
        MateriaPrimaBitacora(
            id_mp=mp.id_mp,
            campo_evento=evento,
            valor_anterior=None if anterior is None else str(anterior),
            valor_nuevo=None if nuevo is None else str(nuevo),
            usuario=usuario_actual(),
            motivo=motivo,
        )
    )


def generar_sku_mp():
    ultimo_id = db.session.query(func.max(MateriaPrima.id_mp)).scalar() or 0
    return f"MP-{ultimo_id + 1:06d}"


def generar_codigo_proveedor_recetario():
    n = 1
    while Proveedor.query.filter_by(codigo_proveedor=f"REC-{n:05d}").first():
        n += 1
    return f"REC-{n:05d}"


def generar_rfc_proveedor_recetario():
    n = 1
    while Proveedor.query.filter_by(rfc=f"REC{n:010d}").first():
        n += 1
    return f"REC{n:010d}"


def proveedor_por_nombre_o_crear(nombre):
    normal = normalizar_texto(nombre)
    for proveedor in Proveedor.query.all():
        if normalizar_texto(proveedor.razon_social) == normal:
            return proveedor, False
    proveedor = Proveedor(
        codigo_proveedor=generar_codigo_proveedor_recetario(),
        razon_social=nombre.strip(),
        rfc=generar_rfc_proveedor_recetario(),
        tipo_proveedor="Materia Prima",
        estatus="Pendiente",
        calle_numero="Por confirmar",
        colonia="Por confirmar",
        ciudad="Cancún",
        estado="Quintana Roo",
        codigo_postal="00000",
        condicion_pago="Por confirmar",
        tiempo_entrega_normal=Decimal("0"),
    )
    db.session.add(proveedor)
    db.session.flush()
    db.session.add(
        Bitacora(
            id_proveedor=proveedor.id_proveedor,
            accion="Referencia de recetario",
            descripcion="Proveedor provisional creado desde referencias del recetario; datos fiscales y comerciales pendientes.",
        )
    )
    return proveedor, True


def sincronizar_proveedores_desde_recetario(RecetaIngrediente):
    creados = 0
    relaciones = 0
    ingredientes = RecetaIngrediente.query.filter(RecetaIngrediente.mp_id.isnot(None)).all()
    for ingrediente in ingredientes:
        mp = db.session.get(MateriaPrima, ingrediente.mp_id)
        if not mp:
            continue
        for nombre_proveedor in dividir_proveedores_recetario(ingrediente.proveedor_referencia):
            proveedor, creado = proveedor_por_nombre_o_crear(nombre_proveedor)
            creados += 1 if creado else 0
            existente = MateriaPrimaProveedor.query.filter_by(
                id_mp=mp.id_mp,
                id_proveedor=proveedor.id_proveedor,
                activo=True,
            ).first()
            if existente:
                continue
            tiene_principal = MateriaPrimaProveedor.query.filter_by(id_mp=mp.id_mp, activo=True, principal=True).first()
            db.session.add(
                MateriaPrimaProveedor(
                    id_mp=mp.id_mp,
                    id_proveedor=proveedor.id_proveedor,
                    principal=tiene_principal is None,
                    presentacion_compra="Referencia recetario",
                    contenido_presentacion=Decimal("1"),
                    unidad_contenido=mp.unidad_base,
                    factor_a_unidad_base=Decimal("1"),
                    precio_vigente=Decimal("0"),
                    tipo_precio="Por presentación",
                    moneda="MXN",
                    iva_tasa=Decimal("0"),
                    compra_minima=Decimal("1"),
                    unidad_compra_minima="Presentación",
                    lead_time_dias=0,
                    creado_por="Sincronización recetario",
                )
            )
            registrar_bitacora_mp(
                mp,
                "Proveedor referenciado en recetario",
                nuevo=f"{proveedor.razon_social} (pendiente de validar condiciones comerciales)",
            )
            relaciones += 1
    if creados or relaciones:
        db.session.commit()
    else:
        db.session.rollback()
    return {"proveedores_creados": creados, "relaciones_creadas": relaciones}


def proveedor_principal_vigente(mp):
    return (
        MateriaPrimaProveedor.query.filter_by(id_mp=mp.id_mp, activo=True, principal=True)
        .order_by(MateriaPrimaProveedor.vigencia_desde.desc(), MateriaPrimaProveedor.id_relacion.desc())
        .first()
    )


def ubicacion_principal_vigente(mp):
    return (
        MateriaPrimaUbicacion.query.filter_by(id_mp=mp.id_mp, activo=True, principal=True)
        .order_by(MateriaPrimaUbicacion.id_ubicacion_mp.desc())
        .first()
    )


def validar_activacion(mp):
    errores = []
    cfg = mp.inventario_config
    ubi = ubicacion_principal_vigente(mp)

    if not mp.nombre_oficial:
        errores.append("Nombre oficial")
    if not mp.familia:
        errores.append("Familia")
    if not mp.estado_fisico:
        errores.append("Estado físico")
    if not mp.unidad_base:
        errores.append("Unidad base")
    if mp.decimales_inventario is None or not 0 <= mp.decimales_inventario <= 6:
        errores.append("Decimales de inventario (0 a 6)")
    if not cfg:
        errores.append("Configuración de inventario")
    else:
        valores = [cfg.stock_minimo, cfg.punto_reorden, cfg.stock_objetivo_maximo]
        if any(v is None for v in valores):
            errores.append("Stock mínimo, punto de reorden y stock objetivo")
        elif not (cfg.stock_objetivo_maximo >= cfg.punto_reorden >= cfg.stock_minimo >= 0):
            errores.append("Coherencia: stock objetivo ≥ punto de reorden ≥ stock mínimo ≥ 0")
        if cfg.lead_time_reabasto_dias is None or cfg.lead_time_reabasto_dias < 0:
            errores.append("Lead time de reabasto")
        if not cfg.metodo_salida:
            errores.append("Método de salida")
    if not ubi:
        errores.append("Almacén y ubicación principal")
    if mp.se_compra_externamente:
        prov = MateriaPrimaProveedor.query.filter_by(id_mp=mp.id_mp, activo=True).first()
        principal = MateriaPrimaProveedor.query.filter_by(id_mp=mp.id_mp, activo=True, principal=True).first()
        if not prov:
            errores.append("Al menos un proveedor activo")
        elif not principal:
            errores.append("Un proveedor principal activo")
    if mp.factor_conversion_manual is not None and mp.factor_conversion_manual <= 0:
        errores.append("Factor de conversión manual mayor que cero")
    if mp.temperatura_min_c is not None and mp.temperatura_max_c is not None:
        if mp.temperatura_min_c > mp.temperatura_max_c:
            errores.append("Temperatura mínima menor o igual a temperatura máxima")
    return errores


def serializar_mp_resumen(mp):
    cfg = mp.inventario_config
    prov = proveedor_principal_vigente(mp)
    ubi = ubicacion_principal_vigente(mp)
    costo_unidad = None
    if prov and prov.factor_a_unidad_base and prov.factor_a_unidad_base > 0:
        if prov.tipo_precio == "Por presentación":
            costo_unidad = float(prov.precio_vigente / prov.factor_a_unidad_base)
        elif prov.tipo_precio in {"Por kg", "Por L", "Por pieza"}:
            costo_unidad = float(prov.precio_vigente)

    return {
        "id_mp": mp.id_mp,
        "id_materia_prima": mp.id_mp,  # compatibilidad con la pantalla existente de proveedores
        "sku_mp": mp.sku_mp,
        "nombre_oficial": mp.nombre_oficial,
        "nombre": mp.nombre_oficial,
        "familia": mp.familia,
        "estado_fisico": mp.estado_fisico,
        "unidad_base": mp.unidad_base,
        "stock_actual": None,
        "stock_minimo": float(cfg.stock_minimo) if cfg else None,
        "punto_reorden": float(cfg.punto_reorden) if cfg else None,
        "stock_objetivo_maximo": float(cfg.stock_objetivo_maximo) if cfg else None,
        "proveedor_principal": prov.proveedor.razon_social if prov and prov.proveedor else None,
        "ubicacion_principal": f"{ubi.almacen} / {ubi.ubicacion}" if ubi else None,
        "costo_unidad_base": costo_unidad,
        "estatus": mp.estatus,
        "activo": mp.activo,
        "borrador": mp.borrador,
    }


def serializar_mp_detalle(mp):
    cfg = mp.inventario_config
    ubicaciones = [
        {
            "id": u.id_ubicacion_mp,
            "almacen": u.almacen,
            "ubicacion": u.ubicacion,
            "principal": u.principal,
            "activo": u.activo,
        }
        for u in mp.ubicaciones
        if u.activo
    ]
    return {
        **serializar_mp_resumen(mp),
        "nombre_corto": mp.nombre_corto,
        "descripcion": mp.descripcion,
        "subfamilia": mp.subfamilia,
        "grado_concentracion": mp.grado_concentracion,
        "cas": mp.cas,
        "unidad_preferida_receta": mp.unidad_preferida_receta,
        "decimales_inventario": mp.decimales_inventario,
        "densidad_kg_l": float(mp.densidad_kg_l) if mp.densidad_kg_l is not None else None,
        "fuente_densidad": mp.fuente_densidad,
        "fecha_densidad": mp.fecha_densidad.isoformat() if mp.fecha_densidad else None,
        "factor_conversion_manual": float(mp.factor_conversion_manual) if mp.factor_conversion_manual is not None else None,
        "permite_fraccion": mp.permite_fraccion,
        "condicion_almacenamiento": mp.condicion_almacenamiento,
        "temperatura_min_c": float(mp.temperatura_min_c) if mp.temperatura_min_c is not None else None,
        "temperatura_max_c": float(mp.temperatura_max_c) if mp.temperatura_max_c is not None else None,
        "incompatibilidades": mp.incompatibilidades,
        "requiere_area_especial": mp.requiere_area_especial,
        "fabricante_marca_referencia": mp.fabricante_marca_referencia,
        "pureza_especificacion": mp.pureza_especificacion,
        "ph_referencia": mp.ph_referencia,
        "peligroso": mp.peligroso,
        "clasificacion_riesgo": mp.clasificacion_riesgo,
        "criterio_recepcion": mp.criterio_recepcion,
        "requiere_coa": mp.requiere_coa,
        "observaciones_tecnicas": mp.observaciones_tecnicas,
        "se_compra_externamente": mp.se_compra_externamente,
        "inventario": {
            "stock_minimo": float(cfg.stock_minimo) if cfg else 0,
            "stock_seguridad": float(cfg.stock_seguridad) if cfg and cfg.stock_seguridad is not None else None,
            "punto_reorden": float(cfg.punto_reorden) if cfg else 0,
            "stock_objetivo_maximo": float(cfg.stock_objetivo_maximo) if cfg else 0,
            "lead_time_reabasto_dias": cfg.lead_time_reabasto_dias if cfg else 0,
            "control_lote": cfg.control_lote if cfg else False,
            "control_caducidad": cfg.control_caducidad if cfg else False,
            "permite_stock_negativo": cfg.permite_stock_negativo if cfg else False,
            "metodo_salida": cfg.metodo_salida if cfg else "FIFO",
        },
        "ubicaciones": ubicaciones,
        "fecha_creacion": mp.fecha_creacion.isoformat(timespec="seconds"),
        "fecha_actualizacion": mp.fecha_actualizacion.isoformat(timespec="seconds"),
    }


def aplicar_datos_mp(mp, data, es_nuevo=False):
    """Aplica campos generales y configuración. Devuelve lista de errores."""
    errores = []

    if "nombre_oficial" in data or es_nuevo:
        nombre = (data.get("nombre_oficial") or "").strip()
        if not nombre:
            errores.append("El nombre oficial es obligatorio.")
        else:
            normalizado = normalizar_texto(nombre)
            existente = MateriaPrima.query.filter_by(nombre_normalizado=normalizado).first()
            if existente and existente.id_mp != mp.id_mp:
                errores.append("Ya existe una materia prima con ese nombre oficial.")
            alias_existente = MateriaPrimaAlias.query.filter_by(alias_normalizado=normalizado, activo=True).first()
            if alias_existente and alias_existente.id_mp != mp.id_mp:
                errores.append("Ese nombre ya está registrado como alias de otra materia prima.")
            if not errores:
                mp.nombre_oficial = nombre
                mp.nombre_normalizado = normalizado

    campos_texto = {
        "nombre_corto": 60,
        "descripcion": 500,
        "familia": 100,
        "subfamilia": 100,
        "estado_fisico": 30,
        "grado_concentracion": 100,
        "cas": 80,
        "unidad_base": 20,
        "unidad_preferida_receta": 20,
        "fuente_densidad": 180,
        "condicion_almacenamiento": 250,
        "incompatibilidades": 500,
        "fabricante_marca_referencia": 150,
        "pureza_especificacion": 100,
        "ph_referencia": 50,
        "clasificacion_riesgo": 150,
        "criterio_recepcion": 500,
        "observaciones_tecnicas": None,
    }
    for campo, limite in campos_texto.items():
        if campo in data:
            valor = data.get(campo)
            if isinstance(valor, str):
                valor = valor.strip() or None
                if limite and valor and len(valor) > limite:
                    errores.append(f"{campo} excede {limite} caracteres.")
            setattr(mp, campo, valor)

    if "decimales_inventario" in data:
        try:
            dec = int(data.get("decimales_inventario"))
            if dec < 0 or dec > 6:
                raise ValueError
            mp.decimales_inventario = dec
        except (TypeError, ValueError):
            errores.append("Los decimales de inventario deben estar entre 0 y 6.")

    for campo in ["densidad_kg_l", "factor_conversion_manual", "temperatura_min_c", "temperatura_max_c"]:
        if campo in data:
            val = decimal_o_none(data.get(campo))
            if data.get(campo) not in (None, "") and val is None:
                errores.append(f"Valor inválido en {campo}.")
            else:
                setattr(mp, campo, val)

    if mp.densidad_kg_l is not None and mp.densidad_kg_l <= 0:
        errores.append("La densidad debe ser mayor que cero.")
    if mp.factor_conversion_manual is not None and mp.factor_conversion_manual <= 0:
        errores.append("El factor de conversión manual debe ser mayor que cero.")

    if "fecha_densidad" in data:
        mp.fecha_densidad = fecha_o_none(data.get("fecha_densidad"))

    for campo in [
        "permite_fraccion",
        "requiere_area_especial",
        "peligroso",
        "requiere_coa",
        "se_compra_externamente",
    ]:
        if campo in data:
            setattr(mp, campo, bool_value(data.get(campo)))

    inv = data.get("inventario") or {}
    if inv or es_nuevo:
        cfg = mp.inventario_config
        if not cfg:
            cfg = MateriaPrimaInventarioConfig(id_mp=mp.id_mp if mp.id_mp else None)
            mp.inventario_config = cfg
        for campo in ["stock_minimo", "stock_seguridad", "punto_reorden", "stock_objetivo_maximo"]:
            if campo in inv:
                val = decimal_o_none(inv.get(campo))
                if inv.get(campo) not in (None, "") and val is None:
                    errores.append(f"Valor inválido en {campo}.")
                elif val is not None and val < 0:
                    errores.append(f"{campo} no puede ser negativo.")
                else:
                    setattr(cfg, campo, val)
        if "lead_time_reabasto_dias" in inv:
            try:
                lt = int(inv.get("lead_time_reabasto_dias"))
                if lt < 0:
                    raise ValueError
                cfg.lead_time_reabasto_dias = lt
            except (TypeError, ValueError):
                errores.append("El lead time debe ser un entero mayor o igual a cero.")
        for campo in ["control_lote", "control_caducidad", "permite_stock_negativo"]:
            if campo in inv:
                setattr(cfg, campo, bool_value(inv.get(campo)))
        if "metodo_salida" in inv:
            metodo = (inv.get("metodo_salida") or "").upper()
            if metodo not in {"FIFO", "FEFO"}:
                errores.append("El método de salida debe ser FIFO o FEFO.")
            else:
                cfg.metodo_salida = metodo
        if cfg.control_caducidad:
            cfg.metodo_salida = "FEFO"

    ubicacion = data.get("ubicacion_principal")
    if ubicacion:
        almacen = (ubicacion.get("almacen") or "").strip()
        posicion = (ubicacion.get("ubicacion") or "").strip()
        if almacen and posicion:
            actual = ubicacion_principal_vigente(mp) if mp.id_mp else None
            if actual and (actual.almacen != almacen or actual.ubicacion != posicion):
                actual.principal = False
                actual.activo = False
                actual.vigencia_hasta = date.today()
                nueva = MateriaPrimaUbicacion(almacen=almacen, ubicacion=posicion, principal=True, activo=True)
                mp.ubicaciones.append(nueva)
            elif not actual:
                mp.ubicaciones.append(MateriaPrimaUbicacion(almacen=almacen, ubicacion=posicion, principal=True, activo=True))
        elif almacen or posicion:
            errores.append("Almacén y ubicación principal deben capturarse juntos.")

    return errores


# ==========================================
# Rutas del Sistema y Seguridad
# ==========================================
@app.route("/", methods=["GET"])
def portal_publico():
    return render_template("portal_registro.html")


@app.route("/portal-registro", methods=["GET"])
def portal_publico_alias():
    return render_template("portal_registro.html")


@app.route("/login", methods=["GET", "POST"])
def login_admin():
    if request.method == "POST":
        usuario = request.form.get("usuario") or (request.json.get("usuario") if request.is_json else None)
        password = request.form.get("password") or (request.json.get("password") if request.is_json else None)

        admin_usuario = os.environ.get("NYDS_ADMIN_USER", "compras.nyds")
        admin_password = os.environ.get("NYDS_ADMIN_PASSWORD", "nyds2026")
        if usuario == admin_usuario and admin_password and secrets.compare_digest(admin_password, password or ""):
            session["admin_logueado"] = True
            session["usuario"] = usuario
            session.permanent = True
            if request.is_json:
                return jsonify({"exito": True, "redirect": "/admin/catalogo"})
            return redirect(url_for("vista_catalogo_admin"))

        if request.is_json:
            return jsonify({"error": "Credenciales incorrectas"}), 401
        return render_template("login.html", error="Credenciales incorrectas"), 401

    return render_template("login.html")


@app.route("/logout", methods=["GET"])
def logout_admin():
    session.clear()
    return redirect(url_for("login_admin"))


@app.route("/admin/catalogo", methods=["GET"])
def vista_catalogo_admin():
    if not session.get("admin_logueado"):
        return redirect(url_for("login_admin"))
    return render_template("catalogo.html")


@app.route("/admin/alta", methods=["GET"])
def vista_alta_admin():
    if not session.get("admin_logueado"):
        return redirect(url_for("login_admin"))
    return render_template("index.html")


@app.route("/admin/editar/<int:id_proveedor>", methods=["GET"])
def vista_editar_admin(id_proveedor):
    if not session.get("admin_logueado"):
        return redirect(url_for("login_admin"))
    proveedor = db.session.get(Proveedor, id_proveedor)
    if not proveedor:
        return "Proveedor no encontrado", 404
    return render_template("editar.html", id_proveedor=id_proveedor, codigo=proveedor.codigo_proveedor, razon=proveedor.razon_social)


@app.route("/admin/materias-primas", methods=["GET"])
def vista_catalogo_materias_primas():
    if not session.get("admin_logueado"):
        return redirect(url_for("login_admin"))
    return render_template("materias_primas_catalogo.html")


@app.route("/admin/materias-primas/nueva", methods=["GET"])
def vista_nueva_materia_prima():
    if not session.get("admin_logueado"):
        return redirect(url_for("login_admin"))
    return render_template("materia_prima_form.html", id_mp=None)


@app.route("/admin/materias-primas/<int:id_mp>", methods=["GET"])
def vista_editar_materia_prima(id_mp):
    if not session.get("admin_logueado"):
        return redirect(url_for("login_admin"))
    mp = db.session.get(MateriaPrima, id_mp)
    if not mp:
        return "Materia prima no encontrada", 404
    return render_template("materia_prima_form.html", id_mp=id_mp)


# ==========================================
# API de Verificación de Proveedores
# ==========================================
@app.route("/api/verificar-duplicado", methods=["POST"])
def verificar_duplicado():
    data = request.json or {}
    campo = data.get("campo")
    valor = data.get("valor", "").strip().lower()

    if not campo or not valor:
        return jsonify({"existe": False})

    es_admin = bool(session.get("admin_logueado"))
    # Portal público: el mensaje es intencionalmente genérico (RN-01 exige revelar
    # el registro encontrado únicamente en el flujo interno de Compras/Administración).
    mensaje_generico = "Ya se encuentra registrado en el sistema."

    def respuesta_duplicado(proveedor):
        if es_admin:
            return jsonify(
                {
                    "existe": True,
                    "mensaje": f"Ya existe un proveedor con el RFC {proveedor.rfc}. Abra el registro existente o verifique la información.",
                    "id_proveedor": proveedor.id_proveedor,
                    "codigo_proveedor": proveedor.codigo_proveedor,
                    "razon_social": proveedor.razon_social,
                }
            )
        return jsonify({"existe": True, "mensaje": mensaje_generico})

    if campo == "rfc":
        val_limpio = normalizar_rfc(valor)
        p = Proveedor.query.filter_by(rfc=val_limpio).first()
        if p:
            return respuesta_duplicado(p)

    elif campo == "razon_social":
        p = Proveedor.query.filter(func.lower(Proveedor.razon_social) == valor).first()
        if p:
            return respuesta_duplicado(p)

    elif campo == "domicilio":
        cp = data.get("codigo_postal", "").strip()
        p = Proveedor.query.filter(
            db.and_(func.lower(Proveedor.calle_numero) == valor, Proveedor.codigo_postal == cp)
        ).first()
        if p:
            return respuesta_duplicado(p)

    return jsonify({"existe": False})


# ==========================================
# APIs Generales de Proveedores
# ==========================================
def _construir_contactos(nuevo_proveedor, contactos_data):
    """Crea los contactos de un proveedor aplicando RN-09 (un solo principal activo)."""
    ya_hay_principal = False
    contactos_creados = []
    for c_data in contactos_data or []:
        if not (c_data.get("nombre") and c_data.get("correo")):
            continue
        es_principal_solicitado = bool_value(c_data.get("es_principal"))
        es_principal_final = es_principal_solicitado and not ya_hay_principal
        if es_principal_solicitado and ya_hay_principal:
            es_principal_final = False
        if es_principal_final:
            ya_hay_principal = True
        contacto = Contacto(
            id_proveedor=nuevo_proveedor.id_proveedor,
            nombre=c_data["nombre"].strip(),
            puesto_area=(c_data.get("puesto_area") or "").strip() or None,
            telefono=(c_data.get("telefono") or "").strip() or None,
            whatsapp=(c_data.get("whatsapp") or "").strip() or None,
            correo=c_data["correo"].strip(),
            tipo_contacto=c_data.get("tipo_contacto", "Principal"),
            es_principal=es_principal_final,
            activo=bool_value(c_data.get("activo"), default=True),
        )
        db.session.add(contacto)
        contactos_creados.append(contacto)
    return contactos_creados


@app.route("/api/proveedores", methods=["GET", "POST"])
def manejar_proveedores():
    if request.method == "GET":
        if not session.get("admin_logueado"):
            return jsonify({"error": "No autorizado"}), 403
        busqueda = request.args.get("q", "").strip()
        estatus_filtro = request.args.get("estatus", "").strip()
        query = Proveedor.query
        if busqueda:
            mp_ids_coincidentes = (
                db.session.query(MateriaPrimaProveedor.id_proveedor)
                .join(MateriaPrima, MateriaPrimaProveedor.id_mp == MateriaPrima.id_mp)
                .filter(MateriaPrima.nombre_oficial.ilike(f"%{busqueda}%"))
            )
            query = query.filter(
                db.or_(
                    Proveedor.razon_social.ilike(f"%{busqueda}%"),
                    Proveedor.nombre_comercial.ilike(f"%{busqueda}%"),
                    Proveedor.rfc.ilike(f"%{busqueda}%"),
                    Proveedor.codigo_proveedor.ilike(f"%{busqueda}%"),
                    Proveedor.id_proveedor.in_(mp_ids_coincidentes),
                )
            )
        if estatus_filtro:
            query = query.filter(Proveedor.estatus == estatus_filtro)
        proveedores = query.order_by(Proveedor.razon_social.asc()).all()
        lista = []
        for p in proveedores:
            contacto_prin = p.contacto_principal()
            lista.append(
                {
                    "id_proveedor": p.id_proveedor,
                    "codigo_proveedor": p.codigo_proveedor,
                    "razon_social": p.razon_social,
                    "nombre_comercial": p.nombre_comercial,
                    "rfc": p.rfc,
                    "tipo_proveedor": p.tipo_proveedor,
                    "estatus": p.estatus,
                    "domicilio": f"{p.calle_numero}, Col. {p.colonia}, {p.ciudad}, C.P. {p.codigo_postal}",
                    "contacto": contacto_prin.nombre if contacto_prin else "Sin contacto",
                    "telefono": (contacto_prin.telefono or contacto_prin.whatsapp) if contacto_prin else "N/A",
                    "materias_primas_relacionadas": len(p.materias_primas),
                }
            )
        return jsonify(lista), 200

    data = request.json or {}
    rfc_normalizado = normalizar_rfc(data.get("rfc", ""))

    if not rfc_normalizado or not data.get("razon_social") or not data.get("codigo_postal"):
        return jsonify({"error": "Complete los campos obligatorios marcados antes de guardar."}), 400

    if not rfc_valido(rfc_normalizado):
        return jsonify({"error": "El RFC no tiene un formato válido."}), 400

    existente = Proveedor.query.filter_by(rfc=rfc_normalizado).first()
    if existente:
        if session.get("admin_logueado"):
            return jsonify(
                {
                    "error": f"Ya existe un proveedor con el RFC {rfc_normalizado}. Abra el registro existente o verifique la información.",
                    "id_proveedor": existente.id_proveedor,
                }
            ), 400
        # Portal público: no revelar datos del proveedor existente.
        return jsonify({"error": "Ya se encuentra registrado en el sistema."}), 400

    if data.get("codigo_postal", "").strip() and len(data["codigo_postal"].strip()) != 5:
        return jsonify({"error": "El código postal fiscal debe tener 5 dígitos."}), 400

    estatus_solicitado = data.get("estatus_inicial") or data.get("estatus") or "Borrador"
    if estatus_solicitado not in ESTATUS_PROVEEDOR_VALIDOS:
        estatus_solicitado = "Borrador"
    tiempo_entrega = data.get("tiempo_entrega_normal", data.get("lead_time_habitual", 0))
    usuario = usuario_actual()

    nuevo_proveedor = Proveedor(
        codigo_proveedor=siguiente_codigo_proveedor(),
        razon_social=data["razon_social"].strip(),
        nombre_comercial=(data.get("nombre_comercial") or "").strip() or None,
        rfc=rfc_normalizado,
        tipo_proveedor=data.get("tipo_proveedor", "Materia prima"),
        estatus="Borrador",  # se activa más abajo solo si cumple los campos mínimos
        sitio_web=(data.get("sitio_web") or "").strip() or None,
        notas_generales=data.get("notas_generales"),
        calle_numero=data.get("calle_numero") or "S/N",
        colonia=data.get("colonia") or "Centro",
        ciudad=data.get("ciudad") or "Cancún",
        estado=data.get("estado") or "Quintana Roo",
        codigo_postal=data["codigo_postal"].strip(),
        regimen_fiscal=data.get("regimen_fiscal"),
        correo_facturacion=data.get("correo_facturacion"),
        forma_pago_habitual=data.get("forma_pago_habitual"),
        metodo_pago_habitual=data.get("metodo_pago_habitual"),
        banco=data.get("banco"),
        beneficiario=data.get("beneficiario"),
        cuenta_bancaria=data.get("cuenta_bancaria"),
        clabe=data.get("clabe"),
        condicion_pago=data.get("condicion_pago") or "Contado",
        dias_credito=int(data.get("dias_credito") or 0),
        tiempo_entrega_normal=decimal_o_none(tiempo_entrega) or Decimal("0"),
        lead_time_maximo=decimal_o_none(data.get("lead_time_maximo")),
        politica_flete=data.get("politica_flete"),
        zona_entrega=data.get("zona_entrega"),
        horario_atencion=data.get("horario_atencion"),
        compra_minima_general=decimal_o_none(data.get("compra_minima_general")),
        moneda_compra_minima=data.get("moneda_compra_minima"),
        vigencia_cotizacion_dias=int(data["vigencia_cotizacion_dias"]) if data.get("vigencia_cotizacion_dias") else None,
        creado_por=usuario,
        actualizado_por=usuario,
    )
    db.session.add(nuevo_proveedor)
    db.session.flush()

    _construir_contactos(nuevo_proveedor, data.get("contactos", []))

    db.session.add(
        Bitacora(
            id_proveedor=nuevo_proveedor.id_proveedor,
            accion="Alta",
            descripcion="Registro inicial de proveedor (Borrador)",
            usuario=usuario,
        )
    )

    mensaje_extra = ""
    if estatus_solicitado == "Activo":
        faltantes = nuevo_proveedor.campos_minimos_faltantes()
        if faltantes:
            mensaje_extra = " Quedó en Borrador: faltan " + ", ".join(faltantes) + "."
        else:
            nuevo_proveedor.estatus = "Activo"
            db.session.add(
                Bitacora(
                    id_proveedor=nuevo_proveedor.id_proveedor,
                    accion="Activación",
                    descripcion="Proveedor activado en la misma alta",
                    usuario=usuario,
                )
            )

    db.session.commit()
    return jsonify(
        {
            "mensaje": "Registro de proveedor exitoso." + mensaje_extra,
            "codigo": nuevo_proveedor.codigo_proveedor,
            "id_proveedor": nuevo_proveedor.id_proveedor,
            "estatus": nuevo_proveedor.estatus,
        }
    ), 201


@app.route("/api/proveedores/activos", methods=["GET"])
@admin_required_json
def proveedores_activos():
    proveedores = Proveedor.query.filter_by(estatus="Activo").order_by(Proveedor.razon_social.asc()).all()
    return jsonify(
        [
            {
                "id_proveedor": p.id_proveedor,
                "codigo_proveedor": p.codigo_proveedor,
                "razon_social": p.razon_social,
                "tiempo_entrega_normal": float(p.tiempo_entrega_normal or 0),
            }
            for p in proveedores
        ]
    )


@app.route("/api/proveedores/<int:id_proveedor>/estatus", methods=["PATCH"])
@admin_required_json
def cambiar_estatus_proveedor(id_proveedor):
    proveedor = db.session.get(Proveedor, id_proveedor)
    if not proveedor:
        return jsonify({"error": "Proveedor no encontrado"}), 404
    data = request.json or {}
    nuevo_estatus = data.get("estatus")
    motivo = (data.get("motivo") or "").strip()

    if not nuevo_estatus:
        nuevo_estatus = "Bloqueado" if proveedor.estatus == "Activo" else "Activo"

    if nuevo_estatus not in ESTATUS_PROVEEDOR_VALIDOS:
        return jsonify({"error": "Estatus no reconocido."}), 400

    if nuevo_estatus == "Bloqueado":
        if not motivo:
            return jsonify({"error": "Bloquear un proveedor requiere un motivo."}), 400
        ordenes_abiertas = OrdenCompra.query.filter(
            OrdenCompra.proveedor_id == id_proveedor, OrdenCompra.estado.in_(ESTADOS_OC_ABIERTOS)
        ).count()
        if ordenes_abiertas:
            return jsonify(
                {"error": "Este proveedor tiene órdenes de compra abiertas. Revise las órdenes antes de bloquearlo."}
            ), 400
        proveedor.motivo_bloqueo = motivo
    elif nuevo_estatus == "Activo":
        # Reactivación o activación de un borrador: exige los campos mínimos (sección 12).
        faltantes = proveedor.campos_minimos_faltantes()
        if faltantes:
            return jsonify(
                {"error": "Complete los campos obligatorios marcados antes de guardar.", "faltantes": faltantes}
            ), 400
        proveedor.motivo_bloqueo = None

    usuario = usuario_actual()
    estatus_anterior = proveedor.estatus
    proveedor.estatus = nuevo_estatus
    proveedor.actualizado_por = usuario
    db.session.add(
        Bitacora(
            id_proveedor=id_proveedor,
            accion="Cambio de Estatus",
            descripcion=f"Estatus actualizado de {estatus_anterior} a {nuevo_estatus}" + (f" — motivo: {motivo}" if motivo else ""),
            usuario=usuario,
        )
    )
    db.session.commit()
    return jsonify({"mensaje": f"Estatus actualizado a {nuevo_estatus}", "nuevo_estatus": proveedor.estatus}), 200


@app.route("/api/proveedores/<int:id_proveedor>/evaluaciones", methods=["GET", "POST"])
@admin_required_json
def evaluaciones_proveedor(id_proveedor):
    proveedor = db.session.get(Proveedor, id_proveedor)
    if not proveedor:
        return jsonify({"error": "Proveedor no encontrado"}), 404

    if request.method == "GET":
        evaluaciones = (
            ProveedorEvaluacion.query.filter_by(id_proveedor=id_proveedor)
            .order_by(ProveedorEvaluacion.fecha.desc())
            .all()
        )
        return jsonify(
            [
                {
                    "id_evaluacion": e.id_evaluacion,
                    "periodo": e.periodo,
                    "calif_precio": float(e.calif_precio),
                    "calif_calidad": float(e.calif_calidad),
                    "calif_entrega": float(e.calif_entrega),
                    "calif_atencion": float(e.calif_atencion),
                    "calificacion_final": float(e.calificacion_final),
                    "comentarios": e.comentarios,
                    "usuario": e.usuario,
                    "fecha": e.fecha.strftime("%d/%m/%Y %H:%M"),
                }
                for e in evaluaciones
            ]
        )

    data = request.json or {}
    calif_precio = decimal_o_none(data.get("calif_precio"))
    calif_calidad = decimal_o_none(data.get("calif_calidad"))
    calif_entrega = decimal_o_none(data.get("calif_entrega"))
    calif_atencion = decimal_o_none(data.get("calif_atencion"))

    for calif in (calif_precio, calif_calidad, calif_entrega, calif_atencion):
        if calif is None or calif < 1 or calif > 5:
            return jsonify({"error": "Cada calificación debe estar entre 1 y 5."}), 400

    evaluacion = ProveedorEvaluacion(
        id_proveedor=id_proveedor,
        periodo=(data.get("periodo") or "").strip() or datetime.utcnow().strftime("%Y-%m"),
        calif_precio=calif_precio,
        calif_calidad=calif_calidad,
        calif_entrega=calif_entrega,
        calif_atencion=calif_atencion,
        ponderacion_precio=decimal_o_none(data.get("ponderacion_precio")) or Decimal("25"),
        ponderacion_calidad=decimal_o_none(data.get("ponderacion_calidad")) or Decimal("30"),
        ponderacion_entrega=decimal_o_none(data.get("ponderacion_entrega")) or Decimal("30"),
        ponderacion_atencion=decimal_o_none(data.get("ponderacion_atencion")) or Decimal("15"),
        comentarios=data.get("comentarios"),
        usuario=usuario_actual(),
    )
    db.session.add(evaluacion)
    db.session.add(
        Bitacora(
            id_proveedor=id_proveedor,
            accion="Evaluación",
            descripcion=f"Nueva evaluación del periodo {evaluacion.periodo}: {evaluacion.calificacion_final}",
            usuario=usuario_actual(),
        )
    )
    db.session.commit()
    return jsonify({"mensaje": "Evaluación registrada", "calificacion_final": float(evaluacion.calificacion_final)}), 201


@app.route("/api/proveedores/<int:id_proveedor>/documentos", methods=["GET", "POST"])
@admin_required_json
def documentos_proveedor(id_proveedor):
    proveedor = db.session.get(Proveedor, id_proveedor)
    if not proveedor:
        return jsonify({"error": "Proveedor no encontrado"}), 404

    if request.method == "GET":
        docs = (
            Documento.query.filter_by(id_proveedor=id_proveedor, activo=True)
            .order_by(Documento.fecha_carga.desc())
            .all()
        )
        return jsonify(
            [
                {
                    "id_documento": d.id_documento,
                    "tipo_documento": d.tipo_documento,
                    "nombre_original": d.nombre_original,
                    "tamano_bytes": d.tamano_bytes,
                    "usuario": d.usuario,
                    "version": d.version,
                    "vigencia_desde": d.vigencia_desde.isoformat() if d.vigencia_desde else None,
                    "vigencia_hasta": d.vigencia_hasta.isoformat() if d.vigencia_hasta else None,
                    "fecha": d.fecha_carga.strftime("%d/%m/%Y %H:%M"),
                    "url_descarga": url_for("descargar_documento_proveedor", id_documento=d.id_documento),
                }
                for d in docs
            ]
        )

    archivo = request.files.get("archivo")
    tipo = (request.form.get("tipo_documento") or "Otro").strip()
    if not archivo or not archivo.filename:
        return jsonify({"error": "Archivo requerido"}), 400
    if not allowed_file(archivo.filename):
        return jsonify({"error": "Tipo de archivo no permitido"}), 400

    nombre_original = archivo.filename
    ext = nombre_original.rsplit(".", 1)[1].lower()
    nombre_guardado = secure_filename(f"prov_{id_proveedor}_{int(datetime.utcnow().timestamp())}_{tipo}.{ext}")
    ruta_guardado = os.path.join(UPLOAD_FOLDER, nombre_guardado)
    archivo.save(ruta_guardado)
    tamano_bytes = os.path.getsize(ruta_guardado)
    usuario = usuario_actual()

    # Reemplazar por nueva versión sin perder la anterior: el documento previo del
    # mismo tipo queda inactivo (histórico), nunca se borra.
    version_nueva = 1
    anterior_activo = (
        Documento.query.filter_by(id_proveedor=id_proveedor, tipo_documento=tipo, activo=True)
        .order_by(Documento.version.desc())
        .first()
    )
    if anterior_activo:
        version_nueva = (anterior_activo.version or 1) + 1
        anterior_activo.activo = False

    doc = Documento(
        id_proveedor=id_proveedor,
        tipo_documento=tipo,
        nombre_original=nombre_original,
        nombre_archivo_guardado=nombre_guardado,
        tamano_bytes=tamano_bytes,
        usuario=usuario,
        vigencia_desde=fecha_o_none(request.form.get("vigencia_desde")),
        vigencia_hasta=fecha_o_none(request.form.get("vigencia_hasta")),
        version=version_nueva,
    )
    db.session.add(doc)
    db.session.add(Bitacora(id_proveedor=id_proveedor, accion="Documento", descripcion=f"Carga: {tipo} (v{version_nueva})", usuario=usuario))
    db.session.commit()
    return jsonify({"mensaje": "Documento cargado", "version": version_nueva}), 201


@app.route("/api/proveedores/documentos/<int:id_documento>/descargar", methods=["GET"])
def descargar_documento_proveedor(id_documento):
    if not session.get("admin_logueado"):
        return redirect(url_for("login_admin"))
    doc = db.session.get(Documento, id_documento)
    if not doc:
        return "Documento no encontrado", 404
    return send_from_directory(UPLOAD_FOLDER, doc.nombre_archivo_guardado, as_attachment=True, download_name=doc.nombre_original)


# ==========================================
# API: Materias Primas
# ==========================================
@app.route("/api/materias-primas/verificar-duplicado", methods=["POST"])
@admin_required_json
def verificar_duplicado_mp():
    data = request.json or {}
    valor = (data.get("valor") or "").strip()
    id_actual = data.get("id_mp")
    if not valor:
        return jsonify({"existe": False, "coincidencias": []})

    n = normalizar_texto(valor)
    coincidencias = []
    mp = MateriaPrima.query.filter_by(nombre_normalizado=n).first()
    if mp and (not id_actual or mp.id_mp != int(id_actual)):
        coincidencias.append({"id_mp": mp.id_mp, "sku_mp": mp.sku_mp, "nombre_oficial": mp.nombre_oficial, "tipo": "Nombre oficial"})

    alias = MateriaPrimaAlias.query.filter_by(alias_normalizado=n, activo=True).first()
    if alias and (not id_actual or alias.id_mp != int(id_actual)):
        coincidencias.append(
            {
                "id_mp": alias.id_mp,
                "sku_mp": alias.materia_prima.sku_mp,
                "nombre_oficial": alias.materia_prima.nombre_oficial,
                "tipo": "Alias",
            }
        )

    return jsonify({"existe": bool(coincidencias), "coincidencias": coincidencias})


@app.route("/api/materias-primas", methods=["GET", "POST"])
@admin_required_json
def materias_primas():
    if request.method == "GET":
        q = request.args.get("q", "").strip()
        familia = request.args.get("familia", "").strip()
        estatus = request.args.get("estatus", "").strip()
        proveedor_id = request.args.get("proveedor_id", type=int)
        debajo_minimo = request.args.get("debajo_minimo") == "1"
        debajo_reorden = request.args.get("debajo_reorden") == "1"

        query = MateriaPrima.query
        if q:
            nq = f"%{normalizar_texto(q)}%"
            alias_ids = db.session.query(MateriaPrimaAlias.id_mp).filter(MateriaPrimaAlias.alias_normalizado.ilike(nq)).subquery()
            prov_ids = (
                db.session.query(MateriaPrimaProveedor.id_mp)
                .filter(MateriaPrimaProveedor.sku_proveedor.ilike(f"%{q}%"))
                .subquery()
            )
            query = query.filter(
                db.or_(
                    MateriaPrima.sku_mp.ilike(f"%{q}%"),
                    MateriaPrima.nombre_normalizado.ilike(nq),
                    MateriaPrima.cas.ilike(f"%{q}%"),
                    MateriaPrima.id_mp.in_(alias_ids),
                    MateriaPrima.id_mp.in_(prov_ids),
                )
            )
        if familia:
            query = query.filter(MateriaPrima.familia == familia)
        if estatus == "Activo":
            query = query.filter(MateriaPrima.activo.is_(True))
        elif estatus == "Borrador":
            query = query.filter(MateriaPrima.activo.is_(False), MateriaPrima.borrador.is_(True))
        elif estatus == "Inactivo":
            query = query.filter(MateriaPrima.activo.is_(False), MateriaPrima.borrador.is_(False))
        if proveedor_id:
            ids = db.session.query(MateriaPrimaProveedor.id_mp).filter_by(id_proveedor=proveedor_id, activo=True).subquery()
            query = query.filter(MateriaPrima.id_mp.in_(ids))

        lista = [serializar_mp_resumen(mp) for mp in query.order_by(MateriaPrima.nombre_oficial.asc()).all()]
        # Stock actual todavía no existe; estos filtros quedan preparados sin devolver falsos positivos.
        if debajo_minimo or debajo_reorden:
            lista = []
        return jsonify(lista)

    data = request.json or {}
    activar = bool_value(data.get("activar"), False)

    mp = MateriaPrima(
        sku_mp=generar_sku_mp(),
        nombre_oficial="TEMP",
        nombre_normalizado=f"TEMP-{datetime.utcnow().timestamp()}",
        familia=(data.get("familia") or "").strip(),
        estado_fisico=(data.get("estado_fisico") or "").strip(),
        unidad_base=(data.get("unidad_base") or "").strip(),
        creado_por=usuario_actual(),
        activo=False,
        borrador=True,
    )
    db.session.add(mp)
    db.session.flush()

    errores = aplicar_datos_mp(mp, data, es_nuevo=True)
    if errores:
        db.session.rollback()
        return jsonify({"error": "No se pudo guardar la materia prima.", "detalles": errores}), 400

    condiciones = data.get("proveedores", [])
    if not isinstance(condiciones, list) or any(not isinstance(c, dict) for c in condiciones):
        db.session.rollback()
        return jsonify({"error": "Proveedores debe ser una lista de condiciones"}), 400
    ids = [str(c.get("id_proveedor")) for c in condiciones]
    if len(ids) != len(set(ids)) or sum(bool_value(c.get("principal")) for c in condiciones) > 1:
        db.session.rollback()
        return jsonify({"error": "No repitas proveedores y selecciona como máximo un principal"}), 400
    for condicion in condiciones:
        respuesta, estado = registrar_condicion_mp(mp, condicion)
        if estado >= 400:
            db.session.rollback()
            return respuesta, estado

    registrar_bitacora_mp(mp, "Alta", nuevo=f"{mp.sku_mp} - {mp.nombre_oficial}")

    if activar:
        errores_activacion = validar_activacion(mp)
        if errores_activacion:
            db.session.rollback()
            return jsonify({"error": "La materia prima no puede activarse todavía.", "detalles": errores_activacion}), 400
        mp.activo = True
        mp.borrador = False
        registrar_bitacora_mp(mp, "Activación", anterior="Borrador", nuevo="Activo")

    db.session.commit()
    return jsonify({"mensaje": "Materia prima guardada", "id_mp": mp.id_mp, "sku_mp": mp.sku_mp, "estatus": mp.estatus}), 201


@app.route("/api/materias-primas/<int:id_mp>", methods=["GET", "PUT"])
@admin_required_json
def materia_prima_detalle(id_mp):
    mp = db.session.get(MateriaPrima, id_mp)
    if not mp:
        return jsonify({"error": "Materia prima no encontrada"}), 404

    if request.method == "GET":
        return jsonify(serializar_mp_detalle(mp))

    data = request.json or {}
    anterior = serializar_mp_detalle(mp)
    errores = aplicar_datos_mp(mp, data)
    if errores:
        db.session.rollback()
        return jsonify({"error": "No se pudieron guardar los cambios.", "detalles": errores}), 400

    # Si ya está activa, no permitir que una edición la deje incoherente.
    if mp.activo:
        errores_activacion = validar_activacion(mp)
        if errores_activacion:
            db.session.rollback()
            return jsonify({"error": "Los cambios dejarían una materia prima activa con configuración incompleta.", "detalles": errores_activacion}), 400

    registrar_bitacora_mp(mp, "Edición", anterior="Ficha actualizada", nuevo="Ficha actualizada", motivo=data.get("motivo"))
    db.session.commit()
    return jsonify({"mensaje": "Cambios guardados", "materia_prima": serializar_mp_detalle(mp)})


@app.route("/api/materias-primas/<int:id_mp>/estatus", methods=["PATCH"])
@admin_required_json
def cambiar_estatus_mp(id_mp):
    mp = db.session.get(MateriaPrima, id_mp)
    if not mp:
        return jsonify({"error": "Materia prima no encontrada"}), 404
    data = request.json or {}
    nuevo = (data.get("estatus") or "").strip()
    motivo = (data.get("motivo") or "").strip() or None
    anterior = mp.estatus

    if nuevo == "Activo":
        errores = validar_activacion(mp)
        if errores:
            return jsonify({"error": "La materia prima no puede activarse.", "detalles": errores}), 400
        mp.activo = True
        mp.borrador = False
    elif nuevo == "Borrador":
        mp.activo = False
        mp.borrador = True
    elif nuevo in {"Inactivo", "Bloqueado"}:
        mp.activo = False
        mp.borrador = False
    else:
        return jsonify({"error": "Estatus inválido"}), 400

    registrar_bitacora_mp(mp, "Cambio de estatus", anterior=anterior, nuevo=mp.estatus, motivo=motivo)
    db.session.commit()
    return jsonify({"mensaje": f"Estatus actualizado a {mp.estatus}", "estatus": mp.estatus})


@app.route("/api/materias-primas/<int:id_mp>/aliases", methods=["GET", "POST"])
@admin_required_json
def aliases_mp(id_mp):
    mp = db.session.get(MateriaPrima, id_mp)
    if not mp:
        return jsonify({"error": "Materia prima no encontrada"}), 404

    if request.method == "GET":
        return jsonify(
            [
                {
                    "id_alias": a.id_alias,
                    "alias": a.alias,
                    "tipo_alias": a.tipo_alias,
                    "observacion": a.observacion,
                    "activo": a.activo,
                }
                for a in MateriaPrimaAlias.query.filter_by(id_mp=id_mp, activo=True).order_by(MateriaPrimaAlias.alias.asc()).all()
            ]
        )

    data = request.json or {}
    alias_txt = (data.get("alias") or "").strip()
    if not alias_txt:
        return jsonify({"error": "El alias es obligatorio"}), 400
    normalizado = normalizar_texto(alias_txt)
    if normalizado == mp.nombre_normalizado:
        return jsonify({"error": "El alias coincide con el nombre oficial"}), 400
    otro_mp = MateriaPrima.query.filter_by(nombre_normalizado=normalizado).first()
    otro_alias = MateriaPrimaAlias.query.filter_by(alias_normalizado=normalizado, activo=True).first()
    if otro_mp or otro_alias:
        return jsonify({"error": "Ese nombre o alias ya se encuentra registrado"}), 400

    alias = MateriaPrimaAlias(
        id_mp=id_mp,
        alias=alias_txt,
        alias_normalizado=normalizado,
        tipo_alias=(data.get("tipo_alias") or "Sinónimo").strip(),
        observacion=(data.get("observacion") or "").strip() or None,
        activo=True,
    )
    db.session.add(alias)
    registrar_bitacora_mp(mp, "Alias agregado", nuevo=alias_txt)
    db.session.commit()
    return jsonify({"mensaje": "Alias agregado", "id_alias": alias.id_alias}), 201


@app.route("/api/materias-primas/<int:id_mp>/aliases/<int:id_alias>", methods=["DELETE"])
@admin_required_json
def desactivar_alias_mp(id_mp, id_alias):
    alias = MateriaPrimaAlias.query.filter_by(id_alias=id_alias, id_mp=id_mp).first()
    mp = db.session.get(MateriaPrima, id_mp)
    if not alias or not mp:
        return jsonify({"error": "Alias no encontrado"}), 404
    alias.activo = False
    registrar_bitacora_mp(mp, "Alias desactivado", anterior=alias.alias)
    db.session.commit()
    return jsonify({"mensaje": "Alias desactivado"})


@app.route("/api/materias-primas/<int:id_mp>/proveedores", methods=["GET", "POST"])
@admin_required_json
def proveedores_mp(id_mp):
    mp = db.session.get(MateriaPrima, id_mp)
    if not mp:
        return jsonify({"error": "Materia prima no encontrada"}), 404

    if request.method == "GET":
        relaciones = (
            MateriaPrimaProveedor.query.filter_by(id_mp=id_mp)
            .order_by(MateriaPrimaProveedor.activo.desc(), MateriaPrimaProveedor.principal.desc(), MateriaPrimaProveedor.vigencia_desde.desc())
            .all()
        )
        return jsonify(
            [
                {
                    "id_relacion": r.id_relacion,
                    "id_proveedor": r.id_proveedor,
                    "proveedor": r.proveedor.razon_social if r.proveedor else "Proveedor eliminado",
                    "codigo_proveedor": r.proveedor.codigo_proveedor if r.proveedor else "",
                    "principal": r.principal,
                    "sku_proveedor": r.sku_proveedor,
                    "marca_fabricante": r.marca_fabricante,
                    "presentacion_compra": r.presentacion_compra,
                    "contenido_presentacion": float(r.contenido_presentacion),
                    "unidad_contenido": r.unidad_contenido,
                    "factor_a_unidad_base": float(r.factor_a_unidad_base),
                    "precio_vigente": float(r.precio_vigente),
                    "tipo_precio": r.tipo_precio,
                    "moneda": r.moneda,
                    "iva_tasa": float(r.iva_tasa),
                    "flete_estimado": float(r.flete_estimado) if r.flete_estimado is not None else None,
                    "compra_minima": float(r.compra_minima),
                    "unidad_compra_minima": r.unidad_compra_minima,
                    "lead_time_dias": r.lead_time_dias,
                    "vigencia_desde": r.vigencia_desde.isoformat() if r.vigencia_desde else None,
                    "vigencia_hasta": r.vigencia_hasta.isoformat() if r.vigencia_hasta else None,
                    "activo": r.activo,
                }
                for r in relaciones
            ]
        )

    respuesta, estado = registrar_condicion_mp(mp, request.json or {})
    if estado >= 400:
        db.session.rollback()
    else:
        db.session.commit()
    return respuesta, estado


def registrar_condicion_mp(mp, data):
    id_mp = mp.id_mp
    proveedor = db.session.get(Proveedor, data.get("id_proveedor"))
    if not proveedor:
        return jsonify({"error": "Proveedor no encontrado"}), 400
    if proveedor.estatus != "Activo":
        return jsonify({"error": "Solo se pueden relacionar proveedores con estatus Activo"}), 400

    contenido = decimal_o_none(data.get("contenido_presentacion"))
    factor = decimal_o_none(data.get("factor_a_unidad_base"))
    precio = decimal_o_none(data.get("precio_vigente"))
    iva = decimal_o_none(data.get("iva_tasa"))
    compra_min = decimal_o_none(data.get("compra_minima"))
    flete = decimal_o_none(data.get("flete_estimado"))

    if not contenido or contenido <= 0:
        return jsonify({"error": "Contenido de presentación inválido"}), 400
    if not factor or factor <= 0:
        return jsonify({"error": "El factor a unidad base debe ser mayor que cero"}), 400
    if precio is None or precio < 0:
        return jsonify({"error": "Precio inválido"}), 400
    if compra_min is None or compra_min <= 0:
        return jsonify({"error": "Compra mínima inválida"}), 400
    try:
        lead = int(data.get("lead_time_dias", 0))
        if lead < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Lead time inválido"}), 400

    if iva is None or not 0 <= iva <= 100:
        return jsonify({"error": "IVA inválido: debe estar entre 0 y 100"}), 400
    if data.get("flete_estimado") not in (None, "") and (flete is None or flete < 0):
        return jsonify({"error": "Flete inválido"}), 400
    if data.get("moneda", "MXN") not in {"MXN", "USD"}:
        return jsonify({"error": "Moneda inválida"}), 400
    if data.get("tipo_precio", "Por presentación") not in {"Por presentación", "Por kg", "Por L", "Por pieza"}:
        return jsonify({"error": "Tipo de precio inválido"}), 400

    principal = bool_value(data.get("principal"))
    if principal:
        MateriaPrimaProveedor.query.filter_by(id_mp=id_mp, activo=True, principal=True).update({"principal": False})

    # Preservar histórico: una nueva condición desactiva la condición anterior del mismo proveedor.
    anteriores = MateriaPrimaProveedor.query.filter_by(id_mp=id_mp, id_proveedor=proveedor.id_proveedor, activo=True).all()
    for anterior in anteriores:
        anterior.activo = False
        anterior.vigencia_hasta = date.today()

    relacion = MateriaPrimaProveedor(
        id_mp=id_mp,
        id_proveedor=proveedor.id_proveedor,
        principal=principal,
        sku_proveedor=(data.get("sku_proveedor") or "").strip() or None,
        marca_fabricante=(data.get("marca_fabricante") or "").strip() or None,
        presentacion_compra=(data.get("presentacion_compra") or "").strip(),
        contenido_presentacion=contenido,
        unidad_contenido=(data.get("unidad_contenido") or "").strip(),
        factor_a_unidad_base=factor,
        precio_vigente=precio,
        tipo_precio=(data.get("tipo_precio") or "Por presentación").strip(),
        moneda=(data.get("moneda") or "MXN").strip().upper(),
        iva_tasa=iva if iva is not None else Decimal("16"),
        flete_estimado=flete,
        modalidad_flete=(data.get("modalidad_flete") or "").strip() or None,
        compra_minima=compra_min,
        unidad_compra_minima=(data.get("unidad_compra_minima") or "Presentación").strip(),
        lead_time_dias=lead,
        vigencia_desde=fecha_o_none(data.get("vigencia_desde")) or date.today(),
        activo=True,
        creado_por=usuario_actual(),
    )
    if not relacion.presentacion_compra or not relacion.unidad_contenido:
        return jsonify({"error": "Presentación y unidad de contenido son obligatorias"}), 400

    db.session.add(relacion)
    db.session.flush()

    # Histórico de costo unitario base cuando el precio está dado por presentación.
    if relacion.tipo_precio == "Por presentación" and relacion.factor_a_unidad_base > 0:
        costo_unitario = relacion.precio_vigente / relacion.factor_a_unidad_base
        db.session.add(
            MateriaPrimaCostoHist(
                id_mp=id_mp,
                tipo_costo="Precio proveedor / unidad base",
                costo_unidad_base=costo_unitario,
                moneda=relacion.moneda,
                origen=f"Proveedor {proveedor.codigo_proveedor} - relación {relacion.id_relacion}",
                usuario=usuario_actual(),
            )
        )

    registrar_bitacora_mp(
        mp,
        "Condición de proveedor",
        nuevo=f"{proveedor.razon_social}: {relacion.presentacion_compra}, {relacion.precio_vigente} {relacion.moneda}",
    )
    return jsonify({"mensaje": "Condición de proveedor registrada", "id_relacion": relacion.id_relacion}), 201


@app.route("/api/materias-primas/<int:id_mp>/proveedores/<int:id_relacion>/desactivar", methods=["PATCH"])
@admin_required_json
def desactivar_proveedor_mp(id_mp, id_relacion):
    mp = db.session.get(MateriaPrima, id_mp)
    relacion = MateriaPrimaProveedor.query.filter_by(id_mp=id_mp, id_relacion=id_relacion).first()
    if not mp or not relacion:
        return jsonify({"error": "Relación no encontrada"}), 404
    relacion.activo = False
    relacion.principal = False
    relacion.vigencia_hasta = date.today()
    registrar_bitacora_mp(mp, "Proveedor desactivado", anterior=relacion.proveedor.razon_social if relacion.proveedor else relacion.id_proveedor)
    db.session.commit()
    return jsonify({"mensaje": "Proveedor desactivado para esta materia prima"})


@app.route("/api/materias-primas/<int:id_mp>/documentos", methods=["GET", "POST"])
@admin_required_json
def documentos_mp(id_mp):
    mp = db.session.get(MateriaPrima, id_mp)
    if not mp:
        return jsonify({"error": "Materia prima no encontrada"}), 404

    if request.method == "GET":
        docs = MateriaPrimaDocumento.query.filter_by(id_mp=id_mp).order_by(MateriaPrimaDocumento.fecha_carga.desc()).all()
        return jsonify(
            [
                {
                    "id_documento": d.id_documento,
                    "tipo_documento": d.tipo_documento,
                    "nombre_original": d.nombre_original,
                    "version": d.version,
                    "fecha_carga": d.fecha_carga.strftime("%d/%m/%Y %H:%M"),
                    "vigencia_desde": d.vigencia_desde.isoformat() if d.vigencia_desde else None,
                    "vigencia_hasta": d.vigencia_hasta.isoformat() if d.vigencia_hasta else None,
                    "url_descarga": url_for("descargar_documento_mp", id_documento=d.id_documento),
                }
                for d in docs
            ]
        )

    archivo = request.files.get("archivo")
    tipo = (request.form.get("tipo_documento") or "Otro").strip()
    if not archivo or not archivo.filename:
        return jsonify({"error": "Archivo requerido"}), 400
    if not allowed_file(archivo.filename):
        return jsonify({"error": "Tipo de archivo no permitido"}), 400

    version_actual = (
        db.session.query(func.max(MateriaPrimaDocumento.version))
        .filter_by(id_mp=id_mp, tipo_documento=tipo)
        .scalar()
        or 0
    )
    version = version_actual + 1
    nombre_original = archivo.filename
    ext = nombre_original.rsplit(".", 1)[1].lower()
    nombre_guardado = secure_filename(f"mp_{id_mp}_{int(datetime.utcnow().timestamp())}_v{version}.{ext}")
    archivo.save(os.path.join(MP_UPLOAD_FOLDER, nombre_guardado))

    doc = MateriaPrimaDocumento(
        id_mp=id_mp,
        tipo_documento=tipo,
        nombre_original=nombre_original,
        nombre_archivo_guardado=nombre_guardado,
        version=version,
        vigencia_desde=fecha_o_none(request.form.get("vigencia_desde")),
        vigencia_hasta=fecha_o_none(request.form.get("vigencia_hasta")),
        usuario=usuario_actual(),
    )
    db.session.add(doc)
    registrar_bitacora_mp(mp, "Documento cargado", nuevo=f"{tipo} v{version}")
    db.session.commit()
    return jsonify({"mensaje": "Documento cargado", "version": version}), 201


@app.route("/api/materias-primas/documentos/<int:id_documento>/descargar", methods=["GET"])
def descargar_documento_mp(id_documento):
    if not session.get("admin_logueado"):
        return redirect(url_for("login_admin"))
    doc = db.session.get(MateriaPrimaDocumento, id_documento)
    if not doc:
        return "Documento no encontrado", 404
    return send_from_directory(MP_UPLOAD_FOLDER, doc.nombre_archivo_guardado, as_attachment=True, download_name=doc.nombre_original)


@app.route("/api/materias-primas/<int:id_mp>/bitacora", methods=["GET"])
@admin_required_json
def bitacora_mp(id_mp):
    mp = db.session.get(MateriaPrima, id_mp)
    if not mp:
        return jsonify({"error": "Materia prima no encontrada"}), 404
    registros = MateriaPrimaBitacora.query.filter_by(id_mp=id_mp).order_by(MateriaPrimaBitacora.fecha_hora.desc()).all()
    return jsonify(
        [
            {
                "id_bitacora": b.id_bitacora,
                "evento": b.campo_evento,
                "valor_anterior": b.valor_anterior,
                "valor_nuevo": b.valor_nuevo,
                "usuario": b.usuario,
                "fecha_hora": b.fecha_hora.strftime("%d/%m/%Y %H:%M:%S"),
                "motivo": b.motivo,
            }
            for b in registros
        ]
    )


# ==========================================
# Compatibilidad: asignación desde expediente de proveedor
# ==========================================
@app.route("/api/proveedores/<int:id_proveedor>/materias-primas", methods=["GET", "POST"])
@admin_required_json
def materias_primas_proveedor(id_proveedor):
    proveedor = db.session.get(Proveedor, id_proveedor)
    if not proveedor:
        return jsonify({"error": "Proveedor no encontrado"}), 404

    if request.method == "GET":
        relaciones = MateriaPrimaProveedor.query.filter_by(id_proveedor=id_proveedor, activo=True).order_by(MateriaPrimaProveedor.id_relacion.desc()).all()
        return jsonify(
            [
                {
                    "id_relacion": r.id_relacion,
                    "id_mp": r.id_mp,
                    "nombre": r.materia_prima.nombre_oficial,
                    "sku_mp": r.materia_prima.sku_mp,
                    "presentacion": r.presentacion_compra,
                    "precio_vigente": float(r.precio_vigente),
                    "moneda": r.moneda,
                    "principal": r.principal,
                }
                for r in relaciones
            ]
        )

    data = request.json or {}
    id_mp = data.get("id_mp", data.get("id_materia_prima"))
    mp = db.session.get(MateriaPrima, id_mp)
    if not mp:
        return jsonify({"error": "Materia prima no encontrada"}), 400

    # Delega usando el mismo contrato de la API de Materia Prima.
    payload = {
        "id_proveedor": id_proveedor,
        "principal": data.get("principal", False),
        "sku_proveedor": data.get("sku_proveedor"),
        "marca_fabricante": data.get("marca_fabricante"),
        "presentacion_compra": data.get("presentacion_compra"),
        "contenido_presentacion": data.get("contenido_presentacion"),
        "unidad_contenido": data.get("unidad_contenido", data.get("unidad_compra")),
        "factor_a_unidad_base": data.get("factor_a_unidad_base"),
        "precio_vigente": data.get("precio_vigente"),
        "tipo_precio": data.get("tipo_precio", "Por presentación"),
        "moneda": data.get("moneda", "MXN"),
        "iva_tasa": data.get("iva_tasa", 16),
        "flete_estimado": data.get("flete_estimado"),
        "compra_minima": data.get("compra_minima", 1),
        "unidad_compra_minima": data.get("unidad_compra_minima", "Presentación"),
        "lead_time_dias": data.get("lead_time_dias", data.get("lead_time", 0)),
    }

    # Validación/creación replicada para evitar una petición HTTP interna.
    # Se permite relacionar materias primas mientras el proveedor está en Borrador
    # (paso previo a la activación, según el flujo de alta); un proveedor Bloqueado
    # o Inactivo no puede recibir nuevas relaciones (RN-08).
    if proveedor.estatus not in ("Activo", "Borrador"):
        return jsonify({"error": "El proveedor debe estar Activo o en Borrador para relacionar materias primas."}), 400
    contenido = decimal_o_none(payload["contenido_presentacion"])
    factor = decimal_o_none(payload["factor_a_unidad_base"])
    precio = decimal_o_none(payload["precio_vigente"])
    if not contenido or contenido <= 0 or not factor or factor <= 0 or precio is None:
        return jsonify({"error": "Contenido, factor a unidad base y precio son obligatorios"}), 400
    try:
        lead = int(payload["lead_time_dias"] or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Lead time inválido"}), 400

    principal = bool_value(payload["principal"])
    if principal:
        MateriaPrimaProveedor.query.filter_by(id_mp=mp.id_mp, activo=True, principal=True).update({"principal": False})
    for anterior in MateriaPrimaProveedor.query.filter_by(id_mp=mp.id_mp, id_proveedor=id_proveedor, activo=True).all():
        anterior.activo = False
        anterior.vigencia_hasta = date.today()

    rel = MateriaPrimaProveedor(
        id_mp=mp.id_mp,
        id_proveedor=id_proveedor,
        principal=principal,
        sku_proveedor=(payload.get("sku_proveedor") or "").strip() or None,
        marca_fabricante=(payload.get("marca_fabricante") or "").strip() or None,
        presentacion_compra=(payload.get("presentacion_compra") or "").strip(),
        contenido_presentacion=contenido,
        unidad_contenido=(payload.get("unidad_contenido") or "").strip(),
        factor_a_unidad_base=factor,
        precio_vigente=precio,
        tipo_precio=payload.get("tipo_precio") or "Por presentación",
        moneda=(payload.get("moneda") or "MXN").upper(),
        iva_tasa=decimal_o_none(payload.get("iva_tasa")) or Decimal("16"),
        flete_estimado=decimal_o_none(payload.get("flete_estimado")),
        compra_minima=decimal_o_none(payload.get("compra_minima")) or Decimal("1"),
        unidad_compra_minima=payload.get("unidad_compra_minima") or "Presentación",
        lead_time_dias=lead,
        creado_por=usuario_actual(),
    )
    if not rel.presentacion_compra or not rel.unidad_contenido:
        return jsonify({"error": "Presentación y unidad son obligatorias"}), 400
    db.session.add(rel)
    registrar_bitacora_mp(mp, "Proveedor asignado", nuevo=proveedor.razon_social)
    db.session.commit()
    return jsonify({"mensaje": "Materia prima asignada al proveedor"}), 201


# Crea únicamente tablas faltantes; conserva las tablas y datos existentes de proveedores.
if __package__:
    from .productos_terminados import registrar_productos_terminados
else:
    from productos_terminados import registrar_productos_terminados

ProductoTerminado = registrar_productos_terminados(app, db)
if __package__:
    from .abastecimiento import registrar_abastecimiento
else:
    from abastecimiento import registrar_abastecimiento
OrdenCompra, CompraLinea, RecepcionMP, MovimientoMP = registrar_abastecimiento(
    app, db, MateriaPrima, Proveedor, MateriaPrimaProveedor, usuario_actual)


if __package__:
    from .recetas import registrar_recetas
else:
    from recetas import registrar_recetas
Receta, RecetaIngrediente, RecetaRevision = registrar_recetas(app, db, MateriaPrima, ProductoTerminado, usuario_actual)

if __package__:
    from .inventario import registrar_inventario
else:
    from inventario import registrar_inventario
AjusteInventario = registrar_inventario(app, db, MateriaPrima, MovimientoMP, usuario_actual)

if __package__:
    from .crm import registrar_crm
else:
    from crm import registrar_crm
(
    ClienteCRM,
    ClienteDomicilio,
    PedidoCRM,
    PedidoLineaCRM,
    PagoPedidoCRM,
    ReservaERP,
    OrdenLlenadoCRM,
    MovimientoPT,
    EventoCRM,
    EvidenciaEntregaCRM,
) = registrar_crm(
    app,
    db,
    MateriaPrima,
    ProductoTerminado,
    Receta,
    RecetaIngrediente,
    MovimientoMP,
    AjusteInventario,
    usuario_actual,
)

if __package__:
    from .operaciones import registrar_operaciones
else:
    from operaciones import registrar_operaciones

registrar_operaciones(
    app,
    db,
    Proveedor,
    MateriaPrima,
    MateriaPrimaProveedor,
    ProductoTerminado,
    Receta,
    RecetaIngrediente,
    OrdenCompra,
    MovimientoMP,
    AjusteInventario,
    PedidoCRM,
    PedidoLineaCRM,
    ReservaERP,
    OrdenLlenadoCRM,
    MovimientoPT,
)

with app.app_context():
    migrar_columnas_faltantes()
    db.create_all()
    sincronizar_proveedores_desde_recetario(RecetaIngrediente)

if __name__ == "__main__":
    app.run(debug=True)
