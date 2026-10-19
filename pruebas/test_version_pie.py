"""Pie NYDS (derechos, equipo) y version del software en ERP, CRM, accesos y reportes de bug."""
import unittest

from test_proveedores_mp import app, db
from proveedores.vendssback import APP_VERSION, APP_VERSION_FECHA


class VersionPieTest(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion["admin_logueado"] = True
            sesion["usuario"] = "compras.nyds"

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def assert_pie(self, html):
        self.assertIn("Todos los derechos reservados", html)
        self.assertIn("Equipo NYDS: CRM ERP", html)
        self.assertIn(f"Versión {APP_VERSION}", html)

    def test_version_tiene_formato_x_y_z(self):
        self.assertRegex(APP_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertRegex(APP_VERSION_FECHA, r"^\d{4}-\d{2}-\d{2}$")

    def test_endpoint_publico_de_version(self):
        r = app.test_client().get("/api/version")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["version"], APP_VERSION)

    def test_pie_en_erp_y_crm(self):
        for ruta in ("/admin/catalogo", "/admin/operaciones", "/crm/tablero", "/crm/clientes"):
            r = self.client.get(ruta)
            self.assertEqual(r.status_code, 200, ruta)
            self.assert_pie(r.get_data(as_text=True))

    def test_pie_en_accesos(self):
        anonimo = app.test_client()
        for ruta in ("/login", "/crm/login"):
            r = anonimo.get(ruta)
            self.assertEqual(r.status_code, 200, ruta)
            self.assert_pie(r.get_data(as_text=True))

    def test_reporte_de_bug_guarda_la_version(self):
        r = self.client.post("/api/reportes-bug", json={"pagina": "/admin/catalogo", "comentario": "Prueba de version"})
        self.assertEqual(r.status_code, 201)
        navegador = db.session.execute(db.text("select navegador from reportes_bug")).scalar()
        self.assertTrue(navegador.endswith(f"NYDS v{APP_VERSION}"))


if __name__ == "__main__":
    unittest.main()
