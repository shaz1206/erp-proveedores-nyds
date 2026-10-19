"""Respaldo diario de la base: una copia por dia, solo las ultimas N, sin tocar datos reales."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import test_proveedores_mp  # noqa: F401  (prepara la ruta de importacion)
from proveedores.respaldo import hacer_respaldo, registrar_respaldo


class RespaldoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raiz = Path(self.tmp.name)
        (raiz / "instance").mkdir()
        self.base = raiz / "instance" / "erp.db"
        with closing(sqlite3.connect(self.base)) as con:
            with con:
                con.execute("create table t (x text)")
                con.execute("insert into t values ('dato real')")
        self.copias = raiz / "backups"

    def tearDown(self):
        self.tmp.cleanup()

    def test_una_copia_por_dia_y_legible(self):
        hoy = date(2026, 10, 17)
        creada = hacer_respaldo(self.base, self.copias, hoy)
        self.assertEqual(creada.name, "erp-diario-20261017.db")
        self.assertIsNone(hacer_respaldo(self.base, self.copias, hoy))  # el mismo dia no se repite
        with closing(sqlite3.connect(creada)) as con:
            self.assertEqual(con.execute("select x from t").fetchone()[0], "dato real")
        self.assertEqual([p.suffix for p in self.copias.iterdir()], [".db"])  # sin temporales

    def test_conserva_solo_las_ultimas(self):
        for dia in range(1, 6):
            hacer_respaldo(self.base, self.copias, date(2026, 10, dia), mantener=3)
        self.assertEqual([p.name for p in sorted(self.copias.glob("*.db"))],
                         ["erp-diario-20261003.db", "erp-diario-20261004.db", "erp-diario-20261005.db"])

    def test_sin_base_no_falla_y_memoria_se_ignora(self):
        self.assertIsNone(hacer_respaldo(self.tmp.name + "/no-existe.db", self.copias))
        from flask import Flask
        memoria = Flask(__name__); memoria.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        registrar_respaldo(memoria)
        self.assertEqual(memoria.before_request_funcs, {})

    def test_primer_uso_del_dia_crea_el_respaldo(self):
        from flask import Flask
        web = Flask(__name__)
        web.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + self.base.as_posix()
        web.add_url_rule("/", "inicio", lambda: "ok")
        registrar_respaldo(web)
        self.assertEqual(web.test_client().get("/").status_code, 200)
        self.assertEqual(len(list(self.copias.glob("erp-diario-*.db"))), 1)


if __name__ == "__main__":
    unittest.main()
