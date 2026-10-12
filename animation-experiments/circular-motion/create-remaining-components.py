"""Author intrinsic assets demanded by C01/C03/C04; inspect before registering."""
import argparse,html,json,math,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from infofilm.core import read,write,digest
from infofilm.animation_objects import validate_svg

PARTS={}
def part(asset,w,h,body,landmarks,notes):
    PARTS[asset]={'width':w,'height':h,'body':body,'landmarks':landmarks,'notes':notes}

def arrow(a,b,color,head=9,stroke=4):
    dx,dy=b[0]-a[0],b[1]-a[1];length=math.hypot(dx,dy);ux,uy=dx/length,dy/length
    base=(b[0]-head*ux,b[1]-head*uy)
    pts=[b,(base[0]-head*.45*uy,base[1]+head*.45*ux),(base[0]+head*.45*uy,base[1]-head*.45*ux)]
    return f'<path d="M{a[0]} {a[1]}L{base[0]} {base[1]}" fill="none" stroke="{color}" stroke-width="{stroke}"/><polygon points="'+ ' '.join(f'{x},{y}' for x,y in pts)+f'" fill="{color}"/>'

def car_top():
    return '<rect x="28" y="6" width="30" height="13" rx="4" fill="#17212C"/><rect x="110" y="6" width="30" height="13" rx="4" fill="#17212C"/><rect x="28" y="71" width="30" height="13" rx="4" fill="#17212C"/><rect x="110" y="71" width="30" height="13" rx="4" fill="#17212C"/><path d="M20 24Q14 45 20 66L130 66Q152 61 154 45Q152 29 130 24Z" fill="#E8AF5A" stroke="#F8E0AE" stroke-width="2"/><path d="M92 27L122 29L127 61L92 63Z" fill="#193D54"/><path d="M34 28L55 28L55 62L34 62Z" fill="#315B6F"/><path d="M71 27V63" stroke="#A87432" stroke-width="2"/>'

def car_front():
    return '<rect x="28" y="69" width="20" height="27" rx="6" fill="#17212C" stroke="#B9C7D1" stroke-width="2"/><rect x="112" y="69" width="20" height="27" rx="6" fill="#17212C" stroke="#B9C7D1" stroke-width="2"/><path d="M25 76L34 48L49 27L111 27L126 48L135 76Z" fill="#E8AF5A" stroke="#F8E0AE" stroke-width="2"/><path d="M49 32L111 32L121 52L39 52Z" fill="#193D54"/><rect x="33" y="58" width="94" height="20" rx="6" fill="#E8AF5A"/><rect x="37" y="64" width="16" height="7" rx="2" fill="#F7F0CE"/><rect x="107" y="64" width="16" height="7" rx="2" fill="#F7F0CE"/><path d="M63 67H97" stroke="#866A43" stroke-width="3"/>'

def road_curve():
    return '<path d="M200 45A155 155 0 1 1 45 200" fill="none" stroke="#334A5D" stroke-width="48"/><path d="M200 21A179 179 0 1 1 21 200M200 69A131 131 0 1 1 69 200" fill="none" stroke="#CBD5E1" stroke-width="2"/><path d="M200 45A155 155 0 1 1 45 200" fill="none" stroke="#F3D177" stroke-width="2" stroke-dasharray="10 9"/><path d="M190 200H210M200 190V210" stroke="#D7E7EC" stroke-width="3"/><circle cx="200" cy="200" r="3" fill="#D7E7EC"/>'

for name,color in [('velocity-green','#34D399'),('net-force-blue','#38BDF8')]:
    part('physics.vector-arrow.'+name+'.v1',240,48,arrow((0,24),(238,24),color,24,7),
         {'vector_tail':[0,24],'vector_tip':[238,24],'arrowhead_base':[214,24]},'Horizontal +x arrow, tail at left-center pivot; scene rotation determines its direction.')
part('notation.force-vector-arrow.v1',240,48,'<g color="#E7E2C7">'+arrow((0,24),(238,24),'currentColor',24,7)+'</g>',
     {'vector_tail':[0,24],'vector_tip':[238,24]},'Horizontal +x tail-pivot arrow. Explicit currentColor placeholders are recolored by the opt-in renderer adapter. Default color is neutral; scale and rotation are controlled by the scene.')
