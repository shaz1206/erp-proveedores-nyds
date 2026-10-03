# Estado actual del ERP + CRM de NYDS

*Generado a partir de una revisión del código fuente y de la suite de pruebas automatizadas (62 pruebas, todas verdes) el 15 de septiembre de 2026.*

## Resumen ejecutivo

El proyecto es una sola aplicación Flask (`vendssback.py` como punto de entrada) que registra un conjunto de módulos independientes — proveedores, materias primas, compras, inventario, recetas, producto terminado, CRM de pedidos y un panel de operaciones — todos contra la misma base de datos SQLite (`instance/erp.db`). No son dos sistemas separados que se sincronizan: el CRM es un módulo más dentro del mismo proceso, que lee y escribe directamente sobre las tablas del ERP (materias primas, producto terminado, recetas, inventario). Por eso el encabezado de la aplicación dice "ERP y CRM conectados": comparten sesión de base de datos, y una operación en el CRM (confirmar un pedido, por ejemplo) puede disparar movimientos de inventario reales en el ERP en la misma transacción.

Hoy en día ya funciona de punta a punta el flujo: alta de proveedor → alta de materia prima con proveedores y precios → orden de compra → recepción con control de lote/caducidad/COA → saldo de inventario → receta que liga producto terminado con materias primas → pedido en el CRM → reserva automática de inventario (producto terminado si hay, o materias primas + orden de llenado si no) → producción/llenado → salida a ruta → entrega con evidencia → movimiento de inventario de salida. Ese ciclo completo está cubierto por pruebas automatizadas y pasa hoy sin errores.

## Arquitectura general

`vendssback.py` define el modelo `Proveedor` y sus entidades relacionadas (contactos, documentos, evaluaciones, materias primas suministradas) y el modelo `MateriaPrima` con toda su ficha técnica. Al arrancar, la app registra en orden los demás módulos, cada uno como una función `registrar_X(app, db, ...)` que recibe los modelos que necesita de los módulos anteriores y devuelve los modelos nuevos que crea, para que el siguiente módulo pueda usarlos:

1. `productos_terminados.py` — catálogo de producto terminado.
2. `abastecimiento.py` — órdenes de compra y recepciones (recibe `MateriaPrima` y `Proveedor`).
3. `recetas.py` — recetas de producción (recibe `MateriaPrima` y `ProductoTerminado`).
4. `inventario.py` — ajustes manuales de inventario de materia prima (recibe `MovimientoMP` de abastecimiento).
5. `crm.py` — pedidos, reservas, llenado, ruta y entrega (recibe `ProductoTerminado`, `Receta`, `RecetaIngrediente`, `MovimientoMP`, `AjusteInventario`).
6. `operaciones.py` — panel de salud que audita que todo lo anterior esté bien enlazado.

Al final, `migrar_columnas_faltantes()` inspecciona las tablas existentes y agrega por SQL crudo cualquier columna nueva que un modelo haya sumado desde la última vez que corrió la app (porque `db.create_all()` solo crea tablas que faltan, nunca columnas nuevas en tablas ya existentes), y después `db.create_all()` crea las tablas que falten. Esto es lo que ha permitido ir ampliando el modelo de datos sin perder la base SQLite real del usuario.

## Autenticación y roles: dos puertas, una sola base de datos

El ERP administrativo y el CRM tienen sistemas de login separados, pero ambos viven en el mismo proceso y comparten las mismas tablas.

El ERP usa una sola cuenta de administrador (`compras.nyds` por defecto) contra la variable de entorno `NYDS_ADMIN_PASSWORD`, que abre `session["admin_logueado"]`. Con eso se accede a `/admin/catalogo`, `/admin/materias-primas`, `/admin/compras`, `/admin/recetas`, `/admin/inventario-mp`, `/admin/productos-terminados` y `/admin/operaciones`.

El CRM (`/crm/login`) tiene cinco cuentas con rol fijo, cada una con su propia variable de entorno de contraseña: `crm.admin` (supervisor), `ventas.nyds` (ventas), `preparacion.nyds` (preparación), `reparto.nyds` (reparto) y `caja.nyds` (cobranza). El rol determina qué puede hacer cada quien: solo ventas crea pedidos y los confirma, solo preparación cierra órdenes de llenado y mueve a "en preparación"/"listo para entrega", solo reparto pasa un pedido a "en ruta" o registra la entrega, y solo cobranza registra pagos. Un usuario logueado como administrador del ERP (`admin_logueado`) automáticamente tiene rol de supervisor dentro del CRM y puede hacer cualquier cosa; en cambio, un usuario logueado solo en el CRM no puede entrar a las pantallas del ERP (`/admin/catalogo` le regresa 302 al login). Esta separación de permisos, incluyendo que un rol no pueda ejecutar acciones de otro rol, está verificada por una prueba automatizada dedicada.

## Catálogo de proveedores

