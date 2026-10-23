# ERP NYDS - Proveedores y Materias Primas

Proyecto Flask/SQLAlchemy para el ERP NYDS. Esta versión conserva el módulo existente de **Proveedores** e incorpora la primera versión funcional del **Catálogo Maestro de Materias Primas**.

## Versiones

La versión vigente vive en `vendssback.py` (`APP_VERSION` y `APP_VERSION_FECHA`). Se muestra en el pie del menú del ERP y del CRM, en las pantallas de acceso, en `GET /api/version` y se anota en cada reporte de bug. Al publicar cambios se sube el número: segundo dígito para funciones nuevas, tercero para correcciones.

- **1.2.0** (2026-10-23): alta de los productos terminados del recetario en un clic (Producto terminado > Dar de alta del recetario); cancelar pedidos desde Pedidos (antes de salir a ruta); tipos de venta (listas de precios por cliente, elegibles al registrar el cliente y en la venta); crear listas copiando otra con un ajuste en %; presentaciones 1 L, 5 L y 20 L para todos los productos; script de carga inicial para un ERP nuevo (`cargar_datos_iniciales.py`, ver `INSTRUCCIONES_SERVIDOR.md`).
- **1.1.1** (2026-10-19): pie simplificado (derechos y versión como texto); recepción de compras con un dato por campo; orden de compra con Material y Presentación separados; Operaciones sin alertas por materia prima; Distribuidores oculto del menú (y del CRM mientras no haya distribuidores); textos más claros en materia prima y personas.
- **1.1.0** (2026-10-19): pie NYDS con versión; importación de proveedores y clientes desde Excel/CSV; "Recibir todo completo"; botones Vender y Cobrar pendientes; datos del proveedor editables; respaldo diario automático.
- **1.0.0**: ERP de proveedores, compras, inventario, producción y CRM de pedidos.

## Requisitos

- Python 3.10 o superior.
- Dependencias de `requirements.txt`.

```bash
python -m pip install -r requirements.txt
```

## Ejecución

Desde la carpeta del proyecto:

```bash
python vendssback.py
```

Después abre:

- Portal público de proveedores: `http://127.0.0.1:5000/`
- Acceso administrativo: `http://127.0.0.1:5000/login`
- Catálogo de proveedores: `http://127.0.0.1:5000/admin/catalogo`
- Catálogo de materias primas: `http://127.0.0.1:5000/admin/materias-primas`
- Inventario de materia prima y de producto terminado: `/admin/inventario-mp` y `/admin/inventario-pt`
- Clientes (balance y estado de cuenta por cliente): `/admin/clientes`
- CRM de pedidos: `http://127.0.0.1:5000/crm/login`
- Tienda en línea para clientes: `http://127.0.0.1:5000/tienda`

La base de datos SQLite vive **fuera** del repositorio, en `../instance/erp.db` (junto a la carpeta del proyecto), y los archivos subidos en `uploads/`. Ninguno de los dos se versiona.

### Usuarios

Los usuarios del ERP y del CRM se guardan en la base de datos (tabla `personas`) y se administran en `/admin/personas`, cada uno con su rol (supervisor, ventas, preparación, reparto o cobranza). En una base nueva se crea un primer supervisor con:

- Usuario: variable de entorno `NYDS_ADMIN_USER` (por omisión `compras.nyds`)
- Contraseña: variable de entorno `NYDS_ADMIN_PASSWORD`. Defínela siempre; si no, se usa la contraseña por omisión que está en el código.
- `SECRET_KEY`: variable de entorno `SECRET_KEY` (si no se define, las sesiones se pierden al reiniciar).

## Pruebas

Las pruebas automatizadas usan SQLite en memoria y nunca tocan la base real. Desde la carpeta del proyecto:

```bash
python -m unittest discover -s pruebas
```

## Documentación

- `docs/ESTADO_ACTUAL_CRM_ERP.md`: descripción general de cómo se conectan el ERP y el CRM (redactada el 15 de septiembre de 2026; algunos detalles, como el login del CRM, ya cambiaron).
- `docs/PROVEEDORES_Y_PRECIOS.md`: uso de proveedores y precios por materia prima.

## Módulo de Materias Primas incluido

La implementación incorpora:

- SKU automático `MP-000001`, `MP-000002`, etc.
- Alta como borrador, edición, activación y desactivación.
- Prevención de duplicados por nombre oficial y alias normalizados.
- Alias/sinónimos sin crear inventarios separados.
- Unidad base, densidad y factores de conversión.
- Configuración de stock mínimo, seguridad, punto de reorden, stock objetivo, lote, caducidad, FIFO/FEFO y lead time.
- Ubicación principal y condiciones de almacenamiento.
- Datos técnicos, seguridad, calidad y COA.
- Relación N:M con proveedores ya registrados.
- Historial de condiciones de proveedor (precio, presentación, MOQ, factor y lead time).
- Expediente documental versionado para materias primas.
- Bitácora de cambios.
- Integración desde el expediente del proveedor hacia el nuevo catálogo maestro.

## Base de datos existente

El proyecto sigue usando SQLite (`instance/erp.db`). Al iniciar la aplicación, `db.create_all()` crea las tablas nuevas que falten y **no elimina las tablas existentes ni los proveedores ya registrados**.

Aun así, antes de probar cambios en una base productiva conviene respaldar `instance/erp.db`.

## Alcance pendiente

Este módulo prepara la estructura para las siguientes fases, pero todavía no implementa movimientos reales de inventario, entradas/salidas, lotes de recepción, órdenes de compra, recetas productivas, órdenes de producción ni descuento automático de existencias. En el catálogo, el stock actual se muestra como pendiente hasta integrar Inventarios.
