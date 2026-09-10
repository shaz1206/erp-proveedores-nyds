# Cambios - Módulo de Materias Primas

## Implementado en esta versión

- Nuevo catálogo administrativo en `/admin/materias-primas`.
- Alta de materia prima como borrador y SKU automático `MP-000001`.
- Expediente con secciones de identificación, unidades, inventario, almacenamiento, alias, proveedores, datos técnicos, documentos y bitácora.
- Prevención de duplicidad por nombre normalizado y alias.
- Activación/desactivación con validación de datos mínimos.
- Relación muchos-a-muchos Materia Prima–Proveedor con histórico de condiciones.
- Cálculo/registro de costo por unidad base a partir de precio por presentación y factor.
- Versionado de documentos de materia prima.
- Integración con el expediente existente de proveedores.
- Corrección del alta interna de proveedores para usar `tiempo_entrega_normal` y del enlace de regreso al catálogo.
- El portal público mantiene mensajes genéricos de duplicidad.

## Tablas nuevas

- `materias_primas`
- `materia_prima_aliases`
- `materia_prima_inventario_config`
- `materia_prima_ubicaciones`
- `materia_prima_proveedor`
- `materia_prima_documentos`
- `materia_prima_costos_hist`
- `materia_prima_bitacora`

Las tablas se crean automáticamente al arrancar la aplicación mediante `db.create_all()` sin borrar las existentes.

## Aún no incluido

- Existencias reales y movimientos de inventario.
- Recepciones y lotes operativos.
- Órdenes de compra.
- Recetas y consumos de producción.
- Órdenes de producción.
- Descuento automático de stock.
- Catálogos administrativos independientes para almacenes/ubicaciones y familias (en esta primera versión se capturan desde la ficha).
