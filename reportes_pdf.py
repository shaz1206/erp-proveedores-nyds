"""Generación de reportes PDF descargables con la identidad visual de NYDS.

Reutilizado por inventario.py (saldos de materia prima) y crm.py (rutas de
entrega y productos entregados) para que todos los PDF del ERP compartan el
mismo membrete, tipografía y colores que el resto del sistema.
"""
import os
from datetime import datetime
from io import BytesIO

from flask import Response
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

MIDNIGHT = colors.HexColor("#132441")
CHARTREUSE = colors.HexColor("#daf561")
POWDER = colors.HexColor("#87bbd7")
LAPIS = colors.HexColor("#3451a3")
MUTED = colors.HexColor("#607086")
BORDE = colors.HexColor("#d9e3e8")
ZEBRA = colors.HexColor("#f4f6fa")

_LOGO_PATH = os.path.join(os.path.dirname(__file__), "static", "nyds-logo.png")

_estilos_base = getSampleStyleSheet()
ESTILO_TITULO = ParagraphStyle("nyds_titulo", parent=_estilos_base["Heading1"], textColor=MIDNIGHT,
                               fontName="Helvetica-Bold", fontSize=16, leading=19, spaceAfter=2)
ESTILO_SUBTITULO = ParagraphStyle("nyds_subtitulo", parent=_estilos_base["Normal"], textColor=MUTED,
                                  fontName="Helvetica", fontSize=9.5, leading=13)
ESTILO_SECCION = ParagraphStyle("nyds_seccion", parent=_estilos_base["Heading2"], textColor=LAPIS,
                                 fontName="Helvetica-Bold", fontSize=11.5, leading=14,
                                 spaceBefore=14, spaceAfter=6)
ESTILO_CELDA = ParagraphStyle("nyds_celda", parent=_estilos_base["Normal"], textColor=MIDNIGHT,
                               fontName="Helvetica", fontSize=8.6, leading=11, alignment=TA_LEFT)
ESTILO_CELDA_MUTED = ParagraphStyle("nyds_celda_muted", parent=ESTILO_CELDA, textColor=MUTED)


def celda(texto, muted=False):
    """Envuelve texto en un Paragraph para que haga salto de línea dentro de la tabla."""
    if texto is None:
        texto = ""
    return Paragraph(str(texto), ESTILO_CELDA_MUTED if muted else ESTILO_CELDA)


def _membrete(titulo, generado_el):
    def dibujar(canvas, doc):
        canvas.saveState()
        ancho, alto = doc.pagesize
        try:
            canvas.drawImage(_LOGO_PATH, 1.6 * cm, alto - 2.0 * cm, width=3.0 * cm, height=1.65 * cm,
                              preserveAspectRatio=True, anchor="sw", mask="auto")
        except Exception:
            pass
        canvas.setFont("Helvetica-Bold", 10)
        canvas.setFillColor(MIDNIGHT)
        canvas.drawRightString(ancho - 1.6 * cm, alto - 1.15 * cm, "NYDS")
        canvas.setFont("Helvetica", 7.6)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(ancho - 1.6 * cm, alto - 1.52 * cm, titulo)
        canvas.setStrokeColor(CHARTREUSE)
        canvas.setLineWidth(2.6)
        canvas.line(1.6 * cm, alto - 2.1 * cm, ancho - 1.6 * cm, alto - 2.1 * cm)
        canvas.setFont("Helvetica", 7.4)
        canvas.setFillColor(MUTED)
        canvas.drawString(1.6 * cm, 1.2 * cm, f"Generado el {generado_el} · NYDS")
        canvas.drawRightString(ancho - 1.6 * cm, 1.2 * cm, f"Página {doc.page}")
        canvas.restoreState()

    return dibujar


def generar_pdf(titulo, subtitulo, secciones, nombre_archivo="reporte-nyds.pdf"):
    """Construye un PDF con membrete NYDS a partir de una lista de secciones.

    Cada sección es un dict con:
      - titulo: encabezado de la sección (opcional, puede ser None)
      - columnas: lista de encabezados de columna (texto plano)
      - filas: lista de filas; cada celda puede ser texto plano o un
        resultado de celda(...) si necesita salto de línea
      - anchos: lista de anchos de columna en cm (opcional)
      - vacio: mensaje a mostrar cuando filas está vacío
    """
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        leftMargin=1.6 * cm, rightMargin=1.6 * cm,
        topMargin=2.5 * cm, bottomMargin=1.8 * cm,
        title=titulo,
    )
    generado_el = datetime.now().strftime("%d/%m/%Y %H:%M")
    cuerpo = [Paragraph(titulo, ESTILO_TITULO)]
    if subtitulo:
        cuerpo.append(Paragraph(subtitulo, ESTILO_SUBTITULO))
    cuerpo.append(Spacer(1, 10))

    for seccion in secciones:
        if seccion.get("titulo"):
            cuerpo.append(Paragraph(seccion["titulo"], ESTILO_SECCION))
        columnas = seccion["columnas"]
        filas = seccion.get("filas") or []
        if not filas:
            mensaje = seccion.get("vacio", "Sin registros.")
            filas = [[celda(mensaje, muted=True)] + ["" for _ in columnas[1:]]]
        anchos = [a * cm for a in seccion["anchos"]] if seccion.get("anchos") else None
        tabla = Table([columnas] + filas, colWidths=anchos, repeatRows=1)
        tabla.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), MIDNIGHT),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8.4),
            ("TOPPADDING", (0, 0), (-1, 0), 7),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 7),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZEBRA]),
            ("GRID", (0, 0), (-1, -1), 0.4, BORDE),
            ("TOPPADDING", (0, 1), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        cuerpo.append(tabla)
        cuerpo.append(Spacer(1, 14))

    membrete = _membrete(titulo, generado_el)
    doc.build(cuerpo, onFirstPage=membrete, onLaterPages=membrete)
    buffer.seek(0)
    return buffer


def respuesta_pdf(buffer, nombre_archivo):
    respuesta = Response(buffer.read(), mimetype="application/pdf")
    respuesta.headers["Content-Disposition"] = f'inline; filename="{nombre_archivo}"'
    respuesta.headers["X-Content-Type-Options"] = "nosniff"
    return respuesta