part('physics.ball.ghost.v1',100,100,'<circle cx="50" cy="50" r="44" fill="#DBF3FF" fill-opacity="0.14" stroke="#D0EFFF" stroke-width="4" stroke-dasharray="8 5"/>',
     {'ball_center':[50,50],'outer_ball_radius':46},'Dashed, translucent ghost ball for the hypothetical no-force trajectory; not a second physical ball.')
for name,separation in [('wide',60),('close',12)]:
    origin=[280,190];length=110
    earlier=[origin[0]+length*math.cos(math.radians(-90+separation/2)),origin[1]+length*math.sin(math.radians(-90+separation/2))]
    later=[origin[0]+length*math.cos(math.radians(-90-separation/2)),origin[1]+length*math.sin(math.radians(-90-separation/2))]
    assert abs(math.dist(origin,earlier)-math.dist(origin,later))<1e-8 and later[0]<earlier[0]
    body='<path d="M280 190H160" stroke="#7FA7BA" stroke-width="2" stroke-dasharray="5 5"/>'+arrow(origin,earlier,'#34D399',10)+arrow(origin,later,'#34D399',10)+arrow(earlier,later,'#38BDF8',7)+ '<circle cx="280" cy="190" r="4" fill="#E6F2FA"/>'
    part('physics.delta-v-vector-subtraction.'+name+'.v1',400,260,body,
         {'common_vector_tail':origin,'earlier_velocity_tip':earlier,'later_velocity_tip':later,'delta_v_tail':earlier,'delta_v_tip':later,'inward_reference_direction':[-1,0],'angular_separation_degrees':separation},
         'Equal-length sampled velocity vectors share a common origin. Blue delta-v runs from earlier tip to later tip; left is inward for the depicted right-side orbital position. Static teaching diagram; native labels are separate.')
earth='<circle cx="200" cy="200" r="48" fill="#276B8E" stroke="#A6D5E7" stroke-width="2"/><path d="M166 182L177 164L202 161L215 178L205 193L220 209L205 230L181 217L173 197Z" fill="#70B699"/>'
part('physics.earth-moon-orbit-diagram.v1',400,400,'<circle cx="200" cy="200" r="130" fill="none" stroke="#91B8CD" stroke-width="2" stroke-dasharray="7 6"/>'+earth+'<circle cx="330" cy="200" r="18" fill="#CED5DD" stroke="#F1F4F6" stroke-width="2"/><circle cx="326" cy="195" r="4" fill="#A9B3BF"/>',
     {'earth_center':[200,200],'moon_center':[330,200],'orbit_radius':130,'moon_inward_direction':[-1,0]},'Schematic circular Earth–Moon orbit; sizes and distances are not to scale. Gravity arrows are separate overlays.')
part('environment.road.curve-topdown.v1',400,400,road_curve(),{'center_of_curvature':[200,200],'inner_radius':131,'outer_radius':179,'road_centerline_radius':155,'rightmost_road_point':[355,200]},'Level-road plan view; center cross fixes inward direction. No banking, vehicles or force vectors are baked in.')
part('vehicle.car.top-down.v1',160,90,car_top(),{'body_center':[80,45],'front_end':[154,45],'rear_end':[18,45],'wheel_centers':[[43,12],[125,12],[43,78],[125,78]]},'Top-down car, nose toward +x; the planner rotates it to the road tangent.')
part('physics.car.level-curve-topdown.v1',400,400,road_curve()+'<g transform="translate(355 200) rotate(-90) scale(.44) translate(-80 -45)">'+car_top()+'</g>',
     {'curve_center':[200,200],'car_center':[355,200],'road_radius':155,'inward_direction_at_car':[-1,0],'car_forward_direction':[0,-1]},'Level-road context with car pointing along the upward tangent at the curve’s rightmost point. Friction vector and labels are separate.')
