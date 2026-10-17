"""Catalogo de envases."""
import unittest
from decimal import Decimal

from test_crm import CRMTest
from proveedores.vendssback import Envase, PresentacionPT, Proveedor, db


class EnvasesTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown

    def preparar(self):
        self.p5 = PresentacionPT(producto_id=self.pt.id, nombre="5 L", capacidad=5, precio_base=100)
        self.p20 = PresentacionPT(producto_id=self.pt.id, nombre="20 L", capacidad=20, precio_base=300)
        self.prov = Proveedor(codigo_proveedor="PE", razon_social="Plasticos SA", rfc="RFC-PE", tipo_proveedor="Envases", estatus="Activo",
                              calle_numero="C", colonia="C", codigo_postal="77500", condicion_pago="Contado", tiempo_entrega_normal=1)
        self.inactivo = Proveedor(codigo_proveedor="PI", razon_social="Inactivo SA", rfc="RFC-PI", tipo_proveedor="Envases", estatus="Inactivo",
                                  calle_numero="C", colonia="C", codigo_postal="77500", condicion_pago="Contado", tiempo_entrega_normal=1)
        db.session.add_all([self.p5, self.p20, self.prov, self.inactivo])
        db.session.commit()

    def csrf(self):
        self.client.get("/admin/envases")
        with self.client.session_transaction() as s:
            return s["envases_csrf"]

    def crear(self, **extra):
        datos = dict(csrf=self.csrf(), nombre="Bote 5 L", material="HDPE", capacidad="5", color="Blanco", precio="12.50")
        datos.update(extra)
        return self.client.post("/admin/envases/nuevo", data=datos)

    def test_exige_login(self):
        anonimo = self.client.application.test_client()
        for ruta in ("/admin/envases", "/admin/envases/nuevo", "/admin/envases/1"):
            self.assertEqual(anonimo.get(ruta).status_code, 302, ruta)
        self.assertEqual(anonimo.post("/admin/envases/nuevo", data={}).status_code, 302)

    def test_paginas_cargan(self):
        self.preparar()
        self.assertEqual(self.client.get("/admin/envases").status_code, 200)
        r = self.client.get("/admin/envases/nuevo")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Plasticos SA", r.data)
        self.assertIn(b'href="/admin/envases"', r.data)

    def test_sin_csrf_rechazado(self):
        r = self.client.post("/admin/envases/nuevo", data=dict(nombre="x", material="PET", capacidad="1"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Envase.query.count(), 0)

    def test_crear_con_sku_automatico_y_liga(self):
        self.preparar()
        r = self.crear(proveedor_id=self.prov.id_proveedor, mp_id=self.envase.id_mp, presentaciones=[self.p5.id])
        self.assertEqual(r.status_code, 302)
        e = Envase.query.one()
        self.assertEqual(e.sku, "ENV-000001")
        self.assertEqual(e.precio, Decimal("12.50"))
        self.assertEqual(e.proveedor.razon_social, "Plasticos SA")
        self.assertEqual(e.mp_id, self.envase.id_mp)
        self.assertEqual([p.id for p in e.presentaciones], [self.p5.id])
        self.crear(nombre="Garrafa 20 L", capacidad="20")
        self.assertEqual(Envase.query.filter_by(nombre="Garrafa 20 L").one().sku, "ENV-000002")
        html = self.client.get("/admin/envases").get_data(as_text=True)
        self.assertIn("ENV-000001", html)
        self.assertIn("5 L", html)

    def test_validaciones(self):
        self.preparar()
        malos = [dict(nombre=""), dict(material="Madera"), dict(capacidad="0"), dict(capacidad="abc"), dict(capacidad="5.1234"),
                 dict(precio="-1"), dict(precio="1.234"), dict(proveedor_id=self.inactivo.id_proveedor), dict(proveedor_id="9999"),
                 dict(mp_id="9999"), dict(color="x" * 61), dict(presentaciones=[self.p20.id]), dict(presentaciones=[9999])]
        for datos in malos:
            r = self.crear(**datos)
            self.assertEqual(r.status_code, 400, datos)
        self.assertEqual(Envase.query.count(), 0)

    def test_precio_opcional(self):
        self.preparar()
        self.crear(precio="")
        self.assertIsNone(Envase.query.one().precio)

    def test_editar_y_cambiar_capacidad_exige_presentaciones_validas(self):
        self.preparar()
        self.crear(presentaciones=[self.p5.id])
        e = Envase.query.one()
        csrf = self.csrf()
        r = self.client.post(f"/admin/envases/{e.id}", data=dict(csrf=csrf, nombre="Bote 20 L", material="PP", capacidad="20", activo="1",
                                                                  presentaciones=[self.p5.id]))
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"/admin/envases/{e.id}", data=dict(csrf=csrf, nombre="Bote 20 L", material="PP", capacidad="20", activo="1",
                                                                  presentaciones=[self.p20.id]))
        self.assertEqual(r.status_code, 302)
        db.session.refresh(e)
        self.assertEqual((e.nombre, e.material, e.capacidad, e.sku), ("Bote 20 L", "PP", Decimal("20"), "ENV-000001"))
        self.assertEqual([p.id for p in e.presentaciones], [self.p20.id])

    def test_editar_inexistente(self):
        self.assertEqual(self.client.get("/admin/envases/999").status_code, 404)

    def test_activar_desactivar_y_filtros(self):
        self.preparar()
        self.crear()
        self.crear(nombre="Botella PET 1 L", material="PET", capacidad="1", color="Transparente")
        e = Envase.query.filter_by(nombre="Bote 5 L").one()
        self.client.post(f"/admin/envases/{e.id}/estado", data=dict(csrf=self.csrf()))
        db.session.refresh(e)
        self.assertFalse(e.activo)
        self.assertEqual(self.client.post(f"/admin/envases/{e.id}/estado", data={}).status_code, 400)
        html = self.client.get("/admin/envases?estado=activos").get_data(as_text=True)
        self.assertNotIn("Bote 5 L", html)
        self.assertIn("Botella PET 1 L", html)
        self.assertIn("Bote 5 L", self.client.get("/admin/envases?estado=inactivos").get_data(as_text=True))
        self.assertIn("Botella PET 1 L", self.client.get("/admin/envases?material=PET").get_data(as_text=True))
        self.assertNotIn("Botella PET 1 L", self.client.get("/admin/envases?q=Bote+5").get_data(as_text=True))
        self.assertIn("Transparente", self.client.get("/admin/envases?q=transp").get_data(as_text=True))

    def test_edicion_conserva_proveedor_que_luego_se_inactivo(self):
        self.preparar()
        self.crear(proveedor_id=self.prov.id_proveedor)
        self.prov.estatus = "Inactivo"
        db.session.commit()
        e = Envase.query.one()
        r = self.client.post(f"/admin/envases/{e.id}", data=dict(csrf=self.csrf(), nombre="Bote 5 L v2", material="HDPE", capacidad="5",
                                                                  proveedor_id=self.prov.id_proveedor, activo="1"))
        self.assertEqual(r.status_code, 302)


if __name__ == "__main__":
    unittest.main()
