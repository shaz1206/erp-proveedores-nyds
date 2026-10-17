"""Reporte de bugs desde cualquier pantalla: captura + comentario, y bandeja para el admin."""
import base64
import binascii
from datetime import datetime

from flask import abort, jsonify, redirect, render_template, request, send_file, session, url_for
from io import BytesIO

MAX_CAPTURA = 4 * 1024 * 1024
MAX_COMENTARIO = 2000


def registrar_bugs(app, db):
    class ReporteBug(db.Model):
        __tablename__ = "reportes_bug"
        id = db.Column(db.Integer, primary_key=True)
        usuario = db.Column(db.String(100), nullable=False)
        pagina = db.Column(db.String(500), nullable=False)
        comentario = db.Column(db.Text, nullable=False)
        captura = db.Column(db.LargeBinary)
        captura_tipo = db.Column(db.String(30))
        navegador = db.Column(db.String(300))
        resuelto = db.Column(db.Boolean, nullable=False, default=False)
        fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def quien():
        if session.get("crm_logueado"):
            return session.get("crm_nombre") or session.get("crm_usuario") or "CRM"
        if session.get("admin_logueado"):
            return session.get("admin_nombre") or session.get("admin_usuario") or "Administrador ERP"
        return None

    @app.route("/api/reportes-bug", methods=["POST"])
    def crear_reporte_bug():
        usuario = quien()
        if not usuario:
            return jsonify({"error": "No autorizado"}), 403
        data = request.get_json(silent=True) or {}
        comentario = (data.get("comentario") or "").strip()
        if not comentario:
            return jsonify({"error": "Describe qué falla."}), 400
        if len(comentario) > MAX_COMENTARIO:
            return jsonify({"error": f"El comentario admite máximo {MAX_COMENTARIO} caracteres."}), 400
        captura = tipo = None
        crudo = data.get("captura") or ""
        if crudo:
            try:
                cabecera, cuerpo = crudo.split(",", 1)
                tipo = cabecera.split(";")[0].replace("data:", "")
                if tipo not in {"image/png", "image/jpeg"}:
                    raise ValueError
                captura = base64.b64decode(cuerpo, validate=True)
            except (ValueError, binascii.Error):
                return jsonify({"error": "La captura no es válida."}), 400
            if len(captura) > MAX_CAPTURA:
                return jsonify({"error": "La captura es demasiado pesada."}), 400
        db.session.add(ReporteBug(
            usuario=usuario,
            pagina=(data.get("pagina") or "")[:500],
            comentario=comentario,
            captura=captura,
            captura_tipo=tipo,
            navegador=(request.headers.get("User-Agent") or "")[:300],
        ))
        db.session.commit()
        return jsonify({"mensaje": "Reporte enviado. ¡Gracias!"}), 201

    @app.route("/admin/bugs")
    def lista_reportes_bug():
        if not session.get("admin_logueado"):
            return redirect(url_for("login_admin"))
        reportes = ReporteBug.query.order_by(ReporteBug.resuelto, ReporteBug.fecha.desc()).all()
        return render_template("bugs.html", reportes=reportes)

    @app.route("/admin/bugs/<int:id_reporte>/captura")
    def captura_reporte_bug(id_reporte):
        if not session.get("admin_logueado"):
            return redirect(url_for("login_admin"))
        reporte = db.session.get(ReporteBug, id_reporte)
        if not reporte or not reporte.captura:
            abort(404)
        respuesta = send_file(BytesIO(reporte.captura), mimetype=reporte.captura_tipo, max_age=0)
        respuesta.headers["X-Content-Type-Options"] = "nosniff"
        return respuesta

    @app.route("/admin/bugs/<int:id_reporte>/resolver", methods=["POST"])
    def resolver_reporte_bug(id_reporte):
        if not session.get("admin_logueado"):
            return redirect(url_for("login_admin"))
        reporte = db.session.get(ReporteBug, id_reporte)
        if not reporte:
            abort(404)
        reporte.resuelto = not reporte.resuelto
        db.session.commit()
        return redirect(url_for("lista_reportes_bug"))

    return ReporteBug