El alta de proveedor exige razón social, RFC (validado contra un patrón real de RFC mexicano), tipo de proveedor, código postal, condición de pago y tiempo de entrega habitual, más al menos un contacto marcado como principal con correo y teléfono o WhatsApp. Cada proveedor recibe un código secuencial `PROV-0001`, `PROV-0002`, etc. El ciclo de vida tiene cuatro estados — Borrador, Activo, Bloqueado e Inactivo — y activar un proveedor exige que todos los campos mínimos estén completos (incluyendo, si su tipo de suministro es materia prima o mixto, al menos una materia prima relacionada y activa). Bloquear o desactivar un proveedor exige motivo y revisa que no tenga órdenes de compra abiertas (Emitida o Parcial) antes de permitirlo. No existe borrado físico: todo cambio de estatus queda en la bitácora del proveedor, con usuario, fecha y motivo. Cada proveedor puede tener documentos (con versión, vigencia y usuario que los subió) y evaluaciones periódicas ponderadas (precio 25%, calidad 30%, entrega 30%, atención 15%) que producen una calificación final.

Esta parte del sistema fue la más recientemente ampliada: los doce criterios de aceptación del documento de especificación del programador (RFC único, un solo contacto principal activo, materias primas con condiciones de precio independientes por proveedor, bloqueo con motivo obligatorio, reactivación auditada, documentos versionados, catálogo de compras que solo ofrece materias primas de proveedores activos, imposibilidad de borrado físico, bitácora completa, entre otros) están hoy cada uno cubierto por su propia prueba automatizada y todos pasan.

## Catálogo de materias primas

Cada materia prima recibe un SKU automático (`MP-000001`, etc.), se da de alta en Borrador y se activa cuando tiene familia, estado físico, unidad base y política de inventario completos. El sistema detecta posibles duplicados por nombre normalizado antes de guardar. Cada materia prima define su unidad base, factor de conversión (con soporte para conversión masa↔volumen únicamente si hay densidad registrada; el sistema explícitamente no asume que 1 L = 1 kg), política de reabastecimiento (stock mínimo, de seguridad, punto de reorden, stock objetivo, FIFO o FEFO, si controla lote o caducidad), ubicación principal de almacén, datos técnicos y de seguridad (peligrosidad, pureza, si requiere certificado de análisis por lote), alias/sinónimos para búsqueda sin crear inventarios paralelos, la relación con los proveedores que la surten (cada uno con su propia presentación, contenido, factor y precio, pudiendo marcar uno como preferente), expediente documental versionado y bitácora de cambios.

## Producto terminado

Catálogo simple pero funcional: SKU único, nombre, categoría, estatus (Borrador/Activo/Inactivo), stock mínimo, atributos de calidad (color, olor, viscosidad, PPM, biodegradabilidad), forma de uso, puntos de venta (Ruta, Vendss Clean, Arpelab, Vending) e imagen (validada como PNG/JPEG/WebP real, máximo 5 MB y 20 megapíxeles, para evitar archivos disfrazados).

## Recetas

Las recetas conectan un producto terminado con las materias primas (o subrecetas) que lo componen, con un rendimiento y unidad de rendimiento que es lo que permite calcular cuánta materia prima se necesita para producir cierta cantidad de producto. Se importaron con una fuente de origen inmutable (`origen` en JSON) y una revisión editable con control de concurrencia optimista (si dos personas editan la misma receta a la vez, la segunda debe recargar) y bitácora de revisiones con motivo obligatorio. El sistema detecta ciclos (una receta no puede depender de sí misma directa o indirectamente a través de subrecetas).

## Compras e inventario de materia prima

Una orden de compra solo puede levantarse contra condiciones vigentes de proveedores activos y materias primas activas, respetando la compra mínima y congelando el precio al momento de crear la orden (para que un cambio de precio del proveedor después no afecte órdenes ya emitidas). La recepción es parcial o total, exige lote y caducidad cuando la materia prima lo requiere, exige referencia de COA cuando aplica, rechaza materia ya vencida y usa actualización optimista de versión para que dos recepciones simultáneas no se pisen. Cada recepción aceptada genera un movimiento real de inventario. Aparte de las recepciones de compra, el módulo de inventario permite registrar saldos iniciales y ajustes manuales (entrada/salida) por materia prima, ubicación, lote y ambiente (real o demo — los movimientos de demostración nunca se mezclan con el saldo real), todo con motivo y referencia obligatorios.

## CRM: pedidos, reservas, llenado, ruta y entrega

Este es el módulo que materializa la conexión ERP–CRM. Un pedido se crea con cliente (nuevo o existente, buscado por teléfono), domicilio de entrega (con cobertura y coordenadas opcionales) y líneas de producto terminado con cantidad y precio. Al confirmarse, el sistema:

