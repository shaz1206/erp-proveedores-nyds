Uso de proveedores y precios por materia prima
=============================================

1. Abre Materias primas → Nueva materia prima y captura los datos generales.
2. En «Proveedores y precios», selecciona un proveedor activo y captura su presentación, factor, precio, moneda y condiciones.
3. Pulsa «Agregar proveedor» y repite para cada proveedor. Puedes marcar uno como principal.
4. Pulsa «Guardar borrador» para guardar la materia prima y todos los proveedores juntos.

Para ajustar precios, abre el expediente → Proveedores y precios → «Editar precio y condiciones» en el proveedor correspondiente. Modifica los valores y pulsa «Guardar condición». En expedientes existentes este botón guarda inmediatamente. La condición anterior permanece como histórica; los otros proveedores conservan sus precios.

«Cancelar captura de proveedor» limpia los campos sin cambiar las condiciones guardadas. «Quitar» desactiva una condición existente y conserva su histórico.

Se aprovechan las tablas existentes; no se requiere migración. Reinicia el servidor Flask si no tiene recarga automática y actualiza la página.

Verificación: test_proveedores_mp.py usa SQLite en memoria y comprueba alta múltiple, precios independientes con histórico, rechazo de duplicados y valores inválidos, transacción sin registros parciales y renderizado del formulario.
