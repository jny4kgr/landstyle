#!/usr/bin/env python3
"""物件JSONのメートル座標から区画図SVGを作る。北角は上から時計回り。"""
import sys
sys.dont_write_bytecode = True
import argparse
import html
import json
import math
from decimal import Decimal, ROUND_DOWN
from pathlib import Path


def polygon_points(value):
    if not isinstance(value, list) or len(value) < 3:
        raise ValueError('polygon は3点以上必要です')
    points = []
    for point in value:
        if not isinstance(point, list) or len(point) != 2 or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in point):
            raise ValueError('polygon の点は有限の数値 [x, y] で指定してください')
        points.append(tuple(point))
    if points[0] == points[-1]:
        points.pop()
    if len(points) < 3 or polygon_area(points) <= 0:
        raise ValueError('polygon の面積は正である必要があります')
    return points


def polygon_area(points):
    return abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(points, points[1:]+points[:1]))) / 2


def plan_issues(data):
    plan = data.get('site_plan')
    if plan is None:
        return []
    issues = []
    def add(path, message, level='warning'):
        issues.append(dict(id='SITE_PLAN', path=path, level=level, message=message, question='区画名・求積図の座標と土地面積を確認してください。'))
    if not isinstance(plan, dict):
        add('site_plan', 'site_plan はオブジェクトで指定してください。', 'error')
        return issues
    units = {u.get('name'): u for u in data.get('buildings', []) + data.get('lands', [])}
    north = plan.get('north_deg', 0)
    if isinstance(north, bool) or not isinstance(north, (int, float)) or not math.isfinite(north):
        add('site_plan.north_deg', '方位角は有限の数値で指定してください。', 'error')
    if not plan.get('lots'):
        add('site_plan.lots', '区画がありません。', 'error')
    for kind in ['roads', 'lots']:
        rows = plan.get(kind, [])
        if not isinstance(rows, list):
            add('site_plan.'+kind, '配列で指定してください。', 'error')
            continue
        for i, row in enumerate(rows):
            path = f'site_plan.{kind}[{i}]'
            try:
                points = polygon_points(row.get('polygon'))
            except (ValueError, AttributeError) as exc:
                add(path+'.polygon', str(exc), 'error')
                continue
            if kind == 'lots':
                unit = units.get(row.get('name'))
                if unit is None:
                    add(path+'.name', f'区画名「{row.get("name", "")}」が buildings / lands にありません。')
                else:
                    area = unit.get('land_area')
                    if isinstance(area, (int, float)) and area > 0 and abs(polygon_area(points)-area)/area >= .03:
                        add(path+'.polygon', f'区画「{row.get("name")}」の計算面積 {polygon_area(points):.2f}㎡ が land_area {area}㎡ と3%以上ずれています。')
    return issues


