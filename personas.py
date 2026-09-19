"""Acceso por persona: reemplaza las cuentas compartidas (admin unico del ERP
y los 5 logins de equipo del CRM) por personas individuales con su propia
contraseña. Los roles y permisos existentes no cambian, solo quien puede
entrar con cada uno."""
import os
import re
from datetime import datetime

from flask import jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

ROLES_PERSONA = {"supervisor", "ventas", "preparacion", "reparto", "cobranza"}
USUARIO_REGEX = re.compile(r"^[a-z0-9._-]{3,60}$")


def normalizar_usuario(valor):
    return re.sub(r"\s+", "", (valor or "").strip().lower())


def _valor_booleano(valor, default=False):
    if valor is None:
        return default
    if isinstance(valor, bool):
        return valor
    return str(valor).strip().lower() in {"1", "true", "si", "sí", "on", "yes"}


def registrar_personas(app, db, usuario_actual):
    class Persona(db.Model):
        __tablename__ = "personas"
        id = db.Column(db.Integer, primary_key=True)
        nombre = db.Column(db.String(180), nullable=False)
        usuario = db.Column(db.String(60), unique=True, nullable=False)
        password_hash = db.Column(db.String(255), nullable=False)
        rol = db.Column(db.String(20), nullable=False)
        activo = db.Column(db.Boolean, nullable=False, default=True)
        fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        fecha_actualizacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def autenticar_persona(usuario, password, roles_permitidos=None):
        """Busca una persona activa por usuario/contraseña. roles_permitidos,
        si se indica, restringe el login a esos roles (p.ej. solo supervisor
        para el ERP)."""
        usuario_norm = normalizar_usuario(usuario)
        if not usuario_norm or not password:
            return None
        persona = Persona.query.filter_by(usuario=usuario_norm, activo=True).first()
        if not persona or not check_password_hash(persona.password_hash, password or ""):
            return None
        if roles_permitidos and persona.rol not in roles_permitidos:
            return None
        return persona

    def asegurar_persona_inicial():
        """Si todavia no existe ninguna persona (instalacion nueva, o base de
        datos recien creada), crea un primer supervisor a partir de las
        variables de entorno NYDS_ADMIN_USER/NYDS_ADMIN_PASSWORD, igual que
        el login unico que existia antes. Asi nadie queda fuera del sistema."""
        if Persona.query.count() > 0:
            return
        usuario = normalizar_usuario(os.environ.get("NYDS_ADMIN_USER", "compras.nyds")) or "compras.nyds"
        password = os.environ.get("NYDS_ADMIN_PASSWORD", "nyds2026")
        db.session.add(Persona(
            nombre="Administrador",
            usuario=usuario,
            password_hash=generate_password_hash(password),
            rol="supervisor",
            activo=True,
        ))
        db.session.commit()

    def supervisores_activos_restantes(excluir_id=None):
        consulta = Persona.query.filter_by(rol="supervisor", activo=True)
        if excluir_id is not None:
            consulta = consulta.filter(Persona.id != excluir_id)
        return consulta.count()

    @app.route("/admin/personas", methods=["GET"])
    def personas_vista():
        if not session.get("admin_logueado"):
            return redirect(url_for("login_admin"))
        return render_template("personas.html", roles=sorted(ROLES_PERSONA))

    @app.route("/api/personas", methods=["GET"])
    def api_personas_lista():
        if not session.get("admin_logueado"):
            return jsonify({"error": "No autorizado"}), 403
        personas = Persona.query.order_by(Persona.nombre).all()
        yo = normalizar_usuario(session.get("usuario"))
        return jsonify([
            {
                "id": p.id,
                "nombre": p.nombre,
                "usuario": p.usuario,
                "rol": p.rol,
                "activo": p.activo,
                "es_yo": p.usuario == yo,
            }
            for p in personas
        ])

    @app.route("/api/personas", methods=["POST"])
    def api_personas_crear():
        if not session.get("admin_logueado"):
            return jsonify({"error": "No autorizado"}), 403
        data = request.json or {}
        nombre = (data.get("nombre") or "").strip()
        usuario = normalizar_usuario(data.get("usuario"))
        password = data.get("password") or ""
        rol = (data.get("rol") or "").strip()
        errores = []
        if not nombre or len(nombre) > 180:
            errores.append("Indica un nombre de hasta 180 caracteres.")
        if not usuario or not USUARIO_REGEX.match(usuario):
            errores.append("El usuario debe tener de 3 a 60 caracteres: minusculas, numeros, punto, guion o guion bajo.")
        elif Persona.query.filter_by(usuario=usuario).first():
            errores.append("Ya existe una persona con ese usuario.")
        if not password or len(password) < 6:
            errores.append("La contraseña debe tener al menos 6 caracteres.")
        if rol not in ROLES_PERSONA:
            errores.append("Selecciona un rol valido.")
        if errores:
            return jsonify({"error": "Revisa los datos.", "detalles": errores}), 400
        persona = Persona(
            nombre=nombre,
            usuario=usuario,
            password_hash=generate_password_hash(password),
            rol=rol,
            activo=True,
        )
        db.session.add(persona)
        db.session.commit()
        return jsonify({"id": persona.id}), 201

    @app.route("/api/personas/<int:id_persona>", methods=["PATCH"])
    def api_personas_actualizar(id_persona):
        if not session.get("admin_logueado"):
            return jsonify({"error": "No autorizado"}), 403
        persona = db.session.get(Persona, id_persona)
        if not persona:
            return jsonify({"error": "Persona no encontrada"}), 404
        data = request.json or {}
        errores = []
        if "nombre" in data:
            nombre = (data.get("nombre") or "").strip()
            if not nombre or len(nombre) > 180:
                errores.append("Indica un nombre de hasta 180 caracteres.")
            else:
                persona.nombre = nombre
        if "rol" in data:
            rol = (data.get("rol") or "").strip()
            if rol not in ROLES_PERSONA:
                errores.append("Selecciona un rol valido.")
            elif (
                persona.rol == "supervisor"
                and rol != "supervisor"
                and persona.activo
                and supervisores_activos_restantes(excluir_id=persona.id) == 0
            ):
                errores.append("No puedes quitar el rol al ultimo supervisor activo.")
            else:
                persona.rol = rol
        if "activo" in data:
            activo = _valor_booleano(data.get("activo"))
            if (
                persona.rol == "supervisor"
                and persona.activo
                and not activo
                and supervisores_activos_restantes(excluir_id=persona.id) == 0
            ):
                errores.append("No puedes desactivar al ultimo supervisor activo.")
            else:
                persona.activo = activo
        if data.get("password"):
            password = data.get("password")
            if len(password) < 6:
                errores.append("La contraseña debe tener al menos 6 caracteres.")
            else:
                persona.password_hash = generate_password_hash(password)
        if errores:
            db.session.rollback()
            return jsonify({"error": "Revisa los datos.", "detalles": errores}), 400
        db.session.commit()
        return jsonify({"ok": True})

    return Persona, autenticar_persona, asegurar_persona_inicial, ROLES_PERSONA
