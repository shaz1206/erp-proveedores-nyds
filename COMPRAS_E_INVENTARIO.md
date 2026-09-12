# Compras, recepción e inventario de materias primas

## Puesta en marcha
Reiniciar el servidor Flask para registrar las rutas y crear las cuatro tablas nuevas mediante `db.create_all()`. No se modifican columnas ni registros de los catálogos existentes. Recargar el navegador.

## Uso
1. Activar el proveedor y la materia prima. Configurar una condición de compra vigente con precio, presentación, unidad y conversión.
2. Abrir **Compras → Nueva orden de compra**, elegir proveedor y agregar partidas. Una orden admite una sola moneda. Revisar total y fecha; emitir guarda la orden internamente, sin enviarla al proveedor.
3. Abrir la orden y registrar cada partida recibida: cantidades aceptadas/rechazadas en presentaciones, ubicación y remisión. Indicar motivo si hay rechazo y confirmar revisión de calidad si hay aceptación. Capturar lote, caducidad y referencia de COA cuando sean obligatorios.
4. Consultar **Inventario MP**: cada aceptación genera una entrada en unidad base, trazable a recepción y orden. Ejemplo: 3 sacos de 25 kg aceptados generan 75 kg; 1 saco rechazado no genera existencia.

## Reglas
- Precios, conversión, unidad e IVA se congelan al emitir; cambios posteriores del catálogo no alteran la orden.
- Se valida vigencia, proveedor y materia prima activos, mínimos de compra, moneda única y cantidades.
- Entregas parciales admitidas; aceptadas más rechazadas no pueden superar la cantidad pendiente.
- Las cantidades rechazadas cierran esa porción de la entrega. Una reposición requiere otra orden. Al completar, el estado es Recibida o Cerrada con rechazo.
- Solo se cancelan órdenes sin recepciones. Recepciones y entradas son inmutables en esta versión.
- Transacción única para recepción y entrada, token contra reenvíos y control de versión frente a actualizaciones concurrentes.
- Acceso con la sesión administrativa existente; formularios nuevos con protección CSRF.

## Alcance de esta etapa
El inventario refleja las entradas capturadas aquí. No incluye saldos iniciales, consumos, salidas, ajustes, transferencias, devoluciones ni producción. El total de orden incluye IVA, pero no flete ni otros cargos; no es un costeo de internación. El control de calidad registra la conformidad y referencia documental; no sustituye un módulo de laboratorio o cuarentena. Las ubicaciones se capturan como texto.

## Verificación
`python -m unittest test_abastecimiento test_proveedores_mp test_productos_terminados`
Las pruebas usan una base SQLite en memoria. La revisión visual cubre cuatro pantallas en 1440 y 390 px y el cálculo de total/agregado de partidas.
