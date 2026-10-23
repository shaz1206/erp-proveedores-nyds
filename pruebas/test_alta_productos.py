"""Alta de productos terminados desde el recetario: Activos, con categoria, enlazados y visibles en el CRM."""
import unittest

from test_proveedores_mp import app, db
from proveedores.alta_productos import categoria_sugerida, productos_del_recetario
from proveedores.vendssback import ProductoTerminado, Receta

URL = "/api/productos-terminados/alta-recetario"


class AltaProductosTest(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion["admin_logueado"] = True
            sesion["usuario"] = "compras.nyds"
        self.items = productos_del_recetario()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def lista(self):
        return self.client.get(URL).get_json()

    def alta(self, filas):
        return self.client.post(URL, json={"productos": [{"clave": f["clave"], "categoria": f["categoria"]} for f in filas]})

    def test_el_recetario_trae_33_productos_y_marca_las_bases(self):
        self.assertEqual(len(self.items), 33)
        bases = [i["nombre"] for i in self.items if i["base"]]
        self.assertEqual(len(bases), 4)
        self.assertTrue(all("CONCENTRADO" in b and "Pisos" in b for b in bases))
        # los concentrados que se venden como tal NO son "base"
        self.assertFalse(any(i["base"] for i in self.items if "N Zematic" in i["nombre"]))

    def test_categorias_sugeridas(self):
        self.assertEqual(categoria_sugerida("Ropa color"), "Lavandería")
        self.assertEqual(categoria_sugerida("Cloro"), "Baños y desinfección")
        self.assertEqual(categoria_sugerida("Shampoo Vehículos Negro N Zematic CONCENTRADO"), "Automotriz")
        self.assertEqual(categoria_sugerida("Algo nuevo"), "General")
        self.assertNotIn("General", {i["categoria"] for i in self.items})  # ninguno cae al comodin

    def test_lista_inicial_todo_pendiente_y_no_guarda(self):
        d = self.lista()
        self.assertEqual((len(d["productos"]), d["pendientes"]), (33, 33))
        self.assertTrue(all(p["estado"] == "nuevo" for p in d["productos"]))
        self.assertEqual(ProductoTerminado.query.count(), 0)

    def test_alta_crea_activos_con_sku_automatico_y_salen_en_el_crm(self):
        r = self.alta(self.lista()["productos"])
        self.assertEqual(r.status_code, 201)
        self.assertEqual((r.get_json()["creados"], r.get_json()["activados"]), (33, 0))
        self.assertEqual(ProductoTerminado.query.filter_by(estatus="Activo").count(), 33)
        skus = [p.sku for p in ProductoTerminado.query.order_by(ProductoTerminado.id)]
        self.assertEqual(skus[:2], ["PT-000001", "PT-000002"])
        self.assertEqual(len(set(skus)), 33)
        catalogo = self.client.get("/api/precios/catalogo").get_json()["productos"]
        self.assertEqual(len(catalogo), 33)
        self.assertEqual(self.lista()["pendientes"], 0)

    def test_activa_borradores_existentes_sin_duplicar_y_clasifica(self):
        # como los deja la carga del recetario: Borrador y "Por clasificar", enlazados a su receta
        item = next(i for i in self.items if i["nombre"] == "Cloro")
        p = ProductoTerminado(sku="PT-R00001", nombre="Cloro", categoria="Por clasificar", estatus="Borrador", stock_minimo=0, puntos_venta=[])
        db.session.add(p)
        db.session.flush()
        db.session.add(Receta(clave_origen="Recetario_NYDS:" + item["clave"], nombre="Cloro", tipo="Producto terminado", producto_id=p.id, origen={}))
        db.session.commit()
        fila = next(f for f in self.lista()["productos"] if f["clave"] == item["clave"])
        self.assertEqual((fila["estado"], fila["sku"], fila["categoria"]), ("borrador", "PT-R00001", "Baños y desinfección"))
        r = self.alta([fila]).get_json()
        self.assertEqual((r["creados"], r["activados"]), (0, 1))
        p = ProductoTerminado.query.one()
        self.assertEqual((p.estatus, p.categoria, p.sku), ("Activo", "Baños y desinfección", "PT-R00001"))

    def test_respeta_categoria_ya_puesta_y_no_repite_al_dar_alta_dos_veces(self):
        item = next(i for i in self.items if i["nombre"] == "Cloro")
        db.session.add(ProductoTerminado(sku="PT-000010", nombre="CLORO ", categoria="Mi categoría", estatus="Inactivo", stock_minimo=0, puntos_venta=[]))
        db.session.commit()
        fila = {"clave": item["clave"], "categoria": "Otra"}
        self.assertEqual(self.alta([fila]).get_json()["activados"], 1)
        self.assertEqual(self.alta([fila]).get_json()["ya_activos"], 1)
        self.assertEqual(ProductoTerminado.query.count(), 1)
        self.assertEqual(ProductoTerminado.query.one().categoria, "Mi categoría")

    def test_enlaza_receta_sin_producto(self):
        item = self.items[0]
        db.session.add(Receta(clave_origen="Recetario_NYDS:" + item["clave"], nombre=item["nombre"], tipo="Producto terminado", origen={}))
        db.session.commit()
        self.alta([{"clave": item["clave"], "categoria": item["categoria"]}])
        self.assertEqual(Receta.query.one().producto_id, ProductoTerminado.query.one().id)

    def test_entradas_invalidas(self):
        self.assertEqual(self.client.post(URL, json={}).status_code, 400)
        self.assertEqual(self.client.post(URL, json={"productos": []}).status_code, 400)
        r = self.client.post(URL, json={"productos": [{"clave": "no-existe"}, None, {"clave": 5}]})
        self.assertEqual(r.get_json()["creados"], 0)
        self.assertEqual(ProductoTerminado.query.count(), 0)

    def test_requiere_admin_y_pantalla_con_enlace(self):
        anonimo = app.test_client()
        self.assertEqual(anonimo.get(URL).status_code, 403)
        self.assertEqual(anonimo.post(URL, json={"productos": [{"clave": "03-01"}]}).status_code, 403)
        self.assertEqual(anonimo.get("/admin/productos-terminados/alta-recetario").status_code, 302)
        self.assertEqual(self.client.get("/admin/productos-terminados/alta-recetario").status_code, 200)
        self.assertIn("Dar de alta del recetario", self.client.get("/admin/productos-terminados").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
