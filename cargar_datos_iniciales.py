"""Carga inicial de catalogos NYDS en un ERP nuevo o casi vacio.

Copia proveedores, materias primas (con sus condiciones de compra), productos
terminados con sus presentaciones, recetas y listas de precios desde
data/carga_inicial/datos_iniciales.json.

Reglas de seguridad:
- Solo AGREGA. Nunca modifica ni borra lo que ya existe (clientes, pedidos,
  personas, compras, inventarios, ni productos o proveedores ya capturados).
- Si un registro ya existe (mismo SKU, nombre o clave) lo deja como esta y
  enlaza lo nuevo a el. Se puede correr varias veces sin duplicar.
- Por omision NO escribe nada (prueba en seco). Para guardar: --aplicar.
- Antes de guardar hace un respaldo de la base SQLite en backups/.
- Todo ocurre en una sola transaccion: si algo falla, no se guarda nada.

Uso (desde la carpeta del proyecto, con el entorno del ERP):
    python cargar_datos_iniciales.py              # prueba en seco: solo informa
    python cargar_datos_iniciales.py --aplicar    # guarda
    python cargar_datos_iniciales.py --exportar   # (desarrollo) regenera el JSON desde la base local
"""
import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import inspect, text

RUTA_JSON = Path(__file__).resolve().parent / "data" / "carga_inicial" / "datos_iniciales.json"

# Tablas "raiz": se identifican por una clave natural y se reutilizan si ya existen.
# excluir: (columna, prefijo) -> filas de demostracion que no se copian.
RAICES = [
    {"tabla": "proveedores", "clave": ["razon_social"], "excluir": ("razon_social", "DEMO")},
    {"tabla": "materias_primas", "clave": ["nombre_normalizado"]},
    {"tabla": "productos_terminados", "clave": ["sku"], "excluir": ("sku", "DEMO")},
    {"tabla": "recetas_borrador", "clave": ["clave_origen"]},
    {"tabla": "listas_precios", "clave": ["nombre"]},
]
# Tablas hijas: solo se copian cuando su registro padre es NUEVO en esta corrida.
HIJAS = [
    ("materia_prima_proveedor", "id_mp", "materias_primas"),
    ("pt_presentaciones", "producto_id", "productos_terminados"),
    ("receta_ingredientes", "receta_id", "recetas_borrador"),
    ("receta_revisiones", "receta_id", "recetas_borrador"),
    ("listas_precios_items", "lista_id", "listas_precios"),
]
TABLAS = [r["tabla"] for r in RAICES] + [h[0] for h in HIJAS]


def _pk(inspector, tabla):
    return inspector.get_pk_constraint(tabla)["constrained_columns"][0]


def _fks(inspector, tabla):
    return {fk["constrained_columns"][0]: fk["referred_table"] for fk in inspector.get_foreign_keys(tabla)}


def exportar(ruta_base):
    """Lee la base local y escribe el JSON de carga (sin datos DEMO)."""
    con = sqlite3.connect(ruta_base)
    con.row_factory = sqlite3.Row
    salida = {"generado": datetime.now().isoformat(timespec="seconds"), "tablas": {}}
    for tabla in TABLAS:
        salida["tablas"][tabla] = [dict(r) for r in con.execute(f'select * from "{tabla}" order by 1')]
    RUTA_JSON.parent.mkdir(parents=True, exist_ok=True)
    RUTA_JSON.write_text(json.dumps(salida, ensure_ascii=False, indent=1), encoding="utf-8")
    return {t: len(f) for t, f in salida["tablas"].items()}