part('vehicle.car.side-profile.v1',180,100,'<path d="M15 67L25 44L51 40L69 20L121 20L146 42L165 48L168 69Z" fill="#E8AF5A" stroke="#F8E0AE" stroke-width="2"/><path d="M71 26L93 26V41H56Z M99 26H118L134 41H99Z" fill="#193D54"/><circle cx="44" cy="70" r="14" fill="#17212C" stroke="#B9C7D1" stroke-width="3"/><circle cx="139" cy="70" r="14" fill="#17212C" stroke="#B9C7D1" stroke-width="3"/><circle cx="44" cy="70" r="5" fill="#8B9BAA"/><circle cx="139" cy="70" r="5" fill="#8B9BAA"/>',
     {'body_center':[90,48],'front_end':[168,57],'rear_end':[15,57],'wheel_contact_points':[[44,85.5],[139,85.5]]},'Side-profile car, nose toward +x, level tire contacts; force vectors are separate.')
part('environment.road.banked-cross-section.v1',420,200,'<path d="M40 155L380 65L380 85L40 175Z" fill="#334A5D"/><path d="M40 155L380 65" fill="none" stroke="#D6E5EC" stroke-width="4"/><path d="M40 155H380" stroke="#A6B8C5" stroke-width="2" stroke-dasharray="7 6"/><path d="M84 155A44 44 0 0 0 82.53 143.74" fill="none" stroke="#F3D177" stroke-width="3"/><circle cx="40" cy="155" r="4" fill="#7FE5C0"/><circle cx="380" cy="65" r="4" fill="#F3D177"/>',
     {'low_inner_edge':[40,155],'raised_outer_edge':[380,65],'horizontal_reference':[[40,155],[380,155]],'banking_angle_degrees':math.degrees(math.atan2(90,340))},'Inner edge is low at left, outer edge raised at right. Angle arc is qualitative; native theta label and force arrows are separate. The planner must align tires to this surface.')
