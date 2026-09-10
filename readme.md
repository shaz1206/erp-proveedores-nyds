# ERP NYDS - Proveedores y Materias Primas

Proyecto Flask/SQLAlchemy para el ERP NYDS. Esta versión conserva el módulo existente de **Proveedores** e incorpora la primera versión funcional del **Catálogo Maestro de Materias Primas**.

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

Credenciales actuales del prototipo:

- Usuario: `compras.nyds`
- Contraseña: `nyds2026*`

> Para producción, mueve `SECRET_KEY` y las credenciales a variables de entorno y sustituye el login fijo por usuarios con contraseñas cifradas.

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
