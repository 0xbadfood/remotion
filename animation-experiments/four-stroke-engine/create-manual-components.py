"""Author the seven requested SVGs. Registration follows explicit visual inspection."""
from pathlib import Path
import json
import sys
import html

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from infofilm.core import read,write,digest,ROOT
from infofilm.animation_objects import validate_svg

ROOT_OUT=ROOT/'artifacts/experiments/four-stroke-engine-luna/manual-components'
REQUESTS=ROOT/'artifacts/experiments/four-stroke-engine-luna/object-requests.json'

def svg(width,height,body,title):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}"><title>{html.escape(title)}</title><g stroke="#E0E9F0" stroke-width="3" stroke-linejoin="round">{body}</g></svg>\n'

# Coordinates are intrinsic part geometry, not a model's scene layout or motion.
parts={
 'req-engine-block':(400,600,
  '<path fill="#526574" d="M90 60 H130 V95 H115 V125 H130 V140 H90 Z M160 60 H190 V140 H160 Z M210 60 H240 V140 H210 Z M270 60 H310 V140 H270 V125 H285 V95 H270 Z"/>'
  '<path fill="#657989" d="M90 140 H120 V390 H90 Z M280 140 H310 V390 H280 Z"/>'
  '<path fill="#526574" d="M90 390 H120 L100 450 V550 Q100 565 120 565 H280 Q300 565 300 550 V450 L280 390 H310 L340 440 V560 Q340 590 310 590 H90 Q60 590 60 560 V440 Z"/>'
  '<path fill="none" stroke="#A9BFCB" d="M120 140 H130 M160 140 H190 M210 140 H240 M270 140 H280"/>'
  '<path stroke="#657989" stroke-dasharray="5 5" fill="none" d="M180 500 H220 M200 480 V520"/>',
  {'bore_top_left':[120,140],'bore_top_right':[280,140],'bore_bottom_left':[120,390],
   'intake_valve_seat':[145,140],'exhaust_valve_seat':[255,140],
   'intake_passage_opening':[115,110],'exhaust_passage_opening':[285,110],
   'spark_plug_port':[200,100],'crankshaft_axis':[200,500]}),
 'req-engine-piston':(160,110,
  '<path fill="#B9CDD8" d="M5 8 H155 V101 H115 V88 H45 V101 H5 Z"/>'
  '<path fill="none" stroke="#627E91" d="M7 23 H153 M7 35 H153"/>'
  '<circle cx="80" cy="64" r="13" fill="#334C60"/><circle cx="80" cy="64" r="5" fill="#B9CDD8"/>',
  {'crown_center':[80,8],'skirt_bottom':[80,101],'wrist_pin':[80,64]}),
 'req-engine-connecting-rod':(70,240,
  '<path fill="#9CB5C5" d="M26 42 H44 L48 177 H22 Z"/>'
  '<circle cx="35" cy="25" r="20" fill="#9CB5C5"/><circle cx="35" cy="25" r="10" fill="#101820"/>'
  '<circle cx="35" cy="204" r="27" fill="#9CB5C5"/><circle cx="35" cy="204" r="16" fill="#101820"/>',
  {'small_end':[35,25],'big_end':[35,204],'shaft_midpoint':[35,115]}),
 'req-engine-crankshaft':(180,180,
  '<path fill="#6E8A9A" d="M30 92 Q27 159 90 160 Q153 159 150 92 L113 82 H67 Z"/>'
  '<path fill="#91AAB9" d="M74 34 H106 L113 92 H67 Z"/>'
  '<circle cx="90" cy="90" r="21" fill="#496474"/><circle cx="90" cy="90" r="9" fill="#D4E4EC"/>'
  '<circle cx="90" cy="34" r="13" fill="#F0BF65"/>',
  {'main_axis':[90,90],'crank_pin':[90,34],'throw_radius_pixels':56}),
 'req-engine-poppet-valve':(40,150,
  '<path fill="#AFCCD8" d="M16 9 H24 V128 L36 134 V144 H4 V134 L16 128 Z"/>',
  {'stem_top':[20,9],'head_center':[20,137],'seat_contact':[20,144]}),
 'req-engine-flow-passage':(120,60,
  '<path fill="#536F80" d="M2 8 H118 V20 H2 Z M2 40 H118 V52 H2 Z"/>'
  '<path fill="none" stroke="#87A9BB" stroke-width="2" d="M2 20 H118 M2 40 H118"/>',
  {'left_open_endpoint':[0,30],'right_open_endpoint':[120,30],'channel_center':[60,30]}),
 'req-engine-spark-plug':(50,140,
  '<path fill="#F3E9D2" d="M19 7 H31 V22 H35 V31 H31 V40 H35 V49 H31 V66 H19 V49 H15 V40 H19 V31 H15 V22 H19 Z"/>'
  '<path fill="#A9BDC9" d="M10 66 H40 V86 H10 Z M16 86 H34 V111 H16 Z"/>'
  '<path stroke="#576C7D" d="M17 93 H33 M17 99 H33 M17 105 H33"/>'
  '<path fill="none" stroke="#E0E9F0" stroke-width="5" d="M25 111 V128 M33 111 V135 H25"/>',
  {'plug_body':[25,76],'thread_center':[25,99],'electrode_tip':[25,128]}),
}

