# Carga inicial del recetario NYDS

Se importó Recetario_NYDS.docx como datos de revisión, sin activar fórmulas ni modificar inventario.

- 31 apartados del documento se desglosan en 41 fórmulas: 33 ligadas a productos y 8 semiterminados (7 colores y DTK).
- 190 renglones de ingredientes; 23 vínculos a otras fórmulas.
- 55 registros de materias primas en borrador. Se normalizaron espacios, mayúsculas y acentos para coincidencias de nombre, pero no se fusionaron equivalencias químicas no confirmadas.
- Rendimiento solo donde se documenta expresamente: 7 colores y 4 productos finales de pisos. En los demás queda pendiente.
- Los proveedores se conservan como referencias de texto. No se inventaron RFC, proveedores comerciales, precios, presentaciones, conversiones de masa/volumen ni existencias.
- Familia y estado físico por clasificar. La unidad base inicial del insumo es la primera unidad observada (g o mL); los usos en distintas unidades quedan señalados para revisión.

## Consulta y actualización
Abrir **Recetas → Revisar receta**. Se pueden actualizar cantidades, unidades, correspondencia con ingredientes/subrecetas, referencia del proveedor, rendimiento, notas y pendientes. Guardar requiere un motivo y conserva la versión anterior. La receta permanece en borrador.

Cada ficha permite desplegar los ingredientes y notas originales, que no se sobrescriben con las ediciones. La referencia de COLORES y DTK se modeló como otra receta, sin crear materias primas externas duplicadas para esas preparaciones. Los concentrados de pisos también están vinculados con sus recetas finales.

## Fuente, respaldo y base
- Fuente original y extracción: `data/recetario/Recetario_NYDS.docx` y `.json`.
- Resultado: `data/recetario/resultado_carga.json`, con ruta del respaldo generado antes de la carga.
- Base predeterminada: `instance/erp.db` en la raíz del proyecto, tanto al iniciar como script como al iniciar con Flask. `DATABASE_URL` sigue permitiendo una configuración explícita.
- La base antigua `proveedores/instance/erp.db` no fue modificada ni fusionada.
- Repetir la carga del mismo documento no duplica ni sobrescribe datos actualizados. Una fuente distinta se rechaza para evitar una sustitución silenciosa.

La importación es una sola transacción. Se verificaron conservación de cantidades y unidades, relaciones entre recetas, ausencia de movimientos, reimportación, historial, control de edición y acceso administrativo.