part('vehicle.car.banked-cross-section.v1',160,110,car_front(),{'body_center':[80,62],'tire_contact_points':[[38,97],[122,97]],'upper_body':[80,27],'road_facing_underside':[80,97]},'Front/cross-section car with equal level tire contacts. Rotate the entire car to match the inclined road; no second intrinsic banking angle is baked into this reusable part.')
theta=math.radians(20);origin=[200-21*math.sin(theta),200-21*math.cos(theta)];normal=[origin[0]-135*math.sin(theta),origin[1]-135*math.cos(theta)];weight=[origin[0],origin[1]+135*math.cos(theta)];horizontal=[normal[0],origin[1]];vertical=[origin[0],normal[1]]
assert abs((vertical[1]-origin[1])+(weight[1]-origin[1]))<1e-8
body='<path d="M40 258.24L360 141.76" stroke="#CBD5E1" stroke-width="5"/><path d="M40 258.24H360" stroke="#718A9F" stroke-width="2" stroke-dasharray="6 6"/><g transform="translate(200 200) rotate(-20) scale(.6) translate(-80 -97)">'+car_front()+'</g>'+arrow(origin,normal,'#E7E2C7',12)+arrow(origin,weight,'#E7E2C7',12)+arrow(origin,horizontal,'#38BDF8',9)+arrow(origin,vertical,'#B9CED6',9)+'<path d="M'+str(horizontal[0])+' '+str(horizontal[1])+'V'+str(normal[1])+'H'+str(vertical[0])+'" fill="none" stroke="#97AFC0" stroke-width="1.5" stroke-dasharray="5 5"/>'
part('physics.banked-curve-force-diagram.v1',400,360,body,{'car_center':origin,'road_midpoint':[200,200],'normal_tail':origin,'normal_tip':normal,'gravity_tail':origin,'gravity_tip':weight,'normal_horizontal_tip':horizontal,'normal_vertical_tip':vertical,'banking_angle_degrees':20},'Ideal frictionless banked curve: the upward-left normal splits into an upward component balancing weight and a leftward inward net-force component. Vector lengths are proportional; geometry is illustrative, labels are separate.')
weather='<defs><clipPath id="weather-globe"><ellipse cx="240" cy="240" rx="176" ry="173"/></clipPath><marker id="weather-tip" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="4" markerHeight="4" orient="auto"><path d="M0 0L10 5L0 10Z" fill="#F3D177"/></marker></defs><g transform="rotate(-18 240 240)" clip-path="url(#weather-globe)"><ellipse cx="240" cy="240" rx="176" ry="44" fill="none" stroke="#D6F4FA" stroke-width="2" stroke-dasharray="6 6"/><path d="M82 160Q240 202 398 160M82 320Q240 362 398 320" fill="none" stroke="#9ACFE2" stroke-width="2" stroke-dasharray="4 6"/><path d="M300 240C300 200 310 170 340 145" fill="none" stroke="#A9E8CA" stroke-width="4" marker-end="url(#weather-tip)"/><path d="M206 150A26 26 0 1 0 180 176" fill="none" stroke="#F3D177" stroke-width="4" marker-end="url(#weather-tip)"/><path d="M296 320A26 26 0 1 1 270 294" fill="none" stroke="#F3D177" stroke-width="4" marker-end="url(#weather-tip)"/></g>'
part('earth.rotating-globe.weather-overlay.v1',480,480,weather,{'globe_center':[240,240],'unrotated_equator_endpoints':[[64,240],[416,240]],'unrotated_parcel_start':[300,240],'unrotated_parcel_end':[340,145],'unrotated_northern_cyclone_center':[180,150],'unrotated_southern_cyclone_center':[270,320],'intrinsic_tilt_degrees':-18},'Same canvas, center pivot and intrinsic tilt as earth.rotating-globe.v1. Northern cyclone arrow is counterclockwise, southern clockwise as viewed from above. Parcel curves eastward during northward travel; all paths are qualitative, not a solver. Attach overlay with identical size/pivot and synchronized rotation to the globe.')
for name,curve in [('flat','M48 110H272'),('concave','M48 80Q160 180 272 80')]:
    body='<path d="'+curve+'L272 192H48Z" fill="#408FC5" fill-opacity=".38"/><path d="'+curve+'" fill="none" stroke="#87D7FF" stroke-width="5"/>'
    landmarks={'left_surface_endpoint':[48,110 if name=='flat' else 80],'right_surface_endpoint':[272,110 if name=='flat' else 80],'surface_midpoint':[160,110 if name=='flat' else 130],'water_bottom':[160,192]}
    part('physics.newton-bucket-'+name+'-water-profile.v1',320,220,body,landmarks,'Static '+name+' water-surface cross-section, matching the other profile’s canvas and pivot. Register inside the bucket outline; this illustration does not simulate fluid deformation.')
stars=''.join(f'<circle cx="{x}" cy="{y}" r="{r}" fill="#D7E5ED"/>' for x,y,r in [(38,42,3),(108,79,2),(184,30,3),(242,62,2),(377,38,3),(472,68,2),(594,46,3),(558,151,2),(612,249,3),(512,303,2),(420,332,3),(80,310,3),(34,214,2),(145,335,2)])
part('physics.mach-principle-cosmic-concept.v1',640,360,stars+'<ellipse cx="320" cy="185" rx="88" ry="62" fill="none" stroke="#728C9E" stroke-width="2" stroke-dasharray="5 7"/><ellipse cx="320" cy="185" rx="38" ry="17" fill="none" stroke="#81D5C0" stroke-width="3"/><circle cx="320" cy="185" r="12" fill="#E8B875"/><path d="M320 145V225" stroke="#E8B875" stroke-width="2"/>',
     {'local_rotating_system_center':[320,185],'local_region_boundary_radii':[88,62],'distant_star_field':'outside local region'},'Conceptual juxtaposition of distant stars and a local rotating system. No connecting arrows or causal mechanism. Native narration/labels must identify Mach’s principle as a proposal.')
