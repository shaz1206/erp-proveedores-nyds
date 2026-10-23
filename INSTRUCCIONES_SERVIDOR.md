# Actualizar el ERP/CRM de nyds.mx a la versión 1.2.1 y cargar el catálogo inicial

Para: la persona que sube el sistema al servidor.
Tiempo estimado: 15–20 minutos. Conviene hacerlo fuera de horario, porque hay que reiniciar la aplicación.

## Qué cambia

- El servidor corre hoy una versión vieja (no tiene `/api/version`) y casi no tiene datos: 0 proveedores, 0 materias primas, 0 recetas, 1 producto (Lavatrastes A-001), 1 cliente (Bokka) con 1 pedido.
- La versión 1.2.1 trae, entre otras cosas: cancelar pedidos, tipos de venta con listas de precios, alta de productos del recetario, importador de Excel, respaldo diario automático, versión visible en el pie y en `/api/version`.
- Un script de carga inicial agrega 8 proveedores, 55 materias primas con su condición de compra, 33 productos terminados con 87 presentaciones (1 L, 5 L, 20 L), 41 recetas y 4 listas de precios (Casa habitación, Restaurantes, Automotrices, Otros negocios).

**El script solo agrega.** No modifica ni borra clientes, pedidos, personas, compras, inventarios, ni el producto A-001, y puede correrse más de una vez sin duplicar.

## Pasos

1. **Respaldar** (obligatorio). Copia la base de datos (`instance/erp.db`, junto a la carpeta del proyecto) y la carpeta `uploads/` a un lugar seguro, con la fecha en el nombre.
2. **Actualizar el código** desde GitHub, rama `main`: <https://github.com/shaz1206/erp-proveedores-nyds>
   - `git pull origin main` (o el método que ya usen para publicar).
   - `python -m pip install -r requirements.txt`
3. **Reiniciar la aplicación** como lo hagan normalmente. Al arrancar crea solas las tablas nuevas. Entra a `https://nyds.mx/api/version`: debe responder `"version": "1.2.1"`.
4. **Prueba en seco** de la carga (no guarda nada). Desde la carpeta del proyecto y con el mismo entorno que usa la aplicación:
   ```
   python cargar_datos_iniciales.py
   ```
   Debe terminar con "Prueba terminada" y una tabla como esta (los números son los esperados en una base casi vacía):

   | Tabla | nuevos |
   |---|---|
   | proveedores | 8 |
   | materias_primas | 55 |
   | productos_terminados | 33 |
   | recetas_borrador | 41 |
   | listas_precios | 4 |
   | materia_prima_proveedor | 56 |
   | pt_presentaciones | 87 |
   | receta_ingredientes | 190 |
   | receta_revisiones | 41 |
   | listas_precios_items | 208 |

   Si aparece una línea que empieza con `AVISO` o un error de tabla o columna faltante, **no sigas**: avisa y manda el mensaje completo.
5. **Aplicar**:
   ```
   python cargar_datos_iniciales.py --aplicar
   ```
   Primero crea un respaldo automático en `backups/` y después guarda todo en una sola transacción: si algo falla, no se guarda nada.
6. **Reiniciar la aplicación** una vez más y revisar.

## Cómo comprobar que quedó bien

Con el usuario administrador:

- `/admin/operaciones`: ya no debe decir "Todavía no hay catálogo maestro"; debe mostrar 55 materias primas activas y 8 proveedores.
- `/admin/materias-primas`: 55 registros.
- `/admin/productos-terminados`: 34 productos (los 33 nuevos más Lavatrastes A-001, que se queda como estaba).
- `/admin/precios`: 4 listas.
- `/admin/clientes`: sigue el cliente Bokka con su pedido de $33.

## Para deshacer

Detener la aplicación, restaurar la copia de `instance/erp.db` del paso 1 y volver a iniciar. La versión anterior del código sigue disponible en el historial de GitHub.

## Notas

- Hay un posible producto duplicado: el servidor tiene "Lavatrastes" (A-001, $33) y la carga agrega "Lavatrastes & Cocina Limón" (PT-R00004). No se fusionan solos; NYDS decidirá cuál conservar.
- Los datos de demostración (proveedor y producto "DEMO") no se copian.
- No se necesita ninguna contraseña ni usuario nuevo para este proceso, y las cuentas existentes no se tocan.
- Los precios de la lista Casa habitación vienen de la lista oficial. Restaurantes, Automotrices y Otros negocios empiezan con los mismos precios y se ajustan después en `/admin/precios`.
