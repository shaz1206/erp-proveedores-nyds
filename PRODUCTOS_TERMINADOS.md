Producto terminado
==================

Acceso: botón «Producto terminado» en Proveedores o Materias primas.
Ruta directa: /admin/productos-terminados.

Incluye catálogo con búsqueda y filtro de estatus, alta y edición.

Campos de la referencia:
- Información básica: SKU, nombre del producto, categoría, estatus y mínimo stock.
- Características físicas: color, olor, viscosidad, PPM, biodegradabilidad y forma de uso.
- Ventas e imagen: Ruta, Vendss Clean, Arpelab y Vending (selección múltiple), e imagen del producto.

Categoría permite elegir categorías previamente capturadas o escribir una nueva. Forma de uso permite elegir Uso directo / Dilución o escribir otra. Estatus: Borrador, Activo o Inactivo. El mínimo stock se guarda como configuración; este módulo no registra movimientos de inventario.

Imágenes: PNG, JPG o WebP, hasta 5 MB y 20 megapíxeles. Se conservan al editar si no se elige otra, y pueden quitarse. Se guardan en la misma base de datos y su consulta requiere iniciar sesión.

Puesta en marcha
---------------
En el entorno Python donde ejecutas el ERP, actualiza dependencias:

    pip install -r requirements.txt

Luego reinicia vendssback.py. Al arrancar se crea la tabla productos_terminados si falta, sin reemplazar tablas existentes.

Pruebas: test_productos_terminados.py y test_proveedores_mp.py, desde la carpeta raíz, usan SQLite en memoria.