def main():
    ROOT_OUT.mkdir(parents=True,exist_ok=True)
    requests=read(REQUESTS)['requests'];rows=[]
    for rid,(width,height,body,landmarks) in parts.items():
        matched=[r for r in requests if r['request_id']==rid];r=matched[0]
        caps=sorted(set().union(*(set(row['required_capabilities']) for row in matched)))
        title=r['requested_asset_id'];data=svg(width,height,body,title).encode()
        validate_svg(data)
        path=ROOT_OUT/(rid+'.svg')
        if path.exists() and path.read_bytes()!=data:raise ValueError('Existing authored SVG differs: '+rid)
        path.write_bytes(data)
        metadata={'pivot':r['svg_requirements']['pivot'],'capabilities':caps,
            'provenance':'Hand-authored reusable side-view engine component for this experiment. '
                'Intrinsic canvas '+str(width)+'x'+str(height)+'. Landmark coordinates in intrinsic SVG pixels: '+
                json.dumps(landmarks,sort_keys=True)+'. This describes the asset only; the frontier model owns scene placement and motion.',
            'semantic_landmarks':landmarks,'viewbox':[0,0,width,height]}
        write(ROOT_OUT/(rid+'.metadata.json'),metadata)
        rows.append({'request_id':rid,'asset_id':title,'svg':str(path),'sha256':digest(data),
            'metadata':str(ROOT_OUT/(rid+'.metadata.json')),'landmarks':landmarks,'width':width,'height':height})
    write(ROOT_OUT/'authored-assets.json',{'objects':rows,'inspection':'pending','registration':'pending'})
    cards=''.join('<figure><div class="sprite">'+Path(r['svg']).read_text()+'</div><figcaption>'+html.escape(r['asset_id'])+'</figcaption></figure>' for r in rows)
    # Visual compatibility test only; these hand-selected display coordinates are not model inputs.
    def use(rid,x,y):
        w,h,body,_=parts[rid]
        return f'<g transform="translate({x} {y})"><svg width="{w}" height="{h}" viewBox="0 0 {w} {h}"><g stroke="#E0E9F0" stroke-width="3" stroke-linejoin="round">{body}</g></svg></g>'
    assembled='<svg xmlns="http://www.w3.org/2000/svg" viewBox="-40 -30 480 650">'+use('req-engine-block',0,0)+use('req-engine-flow-passage',-5,80)+use('req-engine-flow-passage',285,80)+use('req-engine-crankshaft',110,410)+use('req-engine-connecting-rod',165,240)+use('req-engine-piston',120,201)+use('req-engine-poppet-valve',125,-4)+use('req-engine-poppet-valve',235,-4)+use('req-engine-spark-plug',175,-28)+'</svg>'
    page='<html><head><meta charset="utf-8"><style>body{background:#101820;color:#eef6fa;font:15px system-ui;margin:24px}main{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}figure{margin:0;padding:14px;border:1px solid #526574;border-radius:10px}figcaption{overflow-wrap:anywhere;min-height:45px}.sprite{height:250px;display:flex;align-items:center;justify-content:center}.sprite svg{max-height:100%;max-width:100%}.assembly{grid-row:span 2;height:630px}.assembly svg{height:570px;width:100%}</style></head><body><h1>Seven reusable engine components</h1><p>Hand-authored SVGs; assembly at left is an inspection fixture, excluded from model inputs.</p><main><figure class="assembly">'+assembled+'<figcaption>Compatibility inspection: cutaway with articulated parts</figcaption></figure>'+cards+'</main></body></html>'
    (ROOT_OUT/'gallery.html').write_text(page)
    print(ROOT_OUT/'gallery.html')

if __name__=='__main__':main()
