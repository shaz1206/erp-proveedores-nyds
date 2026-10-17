"""Presentaciones y listas de precios por cliente.

Regla central: el precio de un cliente es el de su lista para esa
presentacion; si la lista no lo trae, el precio base de la presentacion."""
import unittest
from decimal import Decimal

from test_crm import CRMTest
from proveedores.vendssback import (ClienteCRM, ClienteListaPrecios, ListaPrecios, ListaPreciosItem,
                                    PresentacionPT, db, resolver_precio)


class PreciosTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown

    def presentacion(self, nombre="5 L", capacidad="5", base="150.00"):
        resp = self.client.post("/admin/precios/presentaciones", data={
            "producto_id": self.pt.id, "nombre": nombre, "capacidad": capacidad, "precio_base": base})
        self.assertEqual(resp.status_code, 302)
        return PresentacionPT.query.filter_by(producto_id=self.pt.id, nombre=nombre).first()

    def cliente(self, nombre="Bokka"):
        c = ClienteCRM(nombre=nombre, telefono="9981112233")
        db.session.add(c)
        db.session.commit()
        return c

    def lista(self, nombre="Distribuidores"):
        resp = self.client.post("/admin/precios", data={"nombre": nombre})
        self.assertEqual(resp.status_code, 302)
        return ListaPrecios.query.filter_by(nombre=nombre).first()

    # ---- acceso ----
    def test_todo_exige_login(self):
        anonimo = self.client.application.test_client()
        for ruta in ("/admin/precios", "/admin/precios/presentaciones", "/admin/precios/1", "/admin/precios/1/pdf"):
            r = anonimo.get(ruta)
            self.assertEqual(r.status_code, 302, ruta)
            self.assertIn("login", r.headers["Location"])
        self.assertEqual(anonimo.get("/api/precios/producto/1").status_code, 403)

    def test_paginas_cargan(self):
        pres = self.presentacion()
        lista = self.lista()
        self.assertEqual(self.client.get("/admin/precios").status_code, 200)
        self.assertEqual(self.client.get("/admin/precios/presentaciones").status_code, 200)
        r = self.client.get(f"/admin/precios/{lista.id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Detergente color 5 L", r.data)
        self.assertIn(b"Precios", self.client.get("/admin/operaciones").data)

    # ---- presentaciones ----
    def test_presentacion_se_crea_y_no_se_duplica(self):
        self.presentacion()
        self.assertEqual(PresentacionPT.query.count(), 1)
        r = self.client.post("/admin/precios/presentaciones", data={
            "producto_id": self.pt.id, "nombre": "5 l", "capacidad": "5"}, follow_redirects=True)
        self.assertIn(b"ya tiene la presentacion", r.data)
        self.assertEqual(PresentacionPT.query.count(), 1)

    def test_presentacion_rechaza_datos_invalidos(self):
        casos = [
            {"producto_id": self.pt.id, "nombre": "1 L", "capacidad": "0", "precio_base": ""},
            {"producto_id": self.pt.id, "nombre": "1 L", "capacidad": "1", "precio_base": "-5"},
            {"producto_id": self.pt.id, "nombre": "1 L", "capacidad": "1", "precio_base": "10.555"},
            {"producto_id": self.pt.id, "nombre": "1 L", "capacidad": "1", "precio_base": "abc"},
            {"producto_id": 9999, "nombre": "1 L", "capacidad": "1"},
            {"producto_id": self.pt.id, "nombre": "", "capacidad": "1"},
        ]
        for datos in casos:
            self.client.post("/admin/precios/presentaciones", data=datos)
        self.assertEqual(PresentacionPT.query.count(), 0)

    def test_presentaciones_estandar_aprovechan_precio_del_catalogo(self):
        self.pt.presentacion = "5 L"
        self.pt.precio = Decimal("140.00")
        db.session.commit()
        self.client.post(f"/admin/precios/presentaciones/estandar/{self.pt.id}")
        nombres = {p.nombre: p.precio_base for p in PresentacionPT.query.all()}
        self.assertEqual(set(nombres), {"1 L", "5 L", "20 L"})
        self.assertEqual(nombres["5 L"], Decimal("140.00"))
        self.assertIsNone(nombres["1 L"])
        self.client.post(f"/admin/precios/presentaciones/estandar/{self.pt.id}")
        self.assertEqual(PresentacionPT.query.count(), 3)

    def test_editar_precio_base_y_desactivar(self):
        pres = self.presentacion()
        self.client.post(f"/admin/precios/presentaciones/{pres.id}/editar", data={"precio_base": "155.50", "activa": "1"})
        db.session.refresh(pres)
        self.assertEqual(pres.precio_base, Decimal("155.50"))
        self.client.post(f"/admin/precios/presentaciones/{pres.id}/editar", data={"precio_base": "155.50"})
        db.session.refresh(pres)
        self.assertFalse(pres.activa)

    # ---- listas y resolucion de precio ----
    def test_lista_nombre_unico_sin_importar_mayusculas(self):
        self.lista("Mayoreo")
        r = self.client.post("/admin/precios", data={"nombre": "mayoreo"}, follow_redirects=True)
        self.assertIn(b"Ya existe una lista", r.data)
        self.assertEqual(ListaPrecios.query.count(), 1)

    def test_cliente_sin_lista_paga_precio_base(self):
        pres = self.presentacion()
        c = self.cliente()
        self.assertEqual(resolver_precio(pres, c.id), (Decimal("150.00"), "base"))

    def test_cliente_con_lista_paga_precio_de_su_lista(self):
        pres = self.presentacion()
        lista = self.lista()
        c = self.cliente()
        self.client.post(f"/admin/precios/{lista.id}/guardar", data={
            "nombre": lista.nombre, "activa": "1", f"precio_{pres.id}": "120.00"})
        self.client.post(f"/admin/precios/{lista.id}/clientes", data={"cliente_id": c.id})
        self.assertEqual(resolver_precio(pres, c.id), (Decimal("120.00"), "lista"))

    def test_lista_sin_precio_para_la_presentacion_cae_al_base(self):
        pres = self.presentacion()
        lista = self.lista()
        c = self.cliente()
        self.client.post(f"/admin/precios/{lista.id}/guardar", data={"nombre": lista.nombre, "activa": "1"})
        self.client.post(f"/admin/precios/{lista.id}/clientes", data={"cliente_id": c.id})
        self.assertEqual(resolver_precio(pres, c.id), (Decimal("150.00"), "base"))

    def test_lista_inactiva_se_ignora(self):
        pres = self.presentacion()
        lista = self.lista()
        c = self.cliente()
        self.client.post(f"/admin/precios/{lista.id}/guardar", data={
            "nombre": lista.nombre, "activa": "1", f"precio_{pres.id}": "120.00"})
        self.client.post(f"/admin/precios/{lista.id}/clientes", data={"cliente_id": c.id})
        self.client.post(f"/admin/precios/{lista.id}/guardar", data={"nombre": lista.nombre, f"precio_{pres.id}": "120.00"})
        self.assertEqual(resolver_precio(pres, c.id), (Decimal("150.00"), "base"))

    def test_vaciar_precio_en_lista_lo_regresa_al_base(self):
        pres = self.presentacion()
        lista = self.lista()
        self.client.post(f"/admin/precios/{lista.id}/guardar", data={"nombre": lista.nombre, "activa": "1", f"precio_{pres.id}": "120"})
        self.assertEqual(ListaPreciosItem.query.count(), 1)
        self.client.post(f"/admin/precios/{lista.id}/guardar", data={"nombre": lista.nombre, "activa": "1", f"precio_{pres.id}": ""})
        self.assertEqual(ListaPreciosItem.query.count(), 0)

    def test_precio_invalido_en_lista_no_guarda_nada(self):
        pres = self.presentacion()
        lista = self.lista()
        r = self.client.post(f"/admin/precios/{lista.id}/guardar", data={
            "nombre": "Nuevo nombre", "activa": "1", f"precio_{pres.id}": "-3"}, follow_redirects=True)
        self.assertIn(b"mayor a cero", r.data)
        db.session.refresh(lista)
        self.assertEqual(lista.nombre, "Distribuidores")
        self.assertEqual(ListaPreciosItem.query.count(), 0)

    def test_cliente_solo_tiene_una_lista_y_se_puede_quitar(self):
        a, b = self.lista("A"), self.lista("B")
        c = self.cliente()
        self.client.post(f"/admin/precios/{a.id}/clientes", data={"cliente_id": c.id})
        self.client.post(f"/admin/precios/{b.id}/clientes", data={"cliente_id": c.id})
        self.assertEqual(ClienteListaPrecios.query.count(), 1)
        self.assertEqual(ClienteListaPrecios.query.first().lista_id, b.id)
        self.client.post(f"/admin/precios/{b.id}/clientes/{c.id}/quitar")
        self.assertEqual(ClienteListaPrecios.query.count(), 0)

    # ---- API del CRM y PDF ----
    def test_api_devuelve_precio_de_lista_para_el_cliente(self):
        pres = self.presentacion()
        lista = self.lista()
        c = self.cliente()
        self.client.post(f"/admin/precios/{lista.id}/guardar", data={
            "nombre": lista.nombre, "activa": "1", f"precio_{pres.id}": "120.00"})
        self.client.post(f"/admin/precios/{lista.id}/clientes", data={"cliente_id": c.id})
        base = self.client.get(f"/api/precios/producto/{self.pt.id}").get_json()
        self.assertEqual(base["presentaciones"][0]["precio"], 150.0)
        self.assertIsNone(base["lista"])
        con_lista = self.client.get(f"/api/precios/producto/{self.pt.id}?cliente_id={c.id}").get_json()
        self.assertEqual(con_lista["presentaciones"][0]["precio"], 120.0)
        self.assertEqual(con_lista["presentaciones"][0]["origen"], "lista")
        self.assertEqual(con_lista["lista"], "Distribuidores")

    def test_api_acepta_sesion_crm(self):
        anonimo = self.client.application.test_client()
        with anonimo.session_transaction() as s:
            s["crm_logueado"] = True
        self.assertEqual(anonimo.get(f"/api/precios/producto/{self.pt.id}").status_code, 200)

    def test_pdf_de_la_lista(self):
        pres = self.presentacion()
        lista = self.lista()
        r = self.client.get(f"/admin/precios/{lista.id}/pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertTrue(r.data.startswith(b"%PDF"))

    def test_formulario_del_crm_muestra_catalogo_y_carrito(self):
        html = self.client.get("/crm/pedidos").data
        for marca in (b"catalogoVenta", b"carritoVenta", b"buscarProducto", b"/api/precios/catalogo"):
            self.assertIn(marca, html)

    def test_catalogo_para_nuevo_pedido(self):
        anonimo = self.client.application.test_client()
        self.assertEqual(anonimo.get("/api/precios/catalogo").status_code, 403)
        pres = self.presentacion()
        self.presentacion("20 L", "20", "500.00")
        lista = self.lista()
        c = self.cliente()
        self.client.post(f"/admin/precios/{lista.id}/guardar", data={
            "nombre": lista.nombre, "activa": "1", f"precio_{pres.id}": "120.00"})
        self.client.post(f"/admin/precios/{lista.id}/clientes", data={"cliente_id": c.id})
        base = self.client.get("/api/precios/catalogo").get_json()
        self.assertIsNone(base["lista"])
        producto = next(p for p in base["productos"] if p["id"] == self.pt.id)
        self.assertEqual([(x["nombre"], x["precio"]) for x in producto["presentaciones"]], [("5 L", 150.0), ("20 L", 500.0)])
        self.assertEqual(producto["categoria"], "Detergentes")
        self.assertIsNone(producto["imagen"])
        con_lista = self.client.get(f"/api/precios/catalogo?cliente_id={c.id}").get_json()
        self.assertEqual(con_lista["lista"], "Distribuidores")
        producto = next(p for p in con_lista["productos"] if p["id"] == self.pt.id)
        self.assertEqual(producto["presentaciones"][0]["precio"], 120.0)
        self.assertEqual(producto["presentaciones"][1]["precio"], 500.0)

    def test_catalogo_solo_productos_activos_y_con_sesion_crm(self):
        self.pt.estatus = "Inactivo"
        db.session.commit()
        crm = self.client.application.test_client()
        with crm.session_transaction() as s:
            s["crm_logueado"] = True
        self.assertEqual(crm.get("/api/precios/catalogo").get_json()["productos"], [])

    def test_imagen_del_producto_para_el_crm(self):
        from io import BytesIO
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", (8, 8), "red").save(buf, "PNG")
        self.pt.imagen, self.pt.imagen_tipo = buf.getvalue(), "image/png"
        db.session.commit()
        url = f"/api/precios/producto/{self.pt.id}/imagen"
        self.assertEqual(self.client.application.test_client().get(url).status_code, 403)
        crm = self.client.application.test_client()
        with crm.session_transaction() as s:
            s["crm_logueado"] = True
        r = crm.get(url)
        self.assertEqual((r.status_code, r.mimetype), (200, "image/png"))
        self.assertEqual(r.data, buf.getvalue())
        producto = self.client.get("/api/precios/catalogo").get_json()["productos"][0]
        self.assertEqual(producto["imagen"], url)
        self.assertEqual(self.client.get("/api/precios/producto/9999/imagen").status_code, 404)


if __name__ == "__main__":
    unittest.main()
