"""Extracción literal del recetario. Nunca ejecuta las notas del documento."""
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from pathlib import Path
from decimal import Decimal
import re, json, hashlib, unicodedata

NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
def clean(s): return re.sub(r'\s+',' ',s).strip()
def key(s):
    return ''.join(c for c in unicodedata.normalize('NFKD',clean(s).casefold()) if not unicodedata.combining(c))
def text(el): return clean(''.join(t.text or '' for t in el.findall('.//w:t',NS)))

def extraer(path):
    with ZipFile(path) as z: body=ET.fromstring(z.read('word/document.xml')).find('w:body',NS)
    recipes=[]; title=''; section=0; group=[]; in_notes=False
    def new(name,tipo,table):
        r=dict(clave=f'{section:02d}-{len(group)+1:02d}',nombre=clean(name),tipo=tipo,seccion=section,tabla=table,componentes=[],notas=[],rendimiento=None,unidad_rendimiento=None)
        if section==1:r.update(rendimiento='1000',unidad_rendimiento='mL')
        if 'PRODUCTO FINAL (1 litro)' in name:r.update(rendimiento='1',unidad_rendimiento='L')
        group.append(r);recipes.append(r);return r
    table_no=0
    for el in body:
        if el.tag.endswith('}p'):
            t=text(el)
            if not t:continue
            if 'Comentarios' in t or t.startswith('Notas'):in_notes=True
            elif t.startswith('NYDS |'):in_notes=False
            elif in_notes and group:
                for r in group:r['notas'].append(t)
            else:title=t
        elif el.tag.endswith('}tbl'):
            table_no+=1
            rows=[[text(c) for c in row.findall('w:tc',NS)] for row in el.findall('w:tr',NS)]
            if not rows:continue
            if rows[0][0]=='Tipo':
                section+=1;group=[];in_notes=False;tipo='Semiterminado' if 'Subreceta' in rows[0][1] else 'Producto terminado'
                section_title=title
            elif rows[0][0]=='Materia prima':
                current=None if section==1 else new(section_title,tipo,table_no)
                for row_no,row in enumerate(rows[1:],2):
                    if len(row)==1:
                        current=new(row[0], 'Semiterminado' if section==1 else 'Producto terminado',table_no)
                    else:
                        if len(row)!=5 or current is None:raise ValueError(f'Tabla {table_no}, fila {row_no}: estructura inesperada')
                        n=Decimal(row[2])
                        if not n.is_finite() or n<=0:raise ValueError('Cantidad inválida')
                        current['componentes'].append(dict(nombre=row[0],proveedor=row[1],cantidad=str(n),unidad=row[3],observaciones=row[4],tabla=table_no,fila=row_no,original=row))
            elif in_notes and group:
                for row in rows:
                    t=' | '.join(filter(None,row))
                    if t:
                        for r in group:r['notas'].append(t)
    assert section==31,(section,'Secciones distintas a las esperadas')
    assert all(r['componentes'] for r in recipes)
    return dict(archivo=Path(path).name,sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),secciones=section,recetas=recipes)

if __name__=='__main__':
    path=Path(__file__).parent/'data/recetario/Recetario_NYDS.docx'
    d=extraer(path)
    out=path.with_suffix('.json');out.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'recetas':len(d['recetas']),'componentes':sum(len(r['componentes']) for r in d['recetas']),'secciones':d['secciones']}))