def render_siteplan(data, rotation='auto', frame_mm=(60, 45)):
    """Fit all drawing bounds + 6% per side; preserve minimum printed type sizes.

    rotation: auto / 0 / 90 (clockwise). Text stays upright; north follows geometry.
    frame_mm is the actual object-fit:contain cell, not the surrounding section.
    """
    problems = plan_issues(data)
    if any(p['level']=='error' for p in problems):
        raise ValueError(' / '.join(p['message'] for p in problems if p['level']=='error'))
    if str(rotation) not in {'auto', '0', '90'}:
        raise ValueError('rotate は auto / 0 / 90 で指定してください')
    if len(frame_mm) != 2 or any(not math.isfinite(n) or n <= 0 for n in frame_mm):
        raise ValueError('frame は正の幅x高さ（mm）で指定してください')
    from make_map import japanese_font
    font = japanese_font(1000)
    plan = data['site_plan']
    units = {u.get('name'): (u,kind) for kind in ['buildings','lands'] for u in data.get(kind,[])}
    rows = [(kind, row, polygon_points(row['polygon']))
            for kind in ['roads', 'lots'] for row in plan.get(kind, [])]
    metrics = {}

    def candidate(degrees):
        # Rotate the geometry first, then lay out upright labels in screen space.
        def xy(p): return (p[0], -p[1]) if degrees == 0 else (p[1], p[0])
        points = [xy(p) for _, _, pp in rows for p in pp]
        gx0, gy0 = min(p[0] for p in points), min(p[1] for p in points)
        gx1, gy1 = max(p[0] for p in points), max(p[1] for p in points)
        units_per_mm = max((gx1-gx0)/frame_mm[0], (gy1-gy0)/frame_mm[1])

        def draw(units_per_mm):
            # Slight headroom over the requested 6.5 / 5.5pt for rounding.
            label_size = 6.6 * 25.4 / 72 * units_per_mm
            dim_size = 5.6 * 25.4 / 72 * units_per_mm
            stroke = .18 * units_per_mm
            svg, bounds = [], []
            dimensions, seen_edges = [], set()

            def include(points, pad=0):
                bounds.extend([(x-pad,y-pad) for x,y in points])
                bounds.extend([(x+pad,y+pad) for x,y in points])

            def text(x, y, label, size, role, angle=0, color='#222', halo=False):
                label = str(label)
                if label not in metrics:
                    # Match the SVG system font. Include both advance and ink bounds.
                    box = font.getbbox(label, anchor='ls')
                    advance = font.getlength(label)
                    metrics[label] = ((min(0,box[0])-advance/2)/1000, box[1]/1000,
                                      (max(advance,box[2])-advance/2)/1000, box[3]/1000)
                left, top, right, bottom = [n*size for n in metrics[label]]
                rad = math.radians(angle)
                corners = [(x+a*math.cos(rad)-b*math.sin(rad),
                            y+a*math.sin(rad)+b*math.cos(rad))
                           for a,b in [(left,top),(right,top),(right,bottom),(left,bottom)]]
                include(corners, stroke if halo else 0)
                extra = f' transform="rotate({angle:.6f} {x:.6f} {y:.6f})"' if angle else ''
                if halo: extra += f' paint-order="stroke" stroke="white" stroke-width="{stroke*2:.6f}" stroke-linejoin="round"'
                svg.append(f'<text data-role="{role}" x="{x:.6f}" y="{y:.6f}" font-size="{size:.6f}" text-anchor="middle" fill="{color}"{extra}>{html.escape(label)}</text>')

            for kind, row, pp in rows:
                screen = [xy(p) for p in pp]
                cx, cy = sum(p[0] for p in screen)/len(screen), sum(p[1] for p in screen)/len(screen)
                unit, unit_kind = units.get(row.get('name'), ({}, ''))
                fill = '#dedede' if kind=='roads' else '#e6f0d5' if unit_kind=='lands' else 'white'
                thick = stroke * (2 if unit_kind=='buildings' and unit.get('status')=='販売中' else 1)
                include(screen, thick/2)
                svg.append(f'<polygon points="{" ".join(f"{x:.6f},{y:.6f}" for x,y in screen)}" fill="{fill}" stroke="#333" stroke-width="{thick:.6f}"/>')
                if kind=='roads':
                    # Follow a narrow road's long axis while keeping the reading direction upright.
                    rw=max(p[0] for p in screen)-min(p[0] for p in screen)
                    rh=max(p[1] for p in screen)-min(p[1] for p in screen)
                    text(cx,cy+dim_size*.35,row.get('label',''),dim_size,'road',-90 if rh>rw*2 else 0)
                    continue
                for i, (a,b) in enumerate(zip(pp, pp[1:]+pp[:1])):
                    p,q=screen[i],screen[(i+1)%len(screen)]
                    mx,my=(p[0]+q[0])/2,(p[1]+q[1])/2
                    edge=tuple(sorted((p,q)))
                    if edge in seen_edges:
                        continue  # One dimension on a shared boundary, not two colliding labels.
                    seen_edges.add(edge)
                    angle=math.degrees(math.atan2(q[1]-p[1],q[0]-p[0]))
                    if angle>90: angle-=180
                    if angle<-90: angle+=180
                    # Center the dimension's ink on its edge, leaving the lot interior
                    # available for readable names and areas. Bounds include the overhang.
                    rad=math.radians(angle)
                    mx-=math.sin(rad)*dim_size*.35
                    my+=math.cos(rad)*dim_size*.35
                    text(mx,my,f'約{math.dist(a,b):.2f}m',dim_size,'dimension',angle,halo=True)
                    dimensions.append(svg.pop())
                sold=unit.get('status')=='済'
                name_x=cx-label_size*.45 if sold else cx
                name_y=cy-label_size*1.1
                text(name_x,name_y,row.get('name',''),label_size,'lot-name',halo=True)
                area=unit.get('land_area')
                if isinstance(area,(int,float)) and area>0:
                    tsubo=(Decimal(str(area))*Decimal('.3025')).quantize(Decimal('.01'),rounding=ROUND_DOWN)
                    # Split the area to avoid squeezing three adjacent lots into tiny type.
                    text(cx,cy,f'約{area:.2f}㎡',label_size,'lot-area',halo=True)
                    text(cx,cy+label_size*1.1,f'(約{tsubo}坪)',label_size,'lot-area',halo=True)
                if sold:
                    sx,sy=cx+label_size*1.7,name_y-label_size*.38
                    radius=label_size*.65
                    include([(sx-radius,sy-radius),(sx+radius,sy+radius)])
                    svg.append(f'<circle data-role="sold" cx="{sx:.6f}" cy="{sy:.6f}" r="{radius:.6f}" fill="#d7141a"/>')
                    text(sx,sy+label_size*.32,'済',label_size*.85,'sold-label',color='white')
            svg.extend(dimensions)
            # North is just above the drawing, never in a fixed oversized canvas.
            nx,ny=gx1-label_size,gy0-label_size*1.3
            angle=plan.get('north_deg',0)+degrees
            rad=math.radians(angle)
            arrow=[(0,-label_size*.8),(-label_size*.35,label_size*.55),
                   (0,label_size*.3),(label_size*.35,label_size*.55)]
            arrow=[(nx+x*math.cos(rad)-y*math.sin(rad),ny+x*math.sin(rad)+y*math.cos(rad)) for x,y in arrow]
            include(arrow,stroke/2)
            svg.append(f'<polygon data-role="north" data-angle="{angle}" points="{" ".join(f"{x:.6f},{y:.6f}" for x,y in arrow)}" fill="white" stroke="#222" stroke-width="{stroke:.6f}"/>')
            svg.append(f'<polygon points="{" ".join(f"{x:.6f},{y:.6f}" for x,y in arrow[:3])}" fill="#222"/>')
            # Keep N readable while moving it to the rotated arrow tip.
            tipx,tipy=arrow[0]
            text(tipx+math.sin(rad)*label_size*.7,
                 tipy-math.cos(rad)*label_size*.7+dim_size*.35,'N',dim_size,'north-label')
            x0,y0=min(p[0] for p in bounds),min(p[1] for p in bounds)
            x1,y1=max(p[0] for p in bounds),max(p[1] for p in bounds)
            padx,pady=(x1-x0)*.06,(y1-y0)*.06
            view=(x0-padx,y0-pady,x1-x0+2*padx,y1-y0+2*pady)
            return svg,view

        # Text changes the bounds. Solve size using the final viewBox, including north,
        # rotated dimensions and margins, so contain's height limit is also respected.
        for _ in range(80):
            svg,view=draw(units_per_mm)
            needed=max(view[2]/frame_mm[0],view[3]/frame_mm[1])
            if abs(needed-units_per_mm) <= units_per_mm*1e-7:
                break
            units_per_mm=needed
        else:
            raise ValueError('指定枠に区画図の文字が収まりません。--frame を大きくしてください')
        root=(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{" ".join(f"{n:.6f}" for n in view)}" '
              f'width="{frame_mm[0]}mm" height="{frame_mm[1]}mm" preserveAspectRatio="xMidYMid meet" '
              f'data-rotation="{degrees}" data-frame-mm="{frame_mm[0]} {frame_mm[1]}" '
              'font-family="Hiragino Kaku Gothic ProN, sans-serif" fill="#222">')
        return '\n'.join([root,*svg,'</svg>']),needed

    options=[candidate(n) for n in ([0,90] if str(rotation)=='auto' else [int(rotation)])]
    return min(options,key=lambda option:option[1])[0]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--rotate', choices=['auto','0','90'], default='auto', help='時計回りの回転。既定は枠に合わせて自動選択')
    parser.add_argument('--frame', default='60x45', help='表示枠の幅x高さ（mm）。既定60x45')
    args=parser.parse_args()
    try:
        data=json.loads(args.data.read_text(encoding='utf-8'))
        if not data.get('site_plan'): raise ValueError('site_plan がありません')
        for issue in plan_issues(data): print(f'{issue["level"]}: {issue["message"]}',file=sys.stderr)
        svg=render_siteplan(data, args.rotate, tuple(map(float,args.frame.lower().split('x'))))
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(svg,encoding='utf-8')
        print('出力: '+str(args.out.resolve()))
        return 0
    except (OSError,ValueError,KeyError) as exc:
        print('区画図作成に失敗しました: '+str(exc),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