- Si hay producto terminado físico disponible (descontando lo ya reservado por otros pedidos), reserva ese inventario directamente y el pedido pasa a Confirmado.
- Si no hay suficiente producto terminado, busca la receta activa del producto, calcula cuánta materia prima se necesita según el rendimiento de la receta, y si hay suficiente materia prima disponible (también descontando reservas activas de otros pedidos), reserva esas materias primas y genera una orden de llenado pendiente.
- Si tampoco hay materia prima suficiente o el producto no tiene receta configurada, el pedido queda en estado "Pendiente de producción" y no se reserva nada (nunca se generan reservas negativas ni inventario fantasma).

Cuando preparación cierra una orden de llenado, el sistema consume la materia prima reservada como un ajuste de inventario de salida (con referencia a esa orden), da de alta el producto terminado resultante y genera la reserva de producto terminado para ese pedido — es decir, el llenado en el CRM efectivamente mueve inventario real del ERP. De ahí el pedido avanza por los estados operativos (en preparación, listo para entrega), reparto lo pasa a "en ruta" (lo que mueve el producto de almacén a "en tránsito") y finalmente registra la entrega con evidencia obligatoria (foto, firma, OTP o receptor) — solo entonces se descuenta definitivamente el producto terminado y el pedido pasa a Entregado. Cancelar o marcar un pedido como devuelto libera automáticamente cualquier reserva activa. Cada cambio de estado queda en una bitácora de eventos del pedido, y también hay un módulo de cobranza para registrar pagos parciales contra el saldo del pedido, con la opción de exigir que el saldo esté en cero antes de poder prepararlo.

La pantalla `/admin/productos-entregados`, dentro del propio ERP, lista las entregas confirmadas cruzando el movimiento de inventario con la evidencia de entrega registrada por reparto — es una vista del ERP alimentada enteramente por datos que se generaron en el CRM.

## Panel de operaciones (salud del sistema)

`/admin/operaciones` es un panel de diagnóstico que recorre todas las tablas anteriores y reporta bloqueos (por ejemplo, inventario negativo, una reserva activa en un pedido ya cerrado, una línea de pedido sin producto terminado válido), alertas (materia prima sin proveedor principal, sin política de inventario o sin ubicación; producto activo sin receta o con receta sin rendimiento; ingrediente de receta sin materia prima ligada) e información general (conteos de proveedores, materias primas activas, productos activos, recetas, órdenes de compra, pedidos y reservas). Es, en la práctica, el punto donde se puede confirmar que ERP y CRM siguen consistentes entre sí sin tener que revisar tabla por tabla.

## Identidad visual compartida

El ERP y el portal del CRM comparten la misma hoja de estilos (`static/style.css`) y el mismo encabezado (`templates/erp_header.html`): paleta NYDS (medianoche `#132441`, chartreuse `#daf561`, powder `#87bbd7`, lapis `#3451a3`), tipografía Inter, tarjetas con radio de 16px y el mismo patrón de pestañas y botones en todas las pantallas. No son dos productos con apariencia distinta que hay que reconciliar visualmente: ya nacen del mismo sistema de diseño.

## Cobertura de pruebas automatizadas

Existen 62 pruebas automatizadas (`unittest`, base de datos SQLite en memoria) que hoy pasan todas, distribuidas en: alta y validación de proveedores (`test_proveedores_mp.py` y los doce criterios de aceptación en `test_proveedores_alta_spec.py`), materias primas y catálogo (dentro de los mismos archivos), compras y recepciones (`test_abastecimiento.py`), inventario y ajustes (`test_inventario.py`), recetas (`test_recetario.py`), producto terminado (`test_productos_terminados.py`), el panel de operaciones (`test_operaciones.py`) y, en particular, el flujo completo de CRM: reserva de componentes → llenado → ruta → entrega; reserva directa de producto terminado existente; entrega directa desde "listo"; pedido que queda pendiente por falta de inventario sin generar negativos; y la separación de permisos entre roles del CRM y el ERP (`test_crm.py`). Esta suite es, hoy, la evidencia más confiable de qué es lo que realmente funciona.

## Lo que todavía no está conectado

Quedan fuera del alcance actual — y no bloquean nada de lo anterior — las integraciones externas del CRM que se discutieron como fase futura: captura automática de conversaciones de WhatsApp, Instagram y Facebook como canal de entrada de pedidos, integración con Google Maps para ruteo real de reparto (hoy la cobertura y las coordenadas del domicilio se capturan pero no se usan para calcular rutas), y notificaciones automáticas al cliente o al colaborador asignado. También sigue pendiente ampliar las plantillas Jinja de alta y edición de proveedor (`templates/index.html` y `templates/editar.html`) para exponer en la interfaz todos los campos nuevos que el backend ya soporta — hoy esos formularios siguen mostrando la versión simple original, mientras que el modelo de datos y la API ya están preparados para la ficha completa.

## Pendiente fuera del código

Sigue abierta la limpieza de datos sensibles en el historial de git del repositorio (base de datos real y un PDF subido que quedaron versionados) y la decisión sobre la visibilidad del repositorio en GitHub — esto requiere que tú ejecutes los comandos de git desde tu máquina, ya que esta sesión no tiene acceso de shell a tu equipo.
