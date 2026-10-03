"""El login del ERP debe distinguir claramente exito de fracaso, incluso
cuando el formulario se envia via fetch (no JSON) como hace login.html.
Un fallo que responda 200 hace que el navegador crea que si entro, y el
usuario queda atorado rebotando entre /login y /admin/catalogo sin ver
ningun mensaje de error."""
import os
import unittest

from test_proveedores_mp import app, db


class LoginAdminTest(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()
        os.environ["NYDS_ADMIN_PASSWORD"] = "clave-de-prueba"
        os.environ.setdefault("NYDS_ADMIN_USER", "compras.nyds")

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_credenciales_correctas_deja_entrar_y_la_sesion_persiste(self):
        respuesta = self.client.post(
            "/login",
            data={"usuario": "compras.nyds", "password": "clave-de-prueba"},
            follow_redirects=False,
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(respuesta.headers["Location"], "/admin/catalogo")
        # La sesion debe seguir siendo valida en la siguiente peticion.
        self.assertEqual(self.client.get("/admin/catalogo").status_code, 200)

    def test_credenciales_incorrectas_no_finge_exito_por_formulario(self):
        # login.html envia el formulario con fetch + URLSearchParams (no JSON),
        # y solo redirige si res.ok. Un 200 aqui esconde el error para siempre.
        respuesta = self.client.post(
            "/login",
            data={"usuario": "compras.nyds", "password": "incorrecta"},
        )
        self.assertEqual(respuesta.status_code, 401)
        self.assertIn(b"Credenciales incorrectas", respuesta.data)
        # Y no debe haber quedado sesion abierta.
        self.assertEqual(self.client.get("/admin/catalogo").status_code, 302)

    def test_credenciales_incorrectas_via_json_tambien_da_401(self):
        respuesta = self.client.post(
            "/login",
            json={"usuario": "compras.nyds", "password": "incorrecta"},
        )
        self.assertEqual(respuesta.status_code, 401)
        self.assertEqual(respuesta.get_json()["error"], "Credenciales incorrectas")

    def test_password_por_defecto_nyds2026_funciona_sin_variable_de_entorno(self):
        # Si nadie definio NYDS_ADMIN_PASSWORD en esta terminal, "nyds2026"
        # debe seguir sirviendo como contraseña de respaldo.
        os.environ.pop("NYDS_ADMIN_PASSWORD", None)
        respuesta = self.client.post(
            "/login",
            data={"usuario": "compras.nyds", "password": "nyds2026"},
            follow_redirects=False,
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(respuesta.headers["Location"], "/admin/catalogo")


if __name__ == "__main__":
    unittest.main()
