"""Pruebas de aceptación CA-01 a CA-12 de 'Apartado de dar de alta proveedor -
Especificación funcional lista para programación'. SQLite en memoria; no toca
datos reales del ERP."""
import io
import unittest
from datetime import date, timedelta

from test_proveedores_mp import app, db
from proveedores.vendssback import (
    Bitacora,
    Contacto,
    Documento,
    MateriaPrima,
    MateriaPrimaProveedor,
    OrdenCompra,
    Proveedor,
    ProveedorEvaluacion,
)


def proveedor_base(rfc="AAA010101AAA", razon_social="Proveedor Uno", **extra):
    payload = dict(
        razon_social=razon_social,
        rfc=rfc,
        tipo_proveedor="Materia prima",
        codigo_postal="77500",
        condicion_pago="Contado",
        tiempo_entrega_normal=3,
        contactos=[
            {
                "nombre": "Ana Pérez",
                "correo": "ana@proveedor.com",
                "telefono": "9981234567",
                "tipo_contacto": "Principal",
                "es_principal": True,
            }
        ],
    )
    payload.update(extra)
    return payload


class ProveedoresAltaSpecTest(unittest.TestCase):
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

    # CA-01 --------------------------------------------------------------
    def test_ca01_alta_valida_queda_activo_y_en_catalogo(self):
        # Regla vigente: lo unico obligatorio es el nombre y un telefono; con eso queda Activo.
        payload = proveedor_base(estatus_inicial="Activo")
        resp = self.client.post("/api/proveedores", json=payload)
        self.assertEqual(resp.status_code, 201, resp.json)
        self.assertTrue(resp.json["codigo"].startswith("PROV-"))
        self.assertEqual(resp.json["estatus"], "Activo")

        payload2 = proveedor_base(rfc="BBB010101BBB", razon_social="Proveedor Servicio",
                                   tipo_proveedor="Servicio", estatus_inicial="Activo")
        resp2 = self.client.post("/api/proveedores", json=payload2)
        self.assertEqual(resp2.status_code, 201, resp2.json)
        self.assertEqual(resp2.json["estatus"], "Activo")

        catalogo = self.client.get("/api/proveedores").json
        self.assertEqual({p["rfc"] for p in catalogo}, {"AAA010101AAA", "BBB010101BBB"})

    def test_solo_el_nombre_es_obligatorio(self):
        # Sin nombre: no se crea.
        sin_nombre = self.client.post("/api/proveedores", json={"telefono": "9981112233"})
        self.assertEqual(sin_nombre.status_code, 400)
        self.assertIn("nombre", sin_nombre.json["error"])
        # Solo el nombre: se crea y puede quedar Activo; nada se inventa.
        ok = self.client.post("/api/proveedores", json={"razon_social": "Quimicos Locales", "estatus_inicial": "Activo"})
        self.assertEqual(ok.status_code, 201, ok.json)
        self.assertEqual(ok.json["estatus"], "Activo")
        fila = self.client.get("/api/proveedores").json[0]
        self.assertEqual(fila["rfc"], "")
        self.assertEqual(fila["domicilio"], "")
        self.assertEqual(fila["contacto"], "Sin contacto")
        # Dos proveedores sin RFC conviven; el telefono, si se da, queda como contacto.
        otro = self.client.post("/api/proveedores", json={"razon_social": "Otro Local", "telefono": "9984445566"})
        self.assertEqual(otro.status_code, 201, otro.json)
        fila = next(p for p in self.client.get("/api/proveedores").json if p["razon_social"] == "Otro Local")
        self.assertEqual(fila["telefono"], "9984445566")
        # Un RFC escrito mal si se rechaza; vacio no.
        malo = self.client.post("/api/proveedores", json={"razon_social": "Mal RFC", "rfc": "123"})
        self.assertEqual(malo.status_code, 400)

    # CA-02 ----------------------------------------------------------------
    def test_ca02_rfc_duplicado_bloquea_y_muestra_registro_existente(self):
        primero = self.client.post("/api/proveedores", json=proveedor_base())
        self.assertEqual(primero.status_code, 201)

        duplicado = self.client.post("/api/proveedores", json=proveedor_base(razon_social="Otro nombre"))
        self.assertEqual(duplicado.status_code, 400)
        self.assertIn("AAA010101AAA", duplicado.json["error"])
        self.assertIn("id_proveedor", duplicado.json)
        self.assertEqual(Proveedor.query.count(), 1)

    # CA-03 ------------------------------------------------------------
    def test_ca03_multiples_contactos_un_solo_principal(self):
        payload = proveedor_base(contactos=[
            {"nombre": "Contacto 1", "correo": "c1@x.com", "telefono": "111", "es_principal": True},
            {"nombre": "Contacto 2", "correo": "c2@x.com", "telefono": "222", "es_principal": True},
            {"nombre": "Contacto 3", "correo": "c3@x.com", "whatsapp": "333", "es_principal": False},
        ])
        resp = self.client.post("/api/proveedores", json=payload)
        self.assertEqual(resp.status_code, 201, resp.json)
        id_proveedor = resp.json["id_proveedor"]
        contactos = Contacto.query.filter_by(id_proveedor=id_proveedor).all()
        self.assertEqual(len(contactos), 3)
        self.assertEqual(sum(1 for c in contactos if c.es_principal), 1)
        principal = next(c for c in contactos if c.es_principal)
        self.assertEqual(principal.nombre, "Contacto 1")

    # CA-04 ------------------------------------------------------------
    def test_ca04_varias_materias_primas_con_condiciones_independientes(self):
        resp = self.client.post("/api/proveedores", json=proveedor_base())
        id_proveedor = resp.json["id_proveedor"]
        ids_mp = []
        for i in range(3):
            mp = MateriaPrima(sku_mp=f"MP-{i}", nombre_oficial=f"Insumo {i}", familia="Pruebas", estado_fisico="Solido",
                               nombre_normalizado=f"insumo {i}", unidad_base="kg", activo=True, borrador=False)
            db.session.add(mp)
            db.session.flush()
            ids_mp.append(mp.id_mp)
        db.session.commit()

        for idx, id_mp in enumerate(ids_mp):
            r = self.client.post(f"/api/proveedores/{id_proveedor}/materias-primas", json={
                "id_mp": id_mp, "presentacion_compra": "Saco 25kg", "contenido_presentacion": 25,
                "unidad_contenido": "kg", "factor_a_unidad_base": 25, "precio_vigente": 100 + idx,
                "moneda": "MXN", "compra_minima": 1, "lead_time_dias": 2,
            })
            self.assertEqual(r.status_code, 201, r.json)

        relaciones = self.client.get(f"/api/proveedores/{id_proveedor}/materias-primas").json
        self.assertEqual(len(relaciones), 3)
        self.assertEqual({r["precio_vigente"] for r in relaciones}, {100.0, 101.0, 102.0})

    # CA-05 ------------------------------------------------------------
    def test_ca05_una_mp_con_dos_proveedores_maximo_un_preferente(self):
        p1 = self.client.post("/api/proveedores", json=proveedor_base()).json
        p2 = self.client.post("/api/proveedores", json=proveedor_base(rfc="CCC010101CCC", razon_social="Proveedor Dos")).json
        mp = MateriaPrima(sku_mp="MP-X", nombre_oficial="Insumo X", nombre_normalizado="insumo x", familia="Pruebas", estado_fisico="Solido",
                           unidad_base="kg", activo=True, borrador=False)
        db.session.add(mp)
        db.session.commit()

        for p, preferente in ((p1, True), (p2, False)):
            r = self.client.post(f"/api/proveedores/{p['id_proveedor']}/materias-primas", json={
                "id_mp": mp.id_mp, "presentacion_compra": "Saco 25kg", "contenido_presentacion": 25,
                "unidad_contenido": "kg", "factor_a_unidad_base": 25, "precio_vigente": 50,
                "moneda": "MXN", "compra_minima": 1, "lead_time_dias": 1, "principal": preferente,
            })
            self.assertEqual(r.status_code, 201, r.json)

        relaciones = MateriaPrimaProveedor.query.filter_by(id_mp=mp.id_mp, activo=True).all()
        self.assertEqual(len(relaciones), 2)
        self.assertEqual(sum(r.principal for r in relaciones), 1)

    # CA-06 ------------------------------------------------------------
    def test_ca06_cambio_de_precio_conserva_historico(self):
        p = self.client.post("/api/proveedores", json=proveedor_base()).json
        mp = MateriaPrima(sku_mp="MP-Y", nombre_oficial="Insumo Y", nombre_normalizado="insumo y", familia="Pruebas", estado_fisico="Solido",
                           unidad_base="kg", activo=True, borrador=False)
        db.session.add(mp)
        db.session.commit()
        base = dict(id_mp=mp.id_mp, presentacion_compra="Saco 25kg", contenido_presentacion=25,
                    unidad_contenido="kg", factor_a_unidad_base=25, moneda="MXN", compra_minima=1,
                    lead_time_dias=1)

        r1 = self.client.post(f"/api/proveedores/{p['id_proveedor']}/materias-primas", json={**base, "precio_vigente": 85})
        self.assertEqual(r1.status_code, 201, r1.json)
        r2 = self.client.post(f"/api/proveedores/{p['id_proveedor']}/materias-primas", json={**base, "precio_vigente": 90})
        self.assertEqual(r2.status_code, 201, r2.json)

        todas = MateriaPrimaProveedor.query.filter_by(id_mp=mp.id_mp, id_proveedor=p["id_proveedor"]).all()
        self.assertEqual(len(todas), 2)
        vigente = next(r for r in todas if r.activo)
        historica = next(r for r in todas if not r.activo)
        self.assertEqual(float(vigente.precio_vigente), 90.0)
        self.assertEqual(float(historica.precio_vigente), 85.0)

    # CA-07 --------------------------------------------------------------
    def test_ca07_bloqueo_requiere_motivo_y_oculta_de_activos(self):
        p = self.client.post("/api/proveedores", json=proveedor_base(
            tipo_proveedor="Servicio", estatus_inicial="Activo")).json
        id_proveedor = p["id_proveedor"]

        sin_motivo = self.client.patch(f"/api/proveedores/{id_proveedor}/estatus",
                                        json={"estatus": "Bloqueado"})
        self.assertEqual(sin_motivo.status_code, 400)

        con_motivo = self.client.patch(f"/api/proveedores/{id_proveedor}/estatus",
                                        json={"estatus": "Bloqueado", "motivo": "Documentación vencida"})
        self.assertEqual(con_motivo.status_code, 200, con_motivo.json)

        activos = self.client.get("/api/proveedores/activos").json
        self.assertNotIn(id_proveedor, [a["id_proveedor"] for a in activos])
        # El histórico sigue consultable.
        self.assertEqual(self.client.get("/api/proveedores").json[0]["estatus"], "Bloqueado")

    def test_ca07b_bloqueo_con_oc_abierta_se_rechaza(self):
        p = self.client.post("/api/proveedores", json=proveedor_base(
            tipo_proveedor="Servicio", estatus_inicial="Activo")).json
        db.session.add(OrdenCompra(proveedor_id=p["id_proveedor"], proveedor_nombre="Proveedor Uno",
                                    entrega=date.today() + timedelta(days=5), estado="Emitida",
                                    moneda="MXN", usuario="compras.nyds", token="tok-oc-1"))
        db.session.commit()
        resp = self.client.patch(f"/api/proveedores/{p['id_proveedor']}/estatus",
                                  json={"estatus": "Bloqueado", "motivo": "Prueba"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("órdenes de compra abiertas", resp.json["error"])

    # CA-08 ------------------------------------------------------------
    def test_ca08_reactivacion_vuelve_a_activo_y_audita(self):
        p = self.client.post("/api/proveedores", json=proveedor_base(
            tipo_proveedor="Servicio", estatus_inicial="Activo")).json
        id_proveedor = p["id_proveedor"]
        self.client.patch(f"/api/proveedores/{id_proveedor}/estatus",
                           json={"estatus": "Bloqueado", "motivo": "Prueba"})

        reactivar = self.client.patch(f"/api/proveedores/{id_proveedor}/estatus", json={"estatus": "Activo"})
        self.assertEqual(reactivar.status_code, 200, reactivar.json)
        self.assertEqual(db.session.get(Proveedor, id_proveedor).estatus, "Activo")
        acciones = [b.accion for b in Bitacora.query.filter_by(id_proveedor=id_proveedor).all()]
        self.assertIn("Cambio de Estatus", acciones)

    # CA-09 --------------------------------------------------------------
    def test_ca09_documentos_guardan_metadatos_y_versionan(self):
        p = self.client.post("/api/proveedores", json=proveedor_base()).json
        id_proveedor = p["id_proveedor"]

        csf = self.client.post(f"/api/proveedores/{id_proveedor}/documentos", data={
            "tipo_documento": "Constancia de Situación Fiscal",
            "archivo": (io.BytesIO(b"contenido-csf"), "csf.pdf"),
        }, content_type="multipart/form-data")
        self.assertEqual(csf.status_code, 201, csf.json)

        sds = self.client.post(f"/api/proveedores/{id_proveedor}/documentos", data={
            "tipo_documento": "SDS",
            "archivo": (io.BytesIO(b"contenido-sds"), "sds.pdf"),
        }, content_type="multipart/form-data")
        self.assertEqual(sds.status_code, 201, sds.json)

        docs = self.client.get(f"/api/proveedores/{id_proveedor}/documentos").json
        self.assertEqual(len(docs), 2)
        for d in docs:
            self.assertIsNotNone(d["tamano_bytes"])
            self.assertEqual(d["usuario"], "compras.nyds")
            self.assertIsNotNone(d["fecha"])

        # Reemplazo de versión: el anterior no se pierde, solo queda inactivo.
        nueva_version = self.client.post(f"/api/proveedores/{id_proveedor}/documentos", data={
            "tipo_documento": "Constancia de Situación Fiscal",
            "archivo": (io.BytesIO(b"contenido-csf-v2"), "csf_v2.pdf"),
        }, content_type="multipart/form-data")
        self.assertEqual(nueva_version.status_code, 201)
        self.assertEqual(nueva_version.json["version"], 2)
        self.assertEqual(Documento.query.filter_by(id_proveedor=id_proveedor, tipo_documento="Constancia de Situación Fiscal").count(), 2)

    # CA-10 --------------------------------------------------------------
    def test_ca10_compras_solo_ofrece_mp_de_proveedores_activos(self):
        activo = self.client.post("/api/proveedores", json=proveedor_base(estatus_inicial="Activo")).json
        bloqueado = self.client.post("/api/proveedores", json=proveedor_base(
            rfc="DDD010101DDD", razon_social="Proveedor Bloqueado", estatus_inicial="Activo")).json

        mp = MateriaPrima(sku_mp="MP-Z", nombre_oficial="Insumo Z", nombre_normalizado="insumo z", familia="Pruebas", estado_fisico="Solido",
                           unidad_base="kg", activo=True, borrador=False)
        db.session.add(mp)
        db.session.commit()
        condicion = dict(id_mp=mp.id_mp, presentacion_compra="Saco 25kg", contenido_presentacion=25,
                          unidad_contenido="kg", factor_a_unidad_base=25, precio_vigente=50,
                          moneda="MXN", compra_minima=1, lead_time_dias=1)
        for pr in (activo, bloqueado):
            self.client.post(f"/api/proveedores/{pr['id_proveedor']}/materias-primas", json=condicion)

        # activo['id_proveedor'] es 'Materia prima' -> tras relacionar la MP, ya puede activarse.
        self.client.patch(f"/api/proveedores/{activo['id_proveedor']}/estatus", json={"estatus": "Activo"})
        self.client.patch(f"/api/proveedores/{bloqueado['id_proveedor']}/estatus",
                           json={"estatus": "Bloqueado", "motivo": "Prueba CA-10"})

        pagina = self.client.get("/admin/compras/nueva")
        self.assertEqual(pagina.status_code, 200)
        html = pagina.data.decode()
        self.assertIn("Proveedor Uno", html)
        self.assertNotIn("Proveedor Bloqueado", html)

    # CA-11 --------------------------------------------------------------
    def test_ca11_no_existe_borrado_fisico_de_proveedor(self):
        p = self.client.post("/api/proveedores", json=proveedor_base()).json
        resp = self.client.delete(f"/api/proveedores/{p['id_proveedor']}")
        self.assertEqual(resp.status_code, 404)  # el método no existe: no hay ruta de borrado.
        self.assertEqual(Proveedor.query.count(), 1)

    # CA-12 --------------------------------------------------------------
    def test_ca12_bitacora_registra_usuario_fecha_y_accion(self):
        p = self.client.post("/api/proveedores", json=proveedor_base()).json
        id_proveedor = p["id_proveedor"]
        self.client.patch(f"/api/proveedores/{id_proveedor}/estatus",
                           json={"estatus": "Bloqueado", "motivo": "Revisión"})

        entradas = Bitacora.query.filter_by(id_proveedor=id_proveedor).order_by(Bitacora.id_bitacora).all()
        self.assertGreaterEqual(len(entradas), 2)
        for entrada in entradas:
            self.assertEqual(entrada.usuario, "compras.nyds")
            self.assertIsNotNone(entrada.fecha)
            self.assertIsNotNone(entrada.accion)


class ProveedorEvaluacionTest(unittest.TestCase):
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

    def test_evaluacion_pondera_y_se_puede_consultar(self):
        p = self.client.post("/api/proveedores", json=proveedor_base()).json
        id_proveedor = p["id_proveedor"]
        resp = self.client.post(f"/api/proveedores/{id_proveedor}/evaluaciones", json={
            "periodo": "2026-Q3", "calif_precio": 4, "calif_calidad": 5,
            "calif_entrega": 3, "calif_atencion": 4,
        })
        self.assertEqual(resp.status_code, 201, resp.json)
        # 4*25 + 5*30 + 3*30 + 4*15 = 100+150+90+60=400 /100 = 4.0
        self.assertAlmostEqual(resp.json["calificacion_final"], 4.0)

        listado = self.client.get(f"/api/proveedores/{id_proveedor}/evaluaciones").json
        self.assertEqual(len(listado), 1)
        self.assertEqual(listado[0]["periodo"], "2026-Q3")

    def test_evaluacion_rechaza_fuera_de_rango(self):
        p = self.client.post("/api/proveedores", json=proveedor_base()).json
        resp = self.client.post(f"/api/proveedores/{p['id_proveedor']}/evaluaciones", json={
            "calif_precio": 6, "calif_calidad": 5, "calif_entrega": 3, "calif_atencion": 4,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(ProveedorEvaluacion.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
