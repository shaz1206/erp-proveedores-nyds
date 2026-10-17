"""Distribuidores, confirmar ventas y ventas canceladas."""
import unittest

from werkzeug.security import generate_password_hash

from test_crm import CRMTest
from proveedores.vendssback import ClienteCRM, ClienteDistribuidor, PedidoCRM, Persona, db


class DistribuidoresTest(unittest.TestCase):
    setUp = CRMTest.setUp
    tearDown = CRMTest.tearDown
    crear_pedido = CRMTest.crear_pedido

    def persona(self, nombre="Adry", usuario="adry", rol="ventas", activo=True):
        p = Persona(nombre=nombre, usuario=usuario, rol=rol, activo=activo, password_hash=generate_password_hash("secreto1"))
        db.session.add(p)
        db.session.commit()
        return p

    def pedido(self, token="p1"):
        resp = self.crear_pedido(token=token)
        self.assertEqual(resp.status_code, 201)
        return PedidoCRM.query.get(resp.get_json()["id"])

    def entrar_crm(self, rol):
        anonimo = self.client.application.test_client()
        with anonimo.session_transaction() as s:
            s["crm_logueado"] = True
            s["crm_usuario"] = f"{rol}.test"
            s["crm_rol"] = rol
        return anonimo

    # ---- acceso ----
    def test_admin_exige_login_de_erp(self):
        anonimo = self.client.application.test_client()
        r = anonimo.get("/admin/distribuidores")
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r.headers["Location"])
        self.assertEqual(anonimo.post("/admin/distribuidores/asignar", data={"cliente_id": 1}).status_code, 302)

    def test_paginas_crm_exigen_sesion_y_rol_ventas(self):
        anonimo = self.client.application.test_client()
        for ruta in ("/crm/ventas/confirmar", "/crm/ventas/canceladas"):
            self.assertEqual(anonimo.get(ruta).status_code, 302, ruta)
            self.assertEqual(self.entrar_crm("reparto").get(ruta).status_code, 403, ruta)
            self.assertEqual(self.entrar_crm("ventas").get(ruta).status_code, 200, ruta)
            self.assertEqual(self.client.get(ruta).status_code, 200, ruta)

    # ---- asignacion ----
    def test_asignar_cambiar_y_quitar_distribuidor(self):
        pedido = self.pedido()
        adry, jose = self.persona(), self.persona("José", "jose")
        cid = pedido.cliente_id
        self.assertEqual(self.client.get("/admin/distribuidores").status_code, 200)
        self.client.post("/admin/distribuidores/asignar", data={"cliente_id": cid, "persona_id": adry.id})
        self.assertEqual(ClienteDistribuidor.query.one().persona_id, adry.id)
        self.client.post("/admin/distribuidores/asignar", data={"cliente_id": cid, "persona_id": jose.id})
        self.assertEqual(ClienteDistribuidor.query.count(), 1)
        self.assertEqual(ClienteDistribuidor.query.one().persona_id, jose.id)
        self.client.post("/admin/distribuidores/asignar", data={"cliente_id": cid, "persona_id": ""})
        self.assertEqual(ClienteDistribuidor.query.count(), 0)

    def test_solo_ventas_o_supervisor_activos_pueden_ser_distribuidores(self):
        pedido = self.pedido()
        reparto = self.persona("Rep", "rep", rol="reparto")
        inactiva = self.persona("Inac", "inac", activo=False)
        for p in (reparto, inactiva):
            self.client.post("/admin/distribuidores/asignar", data={"cliente_id": pedido.cliente_id, "persona_id": p.id})
        self.client.post("/admin/distribuidores/asignar", data={"cliente_id": pedido.cliente_id, "persona_id": "999"})
        self.client.post("/admin/distribuidores/asignar", data={"cliente_id": pedido.cliente_id, "persona_id": "abc"})
        self.assertEqual(ClienteDistribuidor.query.count(), 0)

    def test_busqueda_de_clientes(self):
        self.pedido()
        self.assertIn(b"Cliente prueba", self.client.get("/admin/distribuidores?q=prueba").data)
        self.assertNotIn(b"Cliente prueba", self.client.get("/admin/distribuidores?q=zzz").data)

    # ---- confirmar ventas ----
    def test_pedidos_recibidos_aparecen_y_confirmados_ya_no(self):
        pedido = self.pedido()
        self.assertIn(pedido.folio.encode(), self.client.get("/crm/ventas/confirmar").data)
        self.client.post(f"/api/crm/pedidos/{pedido.id}/confirmar", json={})
        pagina = self.client.get("/crm/ventas/confirmar").data
        self.assertNotIn(pedido.folio.encode(), pagina)
        self.assertIn("Todo al día".encode(), pagina)

    def test_filtro_por_distribuidor_y_sin_distribuidor(self):
        pedido = self.pedido()
        adry, jose = self.persona(), self.persona("José", "jose")
        folio = pedido.folio.encode()
        self.assertIn(folio, self.client.get("/crm/ventas/confirmar?distribuidor=sin").data)
        self.client.post("/admin/distribuidores/asignar", data={"cliente_id": pedido.cliente_id, "persona_id": adry.id})
        self.assertIn(folio, self.client.get(f"/crm/ventas/confirmar?distribuidor={adry.id}").data)
        self.assertNotIn(folio, self.client.get(f"/crm/ventas/confirmar?distribuidor={jose.id}").data)
        self.assertNotIn(folio, self.client.get("/crm/ventas/confirmar?distribuidor=sin").data)
        self.assertIn(folio, self.client.get("/crm/ventas/confirmar?distribuidor=basura").data)

    # ---- canceladas ----
    def test_cancelada_muestra_motivo_usuario_y_sale_de_pendientes(self):
        pedido = self.pedido()
        adry = self.persona()
        self.client.post("/admin/distribuidores/asignar", data={"cliente_id": pedido.cliente_id, "persona_id": adry.id})
        r = self.client.post(f"/api/crm/pedidos/{pedido.id}/estado", json={"estado": "Cancelado", "comentario": "Cliente ya no lo quiso"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(pedido.folio.encode(), self.client.get("/crm/ventas/confirmar").data)
        pagina = self.client.get("/crm/ventas/canceladas").data
        self.assertIn(pedido.folio.encode(), pagina)
        self.assertIn(b"Cliente ya no lo quiso", pagina)
        self.assertIn(b"Adry", pagina)
        self.assertIn(b"ventas.nyds", pagina)
        self.assertNotIn(pedido.folio.encode(), self.client.get("/crm/ventas/canceladas?distribuidor=sin").data)

    def test_cancelada_sin_motivo_no_inventa_uno(self):
        pedido = self.pedido()
        self.client.post(f"/api/crm/pedidos/{pedido.id}/estado", json={"estado": "Cancelado"})
        pagina = self.client.get("/crm/ventas/canceladas").data
        self.assertIn(pedido.folio.encode(), pagina)
        self.assertNotIn(b"Pedido cambiado a Cancelado", pagina)

    def test_menus_muestran_los_enlaces_nuevos(self):
        self.assertIn(b"/admin/distribuidores", self.client.get("/admin/operaciones").data)
        pagina = self.client.get("/crm/ventas/confirmar").data
        self.assertIn(b"/crm/ventas/confirmar", pagina)
        self.assertIn(b"/crm/ventas/canceladas", pagina)


if __name__ == "__main__":
    unittest.main()
