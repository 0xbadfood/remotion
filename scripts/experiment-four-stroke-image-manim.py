#!/usr/bin/env python3
"""An isolated narration-timed raster-layer / Manim four-stroke engine pilot.

The teaching script, speech and MFA are reused. Source-pixel anchors are supplied
explicitly; a small ideal slider-crank controller derives every moving position.
This is a scoped educational mechanism, not arbitrary image understanding or an
LLM-authored animation. No production settings, services or GPU are touched.

  .venv-manim-pilot/bin/python scripts/experiment-four-stroke-image-manim.py \
    --scene artifacts/experiments/four-stroke-image-mggen-manim/scene.json \
    --timings artifacts/experiments/four-stroke-image-mggen-manim/narration-scenes.json \
    --output-dir artifacts/experiments/four-stroke-image-mggen-manim/manim
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import time

import numpy as np
from PIL import Image

SCHEMA = "infofilm.four-stroke-image-manim.v1"
COLOURS = {"intake": "#27B8D5", "compression": "#EAA841", "power": "#F36B47",
           "exhaust": "#AC9EC7", "overview": "#38BFA4", "cycle": "#4FA9ED"}
STROKE_TITLES = {"intake": "1  INTAKE", "compression": "2  COMPRESSION",
                 "power": "3  POWER", "exhaust": "4  EXHAUST"}
DEFAULT_LABELS = {
    "N01": ["A piston changes cylinder volume", "Valves control the gas flow", "A crankshaft turns motion into rotation"],
    "N02": ["Intake valve opens", "Piston travels down", "Fresh charge fills the cylinder"],
    "N03": ["Both valves closed", "Piston travels up", "The mixture is compressed"],
    "N04": ["Spark ignites the charge", "Expanding gas pushes the piston down", "The crankshaft receives the work"],
    "N05": ["Exhaust valve opens", "Piston travels up", "Burned gas leaves the cylinder"],
    "N06": ["Four strokes", "Two crankshaft revolutions", "One combustion event per cycle"],
    "N07": ["Intake → compression → power → exhaust", "One mechanism connects every stroke", "The cycle repeats"]}


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve_asset(value, base):
    path = Path(value)
    if path.is_absolute():
        return path
    candidate = base / path
    return candidate if candidate.exists() else path.resolve()


def slider_crank(theta, mechanism):
    """Source-image pixels; theta=0 is TDC, clockwise viewed from the front."""
    centre = np.asarray(mechanism["crank_center_px"], dtype=float)
    radius, length = float(mechanism["crank_radius_px"]), float(mechanism["rod_length_px"])
    x = float(mechanism.get("axis_x_px", centre[0]))
    if abs(x - centre[0]) > 1e-6:
        raise ValueError("This pilot uses an in-line crank axis")
    pin = centre + radius * np.array([math.sin(theta), -math.cos(theta)])
    wrist = np.array([x, centre[1] - radius * math.cos(theta)
                      - math.sqrt(length * length - radius * radius * math.sin(theta) ** 2)])
    return {"crank_pin_px": pin.tolist(), "wrist_pin_px": wrist.tolist(),
            "rod_length_px": float(np.linalg.norm(pin - wrist))}


def cycle_state(theta, mechanism):
    phase = float(theta) % (4 * math.pi)
    index = min(3, int(phase / math.pi))
    stroke = ("intake", "compression", "power", "exhaust")[index]
    progress = (phase - index * math.pi) / math.pi
    lift = float(mechanism.get("valve_lift_px", 12)) * max(0, math.sin(math.pi * progress))
    result = slider_crank(theta, mechanism)
    result.update(theta_rad=float(theta), stroke=stroke, stroke_progress=progress,
                  intake_lift_px=lift if stroke == "intake" else 0,
                  exhaust_lift_px=lift if stroke == "exhaust" else 0,
                  spark=stroke == "power" and progress < 0.075)
    return result


def validate_mechanism(scene):
    m = scene["mechanism"]
    r, length = float(m["crank_radius_px"]), float(m["rod_length_px"])
    if not (0 < r < length):
        raise ValueError("Slider-crank requires a positive crank radius shorter than its rod")
    states = [cycle_state(float(theta), m) for theta in np.linspace(0, 4 * math.pi, 721)]
    max_rod_error = max(abs(s["rod_length_px"] - length) for s in states)
    closure_error = float(np.linalg.norm(np.asarray(states[0]["wrist_pin_px"]) - states[-1]["wrist_pin_px"]))
    tdc, bdc = slider_crank(0, m), slider_crank(math.pi, m)
    measured_stroke = bdc["wrist_pin_px"][1] - tdc["wrist_pin_px"][1]
    checks = {"rod_length_preserved": max_rod_error < 1e-7,
              "stroke_equals_twice_crank_radius": abs(measured_stroke - 2*r) < 1e-7,
              "two_revolution_cycle_closes": closure_error < 1e-7,
              "intake_opens_only_on_intake": all(s["intake_lift_px"] == 0 or s["stroke"] == "intake" for s in states),
              "exhaust_opens_only_on_exhaust": all(s["exhaust_lift_px"] == 0 or s["stroke"] == "exhaust" for s in states),
              "compression_power_valves_closed": all(s["intake_lift_px"] == s["exhaust_lift_px"] == 0
                                                        for s in states if s["stroke"] in {"compression", "power"}),
              "spark_near_power_tdc": all(not s["spark"] or (s["stroke"] == "power" and s["stroke_progress"] < .075) for s in states)}
    if not all(checks.values()):
        raise ValueError(f"Ideal mechanism check failed: {checks}")
    return {"checks": checks, "max_rod_length_error_px": max_rod_error,
            "cycle_closure_error_px": closure_error, "piston_stroke_px": measured_stroke,
            "controller": "analytic ideal in-line slider-crank, four strokes per 720°",
            "limitations": ["Ideal valve timing; actual engines overlap valve events",
                            "No fluid/thermodynamic simulation; coloured gas and spark are teaching overlays",
                            "Narration duration slows or holds demonstrations; it is not a physical engine speed"]}


def normalise_timings(document, fps):
    scenes = document["scenes"]
    previous = 0.0
    result = []
    for index, item in enumerate(scenes):
        row = dict(item)
        start = float(row.get("start", row.get("start_seconds", 0)))
        end = float(row.get("end", row.get("end_seconds", 0)))
        if end <= start or abs(start-previous) > .04:
            raise ValueError("Narration scenes need increasing, contiguous intervals from time zero")
        row.update(start=start, end=end, start_frame=round(start*fps),
                   end_frame=(math.ceil(end*fps) if index == len(scenes)-1 else round(end*fps)))
        row["frames"] = row["end_frame"] - row["start_frame"]
        row["render_duration"] = row["frames"] / fps
        row.setdefault("id", f"N{index+1:02d}")
        row.setdefault("title", row["id"])
        row.setdefault("actions", row.get("semantic_actions", []))
        result.append(row)
        previous = end
    return result


def build_motion(row):
    """Narration agents may supply exact intervals; safe single-stroke defaults remain scoped."""
    identity = row["id"]
    defaults = {"N02": (0, math.pi), "N03": (math.pi, 2*math.pi),
                "N04": (2*math.pi, 3*math.pi), "N05": (3*math.pi, 4*math.pi)}
    supplied = row.get("motion", row.get("motion_segments", []))
    if supplied:
        return [{"start": float(segment["start"]), "end": float(segment["end"]),
                 "theta_start": math.radians(float(segment.get("theta_start_deg", 0))),
                 "theta_end": math.radians(float(segment.get("theta_end_deg", 720))),
                 "label": segment.get("label", "Narration demonstration")}
                for segment in supplied]
    start, end = row["start"], row["end"]
    if identity in defaults:
        first, last = defaults[identity]
        motion_actions = [a for a in row.get("actions", []) if a.get("kind") in
                          {"stroke", "stroke_demo", "piston_motion", "demonstrate_stroke", "main_stroke"}]
        if motion_actions:
            action = motion_actions[0]
            astart, aend = float(action["start"]), float(action["end"])
        else:
            astart, aend = start + .2*(end-start), start+.72*(end-start)
        segments = [{"start": astart, "end": max(astart+.2, aend), "theta_start": first,
                     "theta_end": last, "label": identity}]
        if identity == "N04":
            linkage = next((a for a in row.get("actions", []) if a.get("kind")=="linkage_motion"),None)
            if linkage:
                segments[0]["theta_end"] = first+.78*(last-first)
                segments.append({"start":float(linkage["start"]),"end":float(linkage["end"]),
                                 "theta_start":segments[0]["theta_end"],"theta_end":last,
                                 "label":"Power stroke: crankshaft driven through connecting rod"})
        return segments
    if identity == "N01":
        action=next((a for a in row.get("actions", []) if a.get("id")=="overview-piston"),None)
        segments = ([{"start":float(action["start"]),"end":float(action["end"]),
                      "theta_start":math.pi,"theta_end":3*math.pi,"label":"Piston up and down"}] if action else [])
        linkage=next((a for a in row.get("actions",[]) if a.get("id")=="overview-linkage"),None)
        if linkage:segments.append({"start":float(linkage["start"]),"end":float(linkage["end"]),
                                   "theta_start":3*math.pi,"theta_end":4*math.pi,"label":"Linked crankshaft rotation"})
        return segments
    if identity == "N06":
        segments=[]
        for i,a in enumerate(a for a in row.get("actions",[]) if a.get("kind")=="stroke_demo"):
            segments.append({"start":float(a["start"]),"end":float(a["end"]),
                             "theta_start":i*math.pi,"theta_end":(i+1)*math.pi,"label":a["label"]})
        cycles=[a for a in row.get("actions",[]) if a.get("kind")=="cycle_two_revolutions"]
        if cycles:
            a=cycles[0];segments.append({"start":float(a["start"]),"end":float(a["end"]),
                                        "theta_start":4*math.pi,"theta_end":8*math.pi,"label":a["label"]})
        return segments
    actions=row.get("actions",[])
    coordinated=next((a for a in actions if a.get("kind")=="cycle_coordination"),None)
    revolutions=next((a for a in actions if a.get("kind")=="cycle_two_revolutions"),None)
    restart=next((a for a in actions if a.get("kind")=="cycle_restart"),None)
    segments=[]
    if coordinated and revolutions:
        segments.append({"start":float(coordinated["start"]),"end":float(revolutions["end"]),
                         "theta_start":0,"theta_end":4*math.pi,"label":"Coordinated four-stroke cycle"})
    if restart:
        segments.append({"start":float(restart["start"]),"end":float(restart["end"]),
                         "theta_start":4*math.pi,"theta_end":5*math.pi,"label":"Intake starts the next cycle"})
    return segments


def theta_at(time_seconds, motion):
    if not motion:
        return 0.0
    theta = motion[0]["theta_start"]
    for segment in motion:
        if time_seconds < segment["start"]:
            return theta
        if time_seconds <= segment["end"]:
            progress = (time_seconds-segment["start"])/(segment["end"]-segment["start"])
            return segment["theta_start"] + max(0, min(1, progress))*(segment["theta_end"]-segment["theta_start"])
        theta = segment["theta_end"]
    return theta


def probe_video(path):
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                           "-show_entries", "stream=width,height,avg_frame_rate,nb_read_frames,duration", "-of", "json", str(path)],
                          text=True, capture_output=True, check=True)
    return json.loads(proc.stdout)["streams"][0]


def prepare_assets(scene, scene_path, output):
    result = {}
    base = scene_path.parent
    for name, spec in scene["assets"].items():
        spec = {"path": spec} if isinstance(spec, str) else dict(spec)
        path = resolve_asset(spec["path"], base)
        image = Image.open(path).convert("RGBA")
        if name == "background":
            result[name] = {"path": path, "sha256": sha256(path)}
            continue
        alpha = image.getchannel("A")
        bounds = alpha.point(lambda p: 255 if p >= 8 else 0).getbbox()
        if not bounds or alpha.getextrema() == (255,255):
            raise ValueError(f"{name}: a non-empty transparent layer is required")
        full = image.size == (int(scene["canvas"]["width"]), int(scene["canvas"]["height"]))
        bbox = spec.get("bbox_xyxy_px", spec.get("bounding_box_xyxy_px", list(bounds) if full else None))
        if bbox is None:
            raise ValueError(f"{name}: cropped layer needs a source-image bbox")
        left, top, right, bottom = map(float, bbox)
        if full:
            source_size = [bounds[2]-bounds[0],bounds[3]-bounds[1]]
            source_centre = [(bounds[0]+bounds[2])/2,(bounds[1]+bounds[3])/2]
        else:
            source_size = [(right-left)*(bounds[2]-bounds[0])/image.width,
                           (bottom-top)*(bounds[3]-bounds[1])/image.height]
            source_centre = [left+(bounds[0]+bounds[2])/2*(right-left)/image.width,
                             top+(bounds[1]+bounds[3])/2*(bottom-top)/image.height]
        cropped = output / f"{name}-render-layer.png"
        image.crop(bounds).save(cropped)
        result[name] = {"path": cropped, "sha256":sha256(path), "source_path":str(path),
                        "size_px":source_size, "centre_px":source_centre, **spec}
        result[name]["path"] = cropped
    required = {"background","piston","intake_valve","exhaust_valve","crankshaft"}
    if not required <= result.keys():
        raise ValueError(f"Missing moving source layers: {required - result.keys()}")
    return result


def render_scene(args, scene, row, sprites, motion, fingerprint):
    import manim
    from manim import (Arrow, Circle, Group, ImageMobject, Line, Polygon,
                       Rectangle, RoundedRectangle, Scene, Text, UpdateFromAlphaFunc, VGroup, VMobject, linear, tempconfig)
    name = row["id"].lower()
    output = args.output_dir / name
    output.mkdir(parents=True,exist_ok=True)
    video, metric_path = output / f"{name}.mp4", output / "metrics.json"
    if video.exists() and metric_path.exists():
        prior = read(metric_path)
        if prior.get("input_fingerprint") == fingerprint and prior.get("checks") and all(prior["checks"].values()):
            print(json.dumps({"scene":row["id"],"state":"cached","frames":row["frames"]}),flush=True)
            return prior
    width, height = float(scene["canvas"]["width"]),float(scene["canvas"]["height"])
    fh, fw = 8.0, 8.0*args.width/args.height
    scale = min(fw/width,fh/height)
    mechanism = scene["mechanism"]
    ref_theta = math.radians(float(mechanism.get("piston_reference_theta_deg",0)))
    ref_wrist = np.asarray(sprites["piston"].get("wrist_pin_px",slider_crank(ref_theta,mechanism)["wrist_pin_px"]))
    crown_offset = float(mechanism.get("piston_crown_offset_px",sprites["piston"].get("crown_y_px",sprites["piston"]["centre_px"][1]-sprites["piston"]["size_px"][1]/2)-ref_wrist[1]))
    bore = list(map(float,mechanism["bore_xyxy_px"] if "bore_xyxy_px" in mechanism else mechanism["bore"]))
    head_y = float(mechanism.get("chamber_head_y_px",bore[1]))
    spark_xy = mechanism.get("spark_tip_px",[mechanism["axis_x_px"],head_y+6])
    rodwidth = float(mechanism.get("rod_width_px",16))*scale/.01
    samples=[]
    stroke = row.get("stroke") or {"N02":"intake","N03":"compression","N04":"power","N05":"exhaust"}.get(row["id"],"cycle" if row["id"] in {"N06","N07"} else "overview")
    labels = row.get("display_labels",DEFAULT_LABELS.get(row["id"],[]))
    if labels and isinstance(labels[0],dict):
        labels=[x.get("text",x.get("label","")) for x in labels]
    colour=COLOURS[stroke]

    def point(xy):
        return np.array([(float(xy[0])-width/2)*scale,(height/2-float(xy[1]))*scale,0])

    def card(text, fs=20, col="#DDE7F0", fill="#102536", width=None):
        label=Text(str(text),font="DejaVu Sans",font_size=fs,color=col,weight="SEMIBOLD")
        if width and label.width > width-.4:
            label.scale((width-.4)/label.width)
        panel=RoundedRectangle(corner_radius=.12,width=width or label.width+.40,height=label.height+.27,
                               fill_color=fill,fill_opacity=.96,stroke_color=col,stroke_opacity=.18,stroke_width=1)
        return VGroup(panel,label).set_z_index(30)

    class EngineScene(Scene):
        def construct(self):
            bg=ImageMobject(str(sprites["background"]["path"])).set_z_index(-20)
            bg.width,bg.height=width*scale,height*scale
            bg.move_to(np.zeros(3));self.add(bg)
            for cavity in scene.get("cavity_masks",[]):
                if cavity["type"]=="rectangle":
                    box=cavity["bbox_xyxy_px"]
                    fill=Rectangle(width=(box[2]-box[0])*scale,height=(box[3]-box[1])*scale)
                    fill.move_to(point([(box[0]+box[2])/2,(box[1]+box[3])/2]))
                elif cavity["type"]=="circle":
                    fill=Circle(radius=cavity["radius_px"]*scale).move_to(point(cavity["center_px"]))
                else:raise ValueError("Supported cavity masks are rectangle and circle")
                fill.set_fill(cavity.get("colour","#DDDAD1"),1).set_stroke(opacity=0).set_z_index(-15)
                self.add(fill)
            sidebar_x=float(scene.get("layout",{}).get("sidebar_x_fraction",.80))*width
            title=card(row["title"],29,"#FFFFFF",width=4.3)
            title.move_to(point([sidebar_x,height*.13]));self.add(title)
            header=card(f"{int(row['id'][1:])}/7   •   FOUR-STROKE ENGINE",14,colour,width=4.3)
            header.move_to(point([sidebar_x,height*.205]));self.add(header)
            sidebar_point=point([sidebar_x,height*.46])
            info=VGroup(*[card(text,18,colour,width=3.6) for text in labels])
            info.arrange(np.array([0,-1,0]),buff=.20).move_to(sidebar_point)
            self.add(info)
            cue_cards={a["id"]:card(a.get("label",""),16,"#FFFFFF",width=4.25)
                       for a in row.get("actions",[]) if a.get("label")}
            for cue in cue_cards.values():
                cue.move_to(point([sidebar_x,height*.63])).set_opacity(0)
                self.add(cue)
            foot=card("Ideal cutaway • motion slowed to match the explanation",13,"#CBDBE7")
            foot.move_to([0,-fh/2+.28,0]);self.add(foot)

            image_objects={}
            for role in ("crankshaft","piston","intake_valve","exhaust_valve"):
                spec=sprites[role]
                image=ImageMobject(str(spec["path"]));image.width=spec["size_px"][0]*scale;image.height=spec["size_px"][1]*scale
                image.move_to(point(spec["centre_px"])).set_z_index(6 if "valve" in role else 5)
                image_objects[role]=image
            crank_template=image_objects["crankshaft"].copy()
            crank_center=point(mechanism["crank_center_px"])
            if sprites["crankshaft"].get("rotation_anchor_px"):
                crank_center=point(sprites["crankshaft"]["rotation_anchor_px"])
            self.add(image_objects["crankshaft"])
            gas=Rectangle(width=max(.01,(bore[2]-bore[0]-6)*scale),height=.1,stroke_width=0,
                          fill_opacity=.25,fill_color=COLOURS["intake"]).set_z_index(1)
            self.add(gas)
            # The thin two-tone rod is reconstructed from the two mechanical
            # anchors; source provenance records this instead of inventing a
            # missing hidden segment in the SAM mask.
            rod=Line(point(ref_wrist),crank_center,stroke_width=rodwidth,color="#314E63").set_z_index(7)
            rodlight=rod.copy().set_stroke("#C3D4DA",max(1,rodwidth*.35)).set_z_index(8)
            wristdot=Circle(radius=6*scale,fill_color="#E7C785",fill_opacity=1,stroke_width=1,color="#263D50").set_z_index(10)
            pindot=wristdot.copy()
            self.add(rod,rodlight,image_objects["piston"],image_objects["intake_valve"],image_objects["exhaust_valve"],wristdot,pindot)
            rod_sprite=None
            if "connecting_rod" in sprites:
                spec=sprites["connecting_rod"]
                rod_sprite=ImageMobject(str(spec["path"]))
                rod_sprite.width=spec["size_px"][0]*scale;rod_sprite.height=spec["size_px"][1]*scale
                rod_sprite.move_to(point(spec["centre_px"])).set_z_index(7)
                rod_template=rod_sprite.copy()
                rod_source_a=point(spec["endpoint_a_px"]);rod_source_b=point(spec["endpoint_b_px"])
                rod_source_vector=rod_source_b-rod_source_a
                rod_source_angle=math.atan2(rod_source_vector[1],rod_source_vector[0])
                rod_source_length=np.linalg.norm(rod_source_vector)
                self.add(rod_sprite)
                rod.set_opacity(0);rodlight.set_opacity(0)
            spark_points=[]
            for i in range(16):
                angle=i*math.pi/8
                radius=(13 if i%2==0 else 5)*scale
                spark_points.append(point(spark_xy)+radius*np.array([math.cos(angle),math.sin(angle),0]))
            spark=Polygon(*spark_points,fill_color="#FFF35A",fill_opacity=1,color="#FFE07D",stroke_width=1).set_z_index(15)
            self.add(spark)
            valve_state=VGroup(card("INTAKE",13,COLOURS["intake"]),card("EXHAUST",13,COLOURS["exhaust"]))
            valve_state.arrange(np.array([1,0,0]),buff=.15).move_to(point([sidebar_x,height*.72]));self.add(valve_state)
            chips=VGroup(*[card(STROKE_TITLES[s],13,COLOURS[s],width=2.0) for s in STROKE_TITLES])
            chips.arrange(np.array([0,-1,0]),buff=.11).move_to(point([sidebar_x,height*.78]))
            if row["id"] in {"N06","N07"}:
                self.remove(valve_state);self.add(chips)
            # Flow arrow only appears through the currently open valve. It
            # carries no claim of simulated fluid dynamics.
            flow=Arrow(point([bore[0]+12,head_y+8]),point([bore[0]+12,head_y+45]),buff=0,
                       color=COLOURS["intake"],stroke_width=4,max_tip_length_to_length_ratio=.35).set_z_index(12)
            port_paths={}
            channel_geometry={"intake":[[383,155],[440,155],[507,177],[551,209]],
                              "exhaust":[[733,209],[795,177],[852,155],[909,155]]}
            for phase,coordinates in channel_geometry.items():
                path=VMobject().set_points_smoothly([point(xy) for xy in coordinates])
                path.set_stroke(COLOURS[phase],4,opacity=0).set_z_index(11)
                port_paths[phase]=path;self.add(path)
            moving=Group(*image_objects.values(),gas,rod,rodlight,wristdot,pindot,spark,flow)
            if rod_sprite is not None:moving.add(rod_sprite)
            initial_theta=theta_at(row["start"],motion)
            last_record=-1
            def update(_group,alpha):
                nonlocal last_record
                now=row["start"]+alpha*(row["end"]-row["start"])
                theta=theta_at(now,motion)
                state=cycle_state(theta,mechanism)
                wrist=np.asarray(state["wrist_pin_px"]);pin=np.asarray(state["crank_pin_px"])
                shift=wrist-ref_wrist
                image_objects["piston"].move_to(point(np.asarray(sprites["piston"]["centre_px"])+shift))
                image_objects["crankshaft"].become(crank_template.copy().rotate(-(theta-ref_theta),about_point=crank_center))
                for role,lift in (("intake_valve",state["intake_lift_px"]),("exhaust_valve",state["exhaust_lift_px"])):
                    image_objects[role].move_to(point(np.asarray(sprites[role]["centre_px"])+[0,lift]))
                rod.put_start_and_end_on(point(wrist),point(pin));rodlight.put_start_and_end_on(point(wrist),point(pin))
                if rod_sprite is not None:
                    current_vector=point(pin)-point(wrist)
                    ratio=np.linalg.norm(current_vector)/rod_source_length
                    angle=math.atan2(current_vector[1],current_vector[0])-rod_source_angle
                    transformed=rod_template.copy().scale(ratio,about_point=rod_source_a)
                    transformed.rotate(angle,about_point=rod_source_a).shift(point(wrist)-rod_source_a)
                    rod_sprite.become(transformed)
                wristdot.move_to(point(wrist));pindot.move_to(point(pin))
                crown=wrist[1]+crown_offset
                gas_bottom=min(bore[3],max(head_y+3,crown-2))
                gas.stretch_to_fit_height(max(.01,(gas_bottom-head_y)*scale))
                gas.move_to(point([(bore[0]+bore[2])/2,(head_y+gas_bottom)/2]))
                visible_stroke = row.get("stroke") or state["stroke"]
                gas.set_fill(COLOURS[visible_stroke],.24 if visible_stroke!="power" else .36)
                if row["id"] == "N01": gas.set_opacity(0)
                spark_cue = any(a.get("kind") in {"spark","ignition"} and float(a["start"]) <= now <= float(a["end"])
                                for a in row.get("actions",[]))
                spark_on = spark_cue if row["id"] in {"N03","N04"} else (state["spark"] and row["id"] in {"N06","N07"})
                spark.set_opacity(.95 if spark_on else 0)
                flow_open=state["intake_lift_px"]>.35 or state["exhaust_lift_px"]>.35
                if flow_open:
                    role="intake_valve" if state["stroke"]=="intake" else "exhaust_valve"
                    valve_x=float(sprites[role]["centre_px"][0])
                    a=[valve_x,head_y+2];b=[valve_x,head_y+max(4,min(52,(gas_bottom-head_y)*.85))]
                    if state["stroke"]=="exhaust":a,b=b,a
                    flow.put_start_and_end_on(point(a),point(b));flow.set_color(COLOURS[state["stroke"]])
                flow.set_opacity(.9 if flow_open and row["id"]!="N01" else 0)
                active=[a for a in row.get("actions",[]) if a["id"] in cue_cards and float(a["start"])<=now<=float(a["end"])]
                # Prefer the shortest phrase cue over a broad stroke interval.
                active_cue=min(active,key=lambda a:float(a["end"])-float(a["start"]))["id"] if active else None
                for cue_id,cue in cue_cards.items():cue.set_opacity(1 if cue_id==active_cue else 0)
                for phase,path in port_paths.items():
                    path.set_stroke(opacity=.65 if flow_open and state["stroke"]==phase and row["id"]!="N01" else 0)
                named_stage=next((a.get("stroke") for a in active if a.get("kind")=="stage_label"),None)
                chip_stage=named_stage or visible_stroke
                for index,s in enumerate(STROKE_TITLES):
                    chips[index].set_opacity(1 if chip_stage==s else .32)
                if row["id"] not in {"N06","N07"}:
                    for valve_i,lift,phase in ((0,state["intake_lift_px"],"intake"),(1,state["exhaust_lift_px"],"exhaust")):
                        valve_state[valve_i].set_opacity(1)
                        valve_state[valve_i][0].set_fill("#102536",.96)
                        valve_state[valve_i][0].set_stroke(COLOURS[phase],opacity=.9 if lift>.35 else .18)
                        valve_state[valve_i][1].set_color(COLOURS[phase] if lift>.35 else "#CBDBE7")
                # Sample mechanism transforms every half second. Actual video
                # frame counts are independently decoded by FFprobe below.
                record_idx=int(alpha*row["render_duration"]*2)
                if record_idx!=last_record:
                    samples.append({"narration_time_seconds":now,**state,"piston_crown_y_px":crown,"spark_visible":spark_on,
                                    "gas_box_xyxy_px":[bore[0]+3,head_y,bore[2]-3,gas_bottom],
                                    "rendered_gas_width_source_px":float(gas.width/scale),
                                    "rendered_gas_height_source_px":float(gas.height/scale)})
                    last_record=record_idx
            update(moving,0)
            self.play(UpdateFromAlphaFunc(moving,update),run_time=row["render_duration"]-1e-8,rate_func=linear)

    started=time.monotonic()
    with tempconfig({"renderer":"cairo","pixel_width":args.width,"pixel_height":args.height,
                     "frame_width":fw,"frame_height":fh,"frame_rate":args.fps,"background_color":"#F4F7FB",
                     "media_dir":str(output/"media"),"output_file":name,"write_to_movie":True,
                     "disable_caching":True,"verbosity":"WARNING"}):
        movie=EngineScene();movie.render();native=Path(movie.renderer.file_writer.movie_file_path)
    shutil.copy2(native,video)
    probe=probe_video(video)
    checks={"frame_count_correct":int(probe["nb_read_frames"])==row["frames"],
            "resolution_correct":(int(probe["width"]),int(probe["height"]))==(args.width,args.height),
            "duration_matches_mfa_window":abs(float(probe["duration"])-row["render_duration"])<=1/args.fps+.001,
            "gas_stays_inside_bore":all(bore[0]<=s["gas_box_xyxy_px"][0]<s["gas_box_xyxy_px"][2]<=bore[2]
                                         and bore[1]<=s["gas_box_xyxy_px"][1]<s["gas_box_xyxy_px"][3]<=bore[3] for s in samples),
            "rendered_gas_width_stays_inside_bore":all(abs(s["rendered_gas_width_source_px"]-(bore[2]-bore[0]-6))<1e-6 for s in samples),
            "rod_remains_connected":all(abs(s["rod_length_px"]-float(mechanism["rod_length_px"]))<1e-7 for s in samples)}
    stills=output/"stills";stills.mkdir(exist_ok=True)
    for i,fraction in enumerate((.12,.4,.68,.90)):
        subprocess.run(["ffmpeg","-y","-v","error","-ss",str(row["render_duration"]*fraction),"-i",str(video),
                        "-frames:v","1",str(stills/f"checkpoint-{i:02d}.png")],check=True)
    metric={"schema_version":SCHEMA,"scene_id":row["id"],"title":row["title"],"input_fingerprint":fingerprint,
            "manim_version":manim.__version__,"renderer":"cairo","seconds":time.monotonic()-started,
            "narration_window_seconds":[row["start"],row["end"]],"duration_seconds":row["render_duration"],
            "frames":row["frames"],"probe":probe,"checks":checks,"motion":motion,"video":str(video.resolve())}
    write(output/"positions.json",samples);write(metric_path,metric)
    print(json.dumps({"scene":row["id"],"state":"rendered","frames":row["frames"],"seconds":metric["seconds"],"checks":checks}),flush=True)
    if not all(checks.values()):
        raise RuntimeError(f"{row['id']}: render verification failed")
    return metric


def mux(args, results, audio_path):
    listing=args.output_dir/"concat.txt"
    listing.write_text("".join("file '"+str(Path(item["video"]).resolve()).replace("'","'\\''")+"'\n" for item in results))
    silent=args.output_dir/"four-stroke-silent.mp4"
    subprocess.run(["ffmpeg","-y","-v","error","-f","concat","-safe","0","-i",str(listing),"-c","copy",str(silent)],check=True)
    video=args.output_dir/"four-stroke-narrated.mp4"
    subprocess.run(["ffmpeg","-y","-v","error","-i",str(silent),"-i",str(audio_path),"-map","0:v:0","-map","1:a:0",
                    "-c:v","copy","-c:a","aac","-b:a","192k","-af","apad","-shortest","-movflags","+faststart",str(video)],check=True)
    audio_probe=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","json",str(audio_path)],
                               capture_output=True,text=True,check=True)
    audio_duration=float(json.loads(audio_probe.stdout)["format"]["duration"])
    probe=probe_video(video)
    all_streams=json.loads(subprocess.run(["ffprobe","-v","error","-show_streams","-of","json",str(video)],
                                         capture_output=True,text=True,check=True).stdout)["streams"]
    audio_streams=[stream for stream in all_streams if stream.get("codec_type")=="audio"]
    audio_stream=audio_streams[0] if audio_streams else {}
    audio_delivery_duration=float(audio_stream.get("duration",0))
    return {"video":str(video.resolve()),"silent_video":str(silent.resolve()),"probe":probe,
            "audio_stream":audio_stream,
            "source_audio":str(audio_path.resolve()),"source_audio_sha256":sha256(audio_path),
            "audio_duration_seconds":audio_duration,"audio_reused_without_TTS":True,
            "delivery_audio":"AAC 192 kbps; source WAV remains unchanged",
            "checks":{"audio_stream_present":len(audio_streams)==1,
                      "audio_stream_complete":abs(audio_delivery_duration-audio_duration)<2/args.fps+.01,
                      "duration_matches_audio":abs(float(probe["duration"])-audio_duration)<1/args.fps+.01,
                      "all_scene_frames_present":int(probe["nb_read_frames"])==sum(item["frames"] for item in results)}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene",type=Path,required=True);parser.add_argument("--timings",type=Path,required=True)
    parser.add_argument("--output-dir",type=Path,required=True);parser.add_argument("--audio",type=Path)
    parser.add_argument("--width",type=int,default=1280);parser.add_argument("--height",type=int,default=720)
    parser.add_argument("--fps",type=int,default=30);parser.add_argument("--check-only",action="store_true")
    parser.add_argument("--only-scene",action="append",help="Render selected scene IDs without concatenation/audio mux")
    args=parser.parse_args();args.output_dir=args.output_dir.resolve();args.output_dir.mkdir(parents=True,exist_ok=True)
    scene,timings=read(args.scene),read(args.timings)
    physics=validate_mechanism(scene);rows=normalise_timings(timings,args.fps)
    sprites=prepare_assets(scene,args.scene.resolve(),args.output_dir)
    audio_value=args.audio or timings.get("audio_path",timings.get("narration_audio"))
    if not audio_value:raise ValueError("An existing narration audio path is required")
    audio=resolve_asset(audio_value,args.timings.resolve().parent)
    expected_audio_sha=timings.get("audio_sha256")
    if expected_audio_sha and sha256(audio)!=expected_audio_sha:
        raise ValueError("Original narration WAV differs from the frozen MFA audio SHA")
    baseprint={"scene":scene,"timings":timings,"assets":{k:v["sha256"] for k,v in sprites.items()},
               "source_code":sha256(Path(__file__)),"quality":[args.width,args.height,args.fps]}
    check={"schema_version":SCHEMA,"physics":physics,"scene_count":len(rows),"audio_path":str(audio),
           "duration_seconds":rows[-1]["end"],"frames":sum(r["frames"] for r in rows),"scenes":rows,
           "provenance":{"background":"generated raster with moving parts removed by MG-Gen/SAM/LaMa",
                         "cavity_repairs":scene.get("cavity_masks",[]),
                         "source_anchor_geometry":"Explicit image-measured source-pixel anchors; pilot adapter authored by hand",
                         "moving_parts":"source-extracted transparent raster layers",
                         "rod":("source-extracted raster rod transformed between source anchor endpoints" if "connecting_rod" in sprites
                                else "code-reconstructed straight connector between wrist pin and crank pin"),
                         "motion":"handwritten scoped ideal slider-crank controller, not LLM motion keyframes"}}
    write(args.output_dir/"preflight.json",check)
    if args.check_only:
        print(json.dumps(check),flush=True);return 0
    results=[]
    started=time.monotonic()
    selected_rows=[r for r in rows if not args.only_scene or r["id"] in args.only_scene]
    if not selected_rows:raise ValueError("Requested scene IDs are absent")
    for row in selected_rows:
        fingerprint=hashlib.sha256(json.dumps({**baseprint,"row":row},sort_keys=True).encode()).hexdigest()
        print(json.dumps({"scene":row["id"],"state":"rendering","position":len(results)+1,"total":len(rows)}),flush=True)
        results.append(render_scene(args,scene,row,sprites,build_motion(row),fingerprint))
    if args.only_scene:
        write(args.output_dir/"partial-render-results.json",{"schema_version":SCHEMA,"physics":physics,"scenes":results,"status":"partial"})
        return 0
    delivery=mux(args,results,audio)
    result={"schema_version":SCHEMA,"status":"passed" if all(delivery["checks"].values()) else "failed",
            "elapsed_seconds":time.monotonic()-started,"physics":physics,"scenes":results,"delivery":delivery}
    write(args.output_dir/"render-results.json",result)
    print(json.dumps({"state":result["status"],"video":delivery["video"],"elapsed_seconds":result["elapsed_seconds"]}),flush=True)
    return 0 if result["status"]=="passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
