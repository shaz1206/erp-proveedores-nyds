"""Acceso por persona: cada quien entra con su propio usuario y contraseña,
en vez de las cuentas compartidas de antes (un solo admin del ERP, 5 logins
de equipo del CRM). Cubre la pantalla de administración de personas y que
el login del ERP y del CRM autentiquen contra la misma tabla."""
import os
import unittest

from test_crm import CRMTest
from test_proveedores_mp import app, db
from proveedores.vendssback import Persona


class PersonasTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown

    def crear_persona(self, **overrides):
        payload = {
            "nombre": "Persona de prueba",
            "usuario": "persona.prueba",
            "password": "clave-segura",
            "rol": "ventas",
        }
        payload.update(overrides)
        return self.client.post("/api/personas", json=payload)

    def test_pantalla_y_api_de_personas_exigen_sesion_de_supervisor(self):
        with self.client.session_transaction() as sesion:
            sesion.clear()
        self.assertEqual(self.client.get("/admin/personas").status_code, 302)
        self.assertEqual(self.client.get("/api/personas").status_code, 403)
        self.assertEqual(self.client.post("/api/personas", json={}).status_code, 403)

    def test_crear_persona_valida_datos_y_aparece_en_el_listado(self):
        respuesta = self.crear_persona()
        self.assertEqual(respuesta.status_code, 201, respuesta.get_json())

        listado = self.client.get("/api/personas").get_json()
        usuarios = {p["usuario"]: p for p in listado}
        self.assertIn("persona.prueba", usuarios)
        self.assertEqual(usuarios["persona.prueba"]["rol"], "ventas")
        self.assertTrue(usuarios["persona.prueba"]["activo"])

    def test_crear_persona_rechaza_usuario_duplicado_password_corta_y_rol_invalido(self):
        self.assertEqual(self.crear_persona().status_code, 201)

        duplicado = self.crear_persona(nombre="Otra persona")
        self.assertEqual(duplicado.status_code, 400)
        self.assertIn("usuario", " ".join(duplicado.get_json()["detalles"]).lower())

        password_corta = self.crear_persona(usuario="otra.persona", password="123")
        self.assertEqual(password_corta.status_code, 400)

        rol_invalido = self.crear_persona(usuario="otra.persona2", rol="gerente")
        self.assertEqual(rol_invalido.status_code, 400)

    def test_persona_recien_creada_puede_iniciar_sesion_en_crm_segun_su_rol(self):
        self.crear_persona(usuario="reparto.ana", rol="reparto", password="clave-segura")

        with self.client.session_transaction() as sesion:
            sesion.clear()
        login = self.client.post("/crm/login", data={"usuario": "reparto.ana", "password": "clave-segura"})
        self.assertEqual(login.status_code, 302)
        self.assertEqual(self.client.get("/crm/pedidos").status_code, 200)
        # El rol "reparto" sigue sin acceso al ERP, igual que antes.
        self.assertEqual(self.client.get("/admin/catalogo").status_code, 302)

    def test_solo_rol_supervisor_puede_entrar_al_erp(self):
        # Se crean ambas personas primero, mientras la sesión todavía tiene
        # admin_logueado (de setUp), que es lo que exige /api/personas.
        self.assertEqual(self.crear_persona(usuario="ventas.luis", rol="ventas", password="clave-segura").status_code, 201)
        self.assertEqual(self.crear_persona(usuario="super.nueva", rol="supervisor", password="clave-segura").status_code, 201)

        with self.client.session_transaction() as sesion:
            sesion.clear()
        respuesta = self.client.post("/login", data={"usuario": "ventas.luis", "password": "clave-segura"})
        self.assertEqual(respuesta.status_code, 401)

        with self.client.session_transaction() as sesion:
            sesion.clear()
        respuesta = self.client.post("/login", data={"usuario": "super.nueva", "password": "clave-segura"})
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(self.client.get("/admin/catalogo").status_code, 200)

    def test_desactivar_persona_le_quita_el_acceso_de_inmediato(self):
        self.crear_persona(usuario="ventas.tere", rol="ventas", password="clave-segura")
        persona = Persona.query.filter_by(usuario="ventas.tere").one()

        patch = self.client.patch(f"/api/personas/{persona.id}", json={"activo": False})
        self.assertEqual(patch.status_code, 200)

        with self.client.session_transaction() as sesion:
            sesion.clear()
        login = self.client.post("/crm/login", data={"usuario": "ventas.tere", "password": "clave-segura"})
        self.assertEqual(login.status_code, 401)

    def test_no_se_puede_desactivar_ni_quitar_rol_al_ultimo_supervisor_activo(self):
        # self.pt / setUp deja la sesion como admin_logueado (equivalente a
        # supervisor), pero no hay ninguna fila Persona todavia: se crea una
        # sola para representar al supervisor actual y probar el bloqueo.
        unico = Persona(nombre="Único supervisor", usuario="unico.super",
                         password_hash="x", rol="supervisor", activo=True)
        db.session.add(unico)
        db.session.commit()

        sin_desactivar = self.client.patch(f"/api/personas/{unico.id}", json={"activo": False})
        self.assertEqual(sin_desactivar.status_code, 400)
        self.assertIn("supervisor", " ".join(sin_desactivar.get_json()["detalles"]).lower())

        sin_cambiar_rol = self.client.patch(f"/api/personas/{unico.id}", json={"rol": "ventas"})
        self.assertEqual(sin_cambiar_rol.status_code, 400)

        db.session.refresh(unico)
        self.assertTrue(unico.activo)
        self.assertEqual(unico.rol, "supervisor")

    def test_restablecer_password_invalida_la_contrasena_anterior(self):
        self.crear_persona(usuario="cobranza.mar", rol="cobranza", password="clave-vieja")
        persona = Persona.query.filter_by(usuario="cobranza.mar").one()

        patch = self.client.patch(f"/api/personas/{persona.id}", json={"password": "clave-nueva-1"})
        self.assertEqual(patch.status_code, 200)

        with self.client.session_transaction() as sesion:
            sesion.clear()
        self.assertEqual(
            self.client.post("/crm/login", data={"usuario": "cobranza.mar", "password": "clave-vieja"}).status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/crm/login", data={"usuario": "cobranza.mar", "password": "clave-nueva-1"}).status_code,
            302,
        )


class BootstrapPersonaInicialTest(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()
        os.environ["NYDS_ADMIN_PASSWORD"] = "clave-de-arranque"
        os.environ.setdefault("NYDS_ADMIN_USER", "compras.nyds")

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_base_de_datos_vacia_se_siembra_con_un_supervisor_al_primer_login(self):
        self.assertEqual(Persona.query.count(), 0)
        respuesta = self.client.post(
            "/login",
            data={"usuario": "compras.nyds", "password": "clave-de-arranque"},
        )
        self.assertEqual(respuesta.status_code, 302)
        personas = Persona.query.all()
        self.assertEqual(len(personas), 1)
        self.assertEqual(personas[0].usuario, "compras.nyds")
        self.assertEqual(personas[0].rol, "supervisor")


if __name__ == "__main__":
    unittest.main()