def cargar(engine, datos, aplicar=False):
    """Inserta lo que falte. Devuelve {tabla: (nuevos, ya_existian, omitidos)}."""
    if engine.dialect.name != "sqlite":
        raise RuntimeError("Este script solo esta probado con SQLite. Base detectada: " + engine.dialect.name)
    mapas = {t: {} for t in TABLAS}     # id viejo -> id en esta base
    nuevos_ids = {t: set() for t in TABLAS}
    resumen = {}
    avisos = []

    with engine.connect() as con:
        inspector = inspect(con)        # sobre la misma conexion (no abre otra que cierre la transaccion)
        faltan = [t for t in TABLAS if t not in set(inspector.get_table_names())]
        if faltan:
            raise RuntimeError("Faltan tablas en la base: " + ", ".join(faltan)
                               + ". Inicia una vez la version nueva del ERP (crea las tablas) y vuelve a correr.")
        tx = con.begin() if not con.in_transaction() else con.get_transaction()
        try:
            def columnas(tabla):
                return {c["name"] for c in inspector.get_columns(tabla)}

            def completar(tabla, fila):
                """La base nueva puede exigir (NOT NULL) campos que la local dejo vacios."""
                ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                for c in inspector.get_columns(tabla):
                    if c["name"] in fila and fila[c["name"]] is None and not c["nullable"]:
                        tipo = str(c["type"]).upper()
                        fila[c["name"]] = ahora if ("DATE" in tipo or "TIME" in tipo) else ("" if ("CHAR" in tipo or "TEXT" in tipo) else 0)
                return fila

            def insertar(tabla, fila, pk):
                fila = completar(tabla, fila)
                cols = [c for c in fila if c != pk and c in columnas(tabla)]
                perdidas = [c for c in fila if c != pk and c not in columnas(tabla)]
                if perdidas and (tabla, tuple(perdidas)) not in {(a[0], a[1]) for a in avisos}:
                    avisos.append((tabla, tuple(perdidas)))
                sql = f'insert into "{tabla}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) values ({", ".join(":" + c for c in cols)})'
                return con.execute(text(sql), {c: fila[c] for c in cols}).lastrowid

            for raiz in RAICES:
                tabla, clave = raiz["tabla"], raiz["clave"]
                pk, fks = _pk(inspector, tabla), _fks(inspector, tabla)
                nuevos = ya = omitidos = 0
                for fila in datos["tablas"][tabla]:
                    if "excluir" in raiz and str(fila.get(raiz["excluir"][0]) or "").upper().startswith(raiz["excluir"][1]):
                        omitidos += 1
                        continue
                    fila = dict(fila)
                    viejo = fila[pk]
                    for col, destino in fks.items():       # recetas -> productos
                        if fila.get(col) is not None:
                            if fila[col] not in mapas.get(destino, {}):
                                fila[col] = None            # producto omitido o nunca cargado
                            else:
                                fila[col] = mapas[destino][fila[col]]
                    donde = " and ".join(f'"{c}" = :{c}' for c in clave)
                    hallado = con.execute(text(f'select "{pk}" from "{tabla}" where {donde}'), {c: fila[c] for c in clave}).fetchone()
                    if hallado:
                        mapas[tabla][viejo] = hallado[0]
                        ya += 1
                        continue
                    mapas[tabla][viejo] = insertar(tabla, fila, pk)
                    nuevos_ids[tabla].add(mapas[tabla][viejo])
                    nuevos += 1
                resumen[tabla] = (nuevos, ya, omitidos)

            for tabla, col_padre, tabla_padre in HIJAS:
                pk, fks = _pk(inspector, tabla), _fks(inspector, tabla)
                nuevos = omitidos = 0
                for fila in datos["tablas"][tabla]:
                    fila = dict(fila)
                    viejo = fila[pk]
                    padre_nuevo = mapas[tabla_padre].get(fila[col_padre])
                    if padre_nuevo is None or padre_nuevo not in nuevos_ids[tabla_padre]:
                        omitidos += 1              # padre omitido (demo) o que ya existia: no se toca
                        continue
                    ok = True
                    for col, destino in fks.items():
                        if fila.get(col) is None:
                            continue
                        nuevo = mapas.get(destino, {}).get(fila[col])
                        if nuevo is None:
                            ok = False
                            break
                        fila[col] = nuevo
                    if not ok:
                        omitidos += 1
                        continue
                    mapas[tabla][viejo] = insertar(tabla, fila, pk)
                    nuevos += 1
                resumen[tabla] = (nuevos, 0, omitidos)

            if aplicar:
                tx.commit()
            else:
                tx.rollback()
        except Exception:
            tx.rollback()
            raise
    return resumen, avisos


def main():
    parser = argparse.ArgumentParser(description="Carga inicial de catalogos NYDS (solo agrega).")
    parser.add_argument("--aplicar", action="store_true", help="guardar los cambios (sin esto solo es una prueba)")
    parser.add_argument("--exportar", action="store_true", help="regenerar el JSON desde la base local (desarrollo)")
    args = parser.parse_args()

    try:
        from .vendssback import app, db
    except ImportError:
        from vendssback import app, db

    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if args.exportar:
        ruta = uri.replace("sqlite:///", "", 1)
        print("Exportado desde", ruta, json.dumps(exportar(ruta), ensure_ascii=False))
        return 0

    datos = json.loads(RUTA_JSON.read_text(encoding="utf-8"))
    print("Base de datos:", uri)
    print("Modo:", "APLICAR (guarda)" if args.aplicar else "PRUEBA EN SECO (no guarda nada)")
    with app.app_context():
        if args.aplicar and uri.startswith("sqlite:///"):
            origen = Path(uri.replace("sqlite:///", "", 1))
            if origen.is_file():
                destino = origen.parent.parent / "backups" / f"erp-antes-carga-inicial-{datetime.now():%Y%m%d-%H%M%S}.db"
                destino.parent.mkdir(exist_ok=True)
                with sqlite3.connect(origen.resolve().as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(destino) as dst:
                    src.backup(dst)
                print("Respaldo creado:", destino)
        resumen, avisos = cargar(db.engine, datos, aplicar=args.aplicar)
    print(f"{'Tabla':32} {'nuevos':>7} {'ya existian':>12} {'omitidos':>9}")
    for tabla, (n, ya, om) in resumen.items():
        print(f"{tabla:32} {n:>7} {ya:>12} {om:>9}")
    for tabla, cols in avisos:
        print(f"AVISO: la tabla {tabla} no tiene las columnas {', '.join(cols)}; se cargo sin ellas (actualiza el ERP primero).")
    print("Listo." if args.aplicar else "Prueba terminada. Para guardar, corre de nuevo con --aplicar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
