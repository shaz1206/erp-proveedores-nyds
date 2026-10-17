"""Respaldo automatico de la base de datos.

Una vez al dia (con el primer uso del ERP) guarda una copia consistente de
instance/erp.db en la carpeta backups/ y conserva solo las ultimas 14. Si algo
sale mal, el ERP sigue funcionando: un respaldo fallido nunca interrumpe una
peticion.
"""
import sqlite3
import threading
from contextlib import closing
from datetime import date
from pathlib import Path

MANTENER = 14
_candado = threading.Lock()


def hacer_respaldo(origen, carpeta, hoy=None, mantener=MANTENER):
    """Copia `origen` en carpeta/erp-diario-AAAAMMDD.db (una por dia).

    Devuelve la ruta creada, o None si ya habia respaldo de hoy o no hay base.
    """
    origen, carpeta = Path(origen), Path(carpeta)
    hoy = hoy or date.today()
    if not origen.is_file():
        return None
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / f"erp-diario-{hoy:%Y%m%d}.db"
    if destino.exists():
        return None
    temporal = destino.with_suffix(".tmp")
    try:
        with closing(sqlite3.connect(origen.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(temporal)) as dest:
                src.backup(dest)
        temporal.replace(destino)
    finally:
        if temporal.exists():
            temporal.unlink()
    for viejo in sorted(carpeta.glob("erp-diario-*.db"))[:-mantener]:
        viejo.unlink()
    return destino


def registrar_respaldo(app, carpeta=None):
    """Hace el respaldo del dia en la primera peticion de cada dia."""
    from sqlalchemy.engine import make_url

    try:
        ruta = make_url(app.config["SQLALCHEMY_DATABASE_URI"]).database
    except Exception:
        ruta = None
    if not ruta or ruta == ":memory:":
        return  # base en memoria (pruebas) u otro motor: no hay archivo que copiar
    origen = Path(ruta)
    destino = Path(carpeta) if carpeta else origen.parent.parent / "backups"
    estado = {"dia": None}

    @app.before_request
    def respaldo_diario():
        hoy = date.today()
        if estado["dia"] == hoy:
            return None
        with _candado:
            if estado["dia"] == hoy:
                return None
            estado["dia"] = hoy
            try:
                hacer_respaldo(origen, destino, hoy)
            except Exception as error:  # nunca romper una peticion por el respaldo
                app.logger.warning("Respaldo diario fallido: %s", error)
        return None
