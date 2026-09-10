"""Catálogo de producto terminado, integrado con la sesión del ERP."""
from decimal import Decimal, InvalidOperation
from io import BytesIO

from flask import request, render_template, redirect, url_for, session, send_file, abort
from PIL import Image, UnidentifiedImageError
from sqlalchemy.exc import IntegrityError


def registrar_productos_terminados(app, db):
    class ProductoTerminado(db.Model):
        __tablename__ = 'productos_terminados'
        id = db.Column(db.Integer, primary_key=True)
        sku = db.Column(db.String(40), unique=True, nullable=False)
        nombre = db.Column(db.String(150), nullable=False)
        categoria = db.Column(db.String(100), nullable=False)
        estatus = db.Column(db.String(20), nullable=False, default='Borrador')
        stock_minimo = db.Column(db.Numeric(18, 6), nullable=False, default=0)
        color = db.Column(db.String(100))
        olor = db.Column(db.String(100))
        viscosidad = db.Column(db.String(100))
        ppm = db.Column(db.Numeric(18, 6))
        biodegradabilidad = db.Column(db.String(40))
        forma_uso = db.Column(db.String(150))
        puntos_venta = db.Column(db.JSON, nullable=False, default=list)
        imagen = db.Column(db.LargeBinary)
        imagen_tipo = db.Column(db.String(30))

    puntos = ['Ruta', 'Vendss Clean', 'Arpelab', 'Vending']
    estados = ['Borrador', 'Activo', 'Inactivo']
    biodegradabilidad = ['Sí', 'No', 'No especificada']

    def autorizado():
        return bool(session.get('admin_logueado'))

    @app.route('/admin/productos-terminados')
    def catalogo_productos_terminados():
        if not autorizado():
            return redirect(url_for('login_admin'))
        q = request.args.get('q', '').strip()
        estado = request.args.get('estatus', '')
        consulta = ProductoTerminado.query
        if q:
            consulta = consulta.filter(db.or_(ProductoTerminado.sku.contains(q, autoescape=True),
                ProductoTerminado.nombre.contains(q, autoescape=True), ProductoTerminado.categoria.contains(q, autoescape=True)))
        if estado:
            consulta = consulta.filter_by(estatus=estado)
        return render_template('productos_terminados_catalogo.html', productos=consulta.order_by(ProductoTerminado.nombre).all(),
            q=q, estado=estado, estados=estados)

    @app.route('/admin/productos-terminados/nuevo', methods=['GET', 'POST'])
    @app.route('/admin/productos-terminados/<int:id_producto>', methods=['GET', 'POST'])
    def formulario_producto_terminado(id_producto=None):
        if not autorizado():
            return redirect(url_for('login_admin'))
        producto = db.session.get(ProductoTerminado, id_producto) if id_producto else None
        if id_producto and not producto:
            abort(404)
        valores = producto or dict(estatus='Borrador', stock_minimo=0, puntos_venta=[])
        errores = []
        if request.method == 'POST':
            valores = request.form.to_dict()
            valores['puntos_venta'] = request.form.getlist('puntos_venta')
            datos = {}
            for campo, limite in dict(sku=40, nombre=150, categoria=100, color=100, olor=100,
                                      viscosidad=100, forma_uso=150).items():
                datos[campo] = request.form.get(campo, '').strip()
                if campo == 'sku':
                    datos[campo] = datos[campo].upper()
                if len(datos[campo]) > limite:
                    errores.append(f'{campo}: máximo {limite} caracteres.')
            if any(not datos[c] for c in ['sku', 'nombre', 'categoria']):
                errores.append('SKU, nombre y categoría son obligatorios.')
            for campo in ['stock_minimo', 'ppm']:
                texto = request.form.get(campo, '').strip()
                try:
                    numero = Decimal(texto) if texto else (Decimal(0) if campo == 'stock_minimo' else None)
                    if numero is not None and (not numero.is_finite() or numero < 0 or numero >= Decimal('1000000000000') or numero.as_tuple().exponent < -6):
                        raise ValueError
                    datos[campo] = numero
                except (InvalidOperation, ValueError):
                    errores.append(f'{campo}: usa un número positivo o cero, con máximo 6 decimales y menor a un billón.')
            datos['estatus'] = request.form.get('estatus')
            datos['biodegradabilidad'] = request.form.get('biodegradabilidad', '')
            datos['puntos_venta'] = list(dict.fromkeys(valores['puntos_venta']))
            if datos['estatus'] not in estados:
                errores.append('Selecciona un estatus válido.')
            if datos['biodegradabilidad'] not in biodegradabilidad + ['']:
                errores.append('Selecciona una biodegradabilidad válida.')
            if any(p not in puntos for p in datos['puntos_venta']):
                errores.append('Punto de venta inválido.')
            duplicado = ProductoTerminado.query.filter_by(sku=datos['sku']).first()
            if duplicado and duplicado.id != id_producto:
                errores.append('El SKU ya está registrado en otro producto terminado.')
            archivo = request.files.get('imagen')
            if archivo and archivo.filename:
                contenido = archivo.read(5 * 1024 * 1024 + 1)
                if len(contenido) > 5 * 1024 * 1024:
                    errores.append('La imagen debe pesar como máximo 5 MB.')
                else:
                    try:
                        with Image.open(BytesIO(contenido)) as imagen:
                            formato = imagen.format
                            if formato not in ['PNG', 'JPEG', 'WEBP'] or imagen.width * imagen.height > 20000000:
                                raise ValueError
                            imagen.verify()
                        datos['imagen'] = contenido
                        datos['imagen_tipo'] = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}[formato]
                    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
                        errores.append('Sube una imagen PNG, JPG o WebP válida, de hasta 20 megapíxeles.')
            elif request.form.get('quitar_imagen'):
                datos['imagen'] = None
                datos['imagen_tipo'] = None
            if not errores:
                nuevo = producto or ProductoTerminado()
                for campo, valor in datos.items():
                    setattr(nuevo, campo, valor)
                db.session.add(nuevo)
                try:
                    db.session.commit()
                    return redirect(url_for('catalogo_productos_terminados', guardado=1))
                except IntegrityError:
                    db.session.rollback()
                    errores.append('El SKU ya existe. Usa uno diferente.')
        categorias = [c[0] for c in db.session.query(ProductoTerminado.categoria).distinct().order_by(ProductoTerminado.categoria)]
        return render_template('producto_terminado_form.html', producto=producto, valores=valores,
            errores=errores, puntos=puntos, estados=estados, biodegradabilidad=biodegradabilidad, categorias=categorias), (400 if errores else 200)

    @app.route('/admin/productos-terminados/<int:id_producto>/imagen')
    def imagen_producto_terminado(id_producto):
        if not autorizado():
            return redirect(url_for('login_admin'))
        producto = db.session.get(ProductoTerminado, id_producto)
        if not producto or not producto.imagen:
            abort(404)
        response = send_file(BytesIO(producto.imagen), mimetype=producto.imagen_tipo, max_age=0)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    return ProductoTerminado
