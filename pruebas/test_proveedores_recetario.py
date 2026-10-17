import unittest

from test_proveedores_mp import app, db, Proveedor
from proveedores.proveedores_recetario import proveedores_del_recetario


class ProveedoresRecetarioTest(unittest.TestCase):
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

    def test_extrae_solo_proveedores_reales(self):
        nombres = set(proveedores_del_recetario())
        self.assertEqual(nombres, {"La Fórmula", "Clean Chemical", "DCC Esencias", "Departamental", "Power Clean", "Mercado Libre", "Pochteca"})
        self.assertNotIn("Propio", nombres)

    def test_crea_borradores_sin_inventar_datos_y_es_idempotente(self):
        r = self.client.get("/api/proveedores/del-recetario").get_json()
        self.assertEqual(len(r["pendientes"]), 7)
        creado = self.client.post("/api/proveedores/del-recetario")
        self.assertEqual(creado.status_code, 201)
        self.assertEqual(Proveedor.query.count(), 7)
        p = Proveedor.query.filter_by(razon_social="La Fórmula").one()
        self.assertEqual(p.estatus, "Borrador")
        self.assertIn("ABDS", p.notas_generales)
        self.assertEqual(p.campos_minimos_faltantes(), [])
        # se aprueba directo: el telefono y el RFC son opcionales
        r = self.client.patch(f"/api/proveedores/{p.id_proveedor}/estatus", json={"estatus": "Activo"})
        self.assertEqual(r.status_code, 200, r.get_json())
        # el telefono se puede agregar despues
        vacio = self.client.patch(f"/api/proveedores/{p.id_proveedor}/telefono", json={"telefono": "  "})
        self.assertEqual(vacio.status_code, 400)
        guardado = self.client.patch(f"/api/proveedores/{p.id_proveedor}/telefono", json={"telefono": "9981234567"})
        self.assertEqual(guardado.status_code, 200, guardado.get_json())
        # segunda vez: no duplica
        self.assertEqual(self.client.get("/api/proveedores/del-recetario").get_json()["pendientes"], [])
        self.client.post("/api/proveedores/del-recetario")
        self.assertEqual(Proveedor.query.count(), 7)

    def test_pide_sesion(self):
        anonimo = app.test_client()
        self.assertEqual(anonimo.get("/api/proveedores/del-recetario").status_code, 403)


if __name__ == "__main__":
    unittest.main()