frames=''.join(f'<g transform="translate({x} {y}) rotate({angle})"><path d="M-14 0H14M0 -14V14" stroke="#ADDBE5" stroke-width="3"/><circle cx="0" cy="0" r="3" fill="#ADDBE5"/></g>' for x,y,angle in [(100,200,-12),(380,200,12),(240,76,18),(240,324,-18)])
part('physics.lense-thirring-frame-dragging-schematic.v1',480,400,'<ellipse cx="240" cy="200" rx="142" ry="92" fill="none" stroke="#6C9CBA" stroke-width="2" stroke-dasharray="6 6"/><ellipse cx="240" cy="200" rx="66" ry="64" fill="#376D8E" stroke="#ACD4E5" stroke-width="3"/><path d="M240 62V338" stroke="#F3D177" stroke-width="3" stroke-dasharray="6 6"/><path d="M170 184C184 151 282 147 310 181" fill="none" stroke="#9AE1CB" stroke-width="4"/>'+arrow((300,170),(310,181),'#9AE1CB',10)+frames,
     {'central_spinning_mass':[240,200],'spin_axis_endpoints':[[240,62],[240,338]],'nearby_reference_frame_centers':[[100,200],[380,200],[240,76],[240,324]]},'Qualitative spinning-mass and nearby frame-marker illustration. Marker angles and ring are schematic, not measured GR predictions or a physical simulation. Native labels must distinguish frame dragging from proof of Mach’s principle.')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requests',type=Path,required=True,help='Unique-asset grouped demands JSON')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    demands=read(args.requests);rows=[]
    write(args.output/'demands.json',demands)
    if set(demands)!=set(PARTS):raise ValueError('Asset definitions do not cover exactly the demands')
    for asset,requests in demands.items():
        p=PARTS[asset];w,h=p['width'],p['height'];name=asset.replace('.','-')
        pivot=requests[0]['svg_requirements']['pivot']
        if any(r['svg_requirements']['pivot']!=pivot for r in requests):raise ValueError('Conflicting pivot')
        caps=sorted(set.union(*(set(r['required_capabilities']) for r in requests)))
        svg=(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}"><title>{html.escape(asset)}</title><desc>{html.escape(p["notes"])}</desc>'+p['body']+'</svg>\n').encode()
        validate_svg(svg);target=args.output/(name+'.svg')
        if target.exists() and target.read_bytes()!=svg:raise ValueError('Immutable candidate differs: '+asset)
        target.write_bytes(svg)
        metadata={'pivot':pivot,'capabilities':caps,'semantic_landmarks':p['landmarks'],
            'provenance':'Original hand-authored circular-motion pilot asset. '+p['notes']+' Intrinsic SVG landmark coordinates: '+json.dumps(p['landmarks'],sort_keys=True)}
        write(args.output/(name+'.metadata.json'),metadata)
        rows.append({'asset_id':asset,'svg':target.name,'metadata':name+'.metadata.json','sha256':digest(svg),'notes':p['notes'],'landmarks':p['landmarks'],'instances':len(requests)})
    write(args.output/'authored-assets.json',{'objects':rows,'inspection':'pending','registration':'pending'})
    cards=''.join('<figure><div class="asset">'+(args.output/r['svg']).read_text()+'</div><figcaption><strong>'+html.escape(r['asset_id'])+'</strong><p>'+html.escape(r['notes'])+'</p></figcaption></figure>' for r in rows)
    (args.output/'report.html').write_text('<!doctype html><html><head><meta charset="utf-8"><title>Circular-motion asset cache</title><style>body{background:#101820;color:#e6f2fa;font:16px system-ui;margin:28px}main{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}figure{margin:0;padding:20px;border:1px solid #476478;border-radius:12px}.asset{height:260px;display:flex;align-items:center;justify-content:center}.asset svg{max-width:100%;max-height:100%}figcaption{overflow-wrap:anywhere}p{line-height:1.5;color:#b9cbd5}</style></head><body><h1>C01, C03 and C04: reusable components</h1><p>Intrinsic SVGs; placement and motion remain model planned.</p><main>'+cards+'</main></body></html>')
    write(args.output/'public-report.json',{'schema_version':'infofilm.public-experiment-report.v1','files':['report.html','authored-assets.json','contact-sheet.png','registration-receipts.json','cache-readiness.json']})
    print('Authored',len(rows),'assets')

if __name__=='__main__':main()
