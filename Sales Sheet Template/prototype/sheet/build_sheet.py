#!/usr/bin/env python3
"""Build a standalone B4 landscape sales sheet from one property JSON."""
import sys
sys.dont_write_bytecode = True
import argparse
import base64
import html
import json
import mimetypes
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
import subprocess
import tempfile
import time
from decimal import Decimal
from checks import COMMON, BUILDING, LAND, candidate_report, check, empty, number, prose_text, tsubo, minutes

ROOT = Path(__file__).resolve().parent
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

def esc(value):
    return html.escape(str(value), quote=True)

def readable_annotations(payload, width_mm, height_mm):
    """Keep annotation text at >=7pt in the specified object-fit image cell.

    Only the embedded copy is changed. Reflow small comments into independent
    columns below the drawing, extending the viewBox to include every line.
    Drawing/car geometry remains untouched; comment leader ends follow the text.
    """
    root=ET.fromstring(payload)
    view=[float(v) for v in root.get('viewBox', '').replace(',', ' ').split()]
    if len(view)!=4 or view[2]<=0 or view[3]<=0:
        return payload
    layer=next((e for e in root.iter() if e.get('id')=='annotations'),None)
    if layer is None:
        return payload
    ns='{http://www.w3.org/2000/svg}'
    groups=[g for g in layer.iter(ns+'g') if g.findall(ns+'text')]
    if not groups:
        return payload
    texts=[t for g in groups for t in g.findall(ns+'text')]
    def font(t):
        return float(t.get('font-size', '44'))
    width_px=width_mm*96/25.4
    height_px=height_mm*96/25.4
    original_scale=min(width_px/view[2], height_px/view[3])
    minimum_px=7*96/72
    if min(font(t) for t in texts)*original_scale>=minimum_px:
        return payload
    # The source annotation layer has one text element per comment line.
    # Reserve printed-space line height first, then solve the drawing scale.
    top=min(float(t.get('y','0'))-font(t) for t in texts)
    drawing_height=max(1, top-view[1])
    rows=max(len(g.findall(ns+'text')) for g in groups)
    reserve_px=minimum_px*(rows*1.25+0.75)
    scale=min(width_px/view[2], max(1,height_px-reserve_px)/drawing_height)
    size=minimum_px/scale
    bottom=top+reserve_px/scale
    root.set('viewBox', ' '.join(map(str,[view[0],view[1],view[2],bottom-view[1]])))
    root.set('height',str(bottom-view[1]))
    for index,g in enumerate(groups):
        center=view[0]+view[2]*(index+0.5)/len(groups)
        for line_index,t in enumerate(g.findall(ns+'text')):
            t.set('x',str(center))
            t.set('y',str(top+size*(1+line_index*1.25)))
            t.set('font-size',str(size))
            t.set('text-anchor','middle')
            # Inline styles must not override the corrected presentation attribute.
            style=re.sub(r'(?:^|;)\s*font-size\s*:[^;]*', '', t.get('style',''))
            t.set('style',style+';font-size:'+str(size)+'px')
        for line in g.findall(ns+'line'):
            line.set('x2',str(center))
            line.set('y2',str(top))
    root.set('data-annotation-min-pt','7')
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    return ET.tostring(root,encoding='utf-8')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--stage',choices=['rough','final'],required=True)
    parser.add_argument('--no-pdf',action='store_true')
    parser.add_argument('--cover',action='store_true')
    parser.add_argument('--allow-missing-images',action='store_true',help='final でも画像の「未設定」枠を許す（画像を持たない架空データの検証用）')
    args=parser.parse_args()
    d=json.loads(args.data.read_text(encoding='utf-8'))
    company=json.loads((ROOT/'data/company.json').read_text(encoding='utf-8'))
    args.out.mkdir(parents=True,exist_ok=True)
    # Never leave a previous successful PDF or HTML behind after a rejected revision.
    for filename in ['naka.pdf','naka.html','cover.pdf','cover.html','siteplan.svg']:
        (args.out/filename).unlink(missing_ok=True)
    items=check(d, stage=args.stage)
    def issue(id,path,message,question,level='error'):
        items.append(dict(id=id,path=path,level=level,message=message,question=question))
    def missing(label): return '' if args.stage=='final' else '<span class="missing">要入力：'+esc(label)+'</span>'  # final では空欄を印字しない
    def value(v,label,path=''):
        bad=any(x['path']==path and x['level']=='error' for x in items) if path else False
        if empty(v): return missing(label)
        s=esc('・'.join(map(str,v)) if isinstance(v,list) else v)
        return '<span class="missing">'+s+'／要入力：'+esc(label)+'</span>' if bad else s
    def field(o,k,label,p): return value(o.get(k),label,p+'.'+k)
    def amount(v): return f'{number(v):,f}'.rstrip('0').rstrip('.') if number(v) is not None and '.' in f'{number(v):,f}' else (f'{number(v):,f}' if number(v) is not None else missing('価格'))
    def area(v): return esc(v)+'㎡('+tsubo(v)+'坪)' if number(v) is not None and number(v)>0 else missing('面積')
    def walk(v): return f'徒歩{minutes(v)}分(約{esc(v)}m)' if number(v) is not None and number(v)>0 else missing('距離未入力')
    def asset(path,label,p,frame_mm=None):
        if not path: return '<div class="placeholder">'+esc(label)+' 未設定</div>'
        f=Path(path).expanduser()
        if not f.is_absolute(): f=args.data.resolve().parent/f
        if not f.is_file():
            issue('ASSET',p,'画像ファイルがありません：'+str(f),label+'の画像パスを確認していただけますか？')
            return missing(label+'の画像')
        mime='image/svg+xml' if f.suffix.lower()=='.svg' else mimetypes.guess_type(f.name)[0]
        if mime not in {'image/svg+xml','image/png','image/jpeg','image/webp','image/gif'}:
            issue('ASSET',p,'未対応の画像形式です。','PNG・JPEG・SVG等の画像に変更していただけますか？')
            return missing(label)
        payload=f.read_bytes()
        if mime=='image/svg+xml' and frame_mm:
            payload=readable_annotations(payload,*frame_mm)
        uri='data:'+mime+';base64,'+base64.b64encode(payload).decode('ascii')
        return f'<img src="{uri}" alt="{esc(label)}">'
    buildings=d.get('buildings',[]); lands=d.get('lands',[])
    active_b=[(i,b) for i,b in enumerate(buildings) if b.get('status')!='済']
    active_l=[(i,b) for i,b in enumerate(lands) if b.get('status')!='済']
    total=f'新築分譲住宅／全{len(buildings)}棟'
    if lands: total+=f'・建築条件付売地／全{len(lands)}区画'
    sale=f'今回販売 {sum(b.get("status")=="販売中" for b in buildings)}棟'
    if lands: sale+=f'・{sum(b.get("status")=="販売中" for b in lands)}区画'
    area_units=sum(1 if unicodedata.east_asian_width(ch) in 'WF' else 0.6 for ch in str(d.get('area','')))
    area_font=max(12,min(27,64*72/25.4/max(1,area_units)))
    facility_count=max(1,len(d.get('facilities',[])))
    life_font=max(6,6.5-max(0,facility_count-11)*0.1)
    life_height=max(32,min(92,6+facility_count*6))
    sidebar=f'<div class="brand" style="--area-font:{area_font:.2f}pt">'+'<div class="series">'+esc(d.get('series','Lancasa'))+'</div><small>〜ランカーザ〜</small><div>'+value(d.get('city'),'市名')+'</div><h1>'+value(d.get('area'),'エリア名')+'</h1><small>'+esc(d.get('roman',''))+'</small></div><p class="counts">'+total+'<br>'+sale+'</p><section class="access"><h2>Access</h2><div class="stations">'
    for i,a in enumerate(d.get('access',[])):
        sidebar+='<div class="station"><small>'+field(a,'line','路線',f'access.{i}')+'</small><div>「<b>'+field(a,'station','駅名',f'access.{i}')+'</b>」駅</div><strong>'+walk(a.get('distance_m'))+'</strong></div>'
    sidebar+='</div></section><div class="map">'+asset(d.get('map_path'),'地図','map_path')+('<span class="map-credit">出典：国土地理院（地理院タイル）</span>' if not empty(d.get('map_path')) and d.get('map_source','gsi')=='gsi' else '')+'</div><div class="navigation">カーナビ／'+value(d.get('navigation'),'カーナビ住所')+'</div><section class="life-section"><h3>Life Information</h3><ul class="life">'
    for a in d.get('facilities',[]): sidebar+='<li><span>'+esc(a.get('name',''))+'</span><i></i><span>'+walk(a.get('distance_m'))+'</span></li>'
    sidebar+='</ul></section>'
    catch=d.get('catch') or []
    if isinstance(catch,(str,dict)): catch=[catch]
    highlighted=[]
    words=[w for w in d.get('highlight',[]) if w]
    for line in catch:
        line=prose_text(line)
        parts=re.split('('+'|'.join(re.escape(w) for w in sorted(words,key=len,reverse=True))+')',line) if words else [line]
        highlighted.append(''.join('<em>'+esc(p)+'</em>' if p in words else esc(p) for p in parts))
    title='<div class="catch '+('missing' if any(x['path']=='catch' and x['level']=='error' for x in items) else '')+'">'+'<br>'.join(highlighted)+'</div><div class="subcopy">'+value(prose_text(d.get('subcopy','')),'サブコピー','subcopy')+'</div>'
    hero=d.get('hero') or {}
    hero_html=asset(hero.get('path'),'外観パース','hero.path')+'<figcaption>'+value(hero.get('caption'),'画像の種類','hero.caption')+'</figcaption>' if hero else asset(None,'外観パース','hero.path')
    division_path=d.get('division_path')
    if not division_path and d.get('site_plan'):
        from make_siteplan import render_siteplan
        try:
            generated_plan=args.out/'siteplan.svg'
            generated_plan.write_text(render_siteplan(d, frame_mm=(57.4, 44.4)),encoding='utf-8')
            division_path=str(generated_plan.resolve())
        except ValueError as exc:
            if not any(x['id']=='SITE_PLAN' and x['level']=='error' for x in items):
                issue('SITE_PLAN_LAYOUT','site_plan',str(exc),'区画図の座標・文字量と表示枠を確認してください。')
    division='<div class="division"><h3>Division'+(' 建築条件付売地' if lands else '')+'</h3>'+asset(division_path,'区画図','division_path')+'</div>'
    def plans(obj,p,frame_mm=(80,39),selection=None):
        fs=list(enumerate(obj.get('floorplans',[])))
        if selection is not None:
            fs=fs[selection]
        if not fs: return '<div class="floorplans">'+asset(None,'間取り図',p)+'</div>'
        # Figures stack in this cell, with 2mm gaps and a 3mm caption each.
        frame=(frame_mm[0],(frame_mm[1]+3-2*(len(fs)-1))/len(fs)-3)
        return '<div class="floorplans">'+''.join('<figure>'+asset(f.get('path'),f.get('label','間取り図'),p+f'.floorplans.{j}.path',frame)+'<figcaption>'+esc(f.get('label',''))+'</figcaption></figure>' for j,f in fs)+'</div>'
    mixed=bool(lands or len(active_b)>1)
    cards=''
    for i,b in active_b:
        p=f'buildings.{i}'
        cards+='<article class="building"><div class="price-panel"><h2>◀ Room Plan ▶</h2><div class="unit-name">'+field(b,'name','号棟',p)+'</div><div class="price"><small>販売価格</small><b>'+amount(b.get('price'))+'</b><span>〈税込〉<br>万円</span></div><strong class="layout">'+field(b,'layout','間取り',p)+'</strong><p>土地面積／'+area(b.get('land_area'))+'<br>建物面積／'+area(b.get('building_area'))
        if b.get('has_garage'): cards+='<br>'+value(f'※車庫部分{b.get("garage_area", "未入力")}㎡含む' if b.get('garage_included') else None,'車庫面積・算入',p+'.garage_area')
        cards+='<br>建築確認番号／'+field(b,'confirmation','建築確認番号',p)+'</p><div class="features">'+''.join('<span class="feature-badge">'+value(prose_text(t),'特長ラベル',p+'.features')+'</span>' for t in (b.get('features') or [])[:3])+'</div>'
        cards+='</div><div class="plan-one">'+plans(b,p,(75,45) if mixed else (80,39),None if mixed else slice(0,1))+'</div>'
        if not mixed:
            cards+='<div class="plan-rest">'+plans(b,p,(131,109),slice(1,None))+'</div>'
        cards+='<div class="plan-extras">'+''.join('<span class="feature-badge">'+value(prose_text(t),'特長ラベル',p+'.features')+'</span>' for t in (b.get('features') or [])[3:])
        if b.get('comments'):
            cards+='<div class="comments">'+''.join('<span>'+value(prose_text(t),'コメント',p+'.comments')+'</span>' for t in b['comments'])+'</div>'
        cards+='</div></article>'
    if active_l:
        cards+='<section class="land-cards">'
        for i,b in active_l:
            p=f'lands.{i}'; ref=b.get('reference_plan')
            cards+='<article><h3>'+field(b,'name','区画番号',p)+' 建築条件付売地</h3><p>土地価格 <b>'+amount(b.get('price'))+'万円</b>（非課税）<br>土地面積／'+area(b.get('land_area'))+'</p>'
            if ref is not None:
                rp=p+'.reference_plan'
                cards+='<h3>'+esc(b.get('name',''))+' Reference Plan</h3><p>参考建物価格 <b>'+amount(ref.get('price'))+'万円〈税込〉</b><br>建物面積／'+area(ref.get('building_area'))+'<br>土地＋参考建物価格 <b>'
                total_price=number(b.get('price'))+number(ref.get('price')) if number(b.get('price')) is not None and number(ref.get('price')) is not None else None
                cards+=amount(total_price)+'万円</b><br>（土地非課税・建物税込）</p>'+plans(ref,rp)
            cards+='</article>'
        cards+='</section>'
    c=d.get('common',{})
    overview='<h3>物件概要〈'+esc(d.get('city','')+d.get('area',''))+'・新築'+str(len(buildings))+'棟'+('・建築条件付売地'+str(len(lands))+'区画' if lands else '')+'〉</h3>'
    if lands: overview+='<b>《共通概要》</b>'
    overview+='<p>'
    for id,k,label in COMMON:
        v=c.get(k)
        if k in {'coverage','far'} and not empty(v): v=str(v)+'%'
        overview+=' <span>●'+label+'／'+value(v,label,'common.'+k)+'</span>'
    overview+=' ●交通／'+'・'.join(esc(a.get('line','')+'「'+a.get('station','')+'」駅')+walk(a.get('distance_m')) for a in d.get('access',[]))
    overview+=' ●接道／'+'・'.join(esc(r.get('direction',''))+' 約'+value(r.get('width_m'),'道路幅員',f'common.roads.{i}.width_m')+'m '+esc(r.get('type','')) for i,r in enumerate(c.get('roads',[])))
    overview+=' ●総棟数・区画数／'+total+' ●販売数／'+sale+' ●価格／別記'
    if c.get('rights')=='借地権':
        for k,label in [('lease_type','借地権の種類'),('lease_term','借地期間'),('ground_rent','地代')]: overview+=' ●'+label+'／'+field(c,k,label,'common')
    overview+='</p>'
    def group(rows,fields,kind,title):
        out='<b>'+title+'</b><p>'
        for id,key,label in fields:
            vals=[b.get(key) for i,b in rows]
            if not vals: continue
            if key in {'price','land_area','building_area','confirmation','layout','name'}: result='別記'
            elif any(v!=vals[0] for v in vals):
                result='別記（'+'・'.join(esc(b.get('name') or '号棟未入力')+'：'+field(b,key,label,f'{kind}.{i}') for i,b in rows)+'）'
            else: result=field(rows[0][1],key,label,f'{kind}.{rows[0][0]}')
            out+=' ●'+label+'／'+result
        return out+'</p>'
    if active_l: overview+=group(active_l,LAND,'lands','《建築条件付売地概要》')
    if active_b: overview+=group(active_b,BUILDING+[('B08','completion_status','完成状態')],'buildings','《新築分譲住宅》' if lands else '')
    notes=[]
    if buildings: notes.append(('N1','電波障害地域の有無にかかわらず、TVアンテナ・ケーブルテレビ等を利用する場合の費用は買主様のご負担となります。'))
    if d.get('utility_poles_possible'): notes.append(('N2','電気の供給のため、敷地内に電柱及び支線が入る場合があります。'))
    notes += [('N3','当社指定の司法書士・家屋調査士を利用させて頂きます。'),('N4','図面・設備等に関しては、変更する場合があります。ご了承下さい。'),('N5','図面と現況が異なる場合は現況を優先と致します。')]
    for i,b in active_l:
        p=f'lands.{i}'
        months=field(b,'contract_months','請負契約期限',p); contractor=field(b,'contractor','請負先',p)
        notes.append(('N6',esc(b.get('name',''))+'〈建築条件付売地とは〉土地売買契約後'+months+'か月以内に、'+contractor+'と住宅建築請負契約を結んで頂くことを停止条件として販売します。土地契約後、直ちに建築設計の協議をして頂きますが、'+months+'か月以内に住宅の建築請負契約が成立しない場合は、売買はなかったことになり、申込金その他お預かりした金銭は全額無条件で速やかに返還します。'))
        ref=b.get('reference_plan')
        if ref is not None:
            fees='・'.join(esc(f.get('label',''))+' '+amount(f.get('amount'))+'万円' for f in ref.get('extra_costs',[])) or missing('追加費用')
            free='参考プランは一例です。採用するかどうかは自由にお決め頂けます。' if ref.get('adoption_optional') is True else missing('参考プランの自由採用確認')
            notes.append(('N7',esc(b.get('name',''))+free+'参考建物価格のほかに'+fees+'が必要です。'))
    overview+='<div class="notes">'+''.join('<span data-note="'+id+'">※'+text+'</span>' for id,text in notes)+'</div>'
    display_equipment=list(dict.fromkeys(d.get('equipment',[])+[e for _, b in active_b for e in b.get('equipment',[])]))
    icon_rules=json.loads((ROOT/'templates/icons/icons.json').read_text(encoding='utf-8'))
    def equipment_badge(label):
        rule=next((rule for rule in icon_rules if any(word.casefold() in label.casefold() for word in rule['keywords'])),None)
        if rule is None:
            return '<span class="text-badge">'+esc(label)+'</span>'
        svg=(ROOT/'templates/icons'/rule['file']).read_text(encoding='utf-8')
        return '<div class="equipment-icon">'+svg+'<span>'+esc(label)+'</span></div>'
    equipment='<div class="standard">標準設備・仕様<br><strong>安心な快適な<br>住まいの創造</strong></div><div class="warranties">'+''.join('<div>'+esc(w)+'</div>' for w in d.get('warranties',[]))+'</div><div class="badges">'+''.join(equipment_badge(e) for e in display_equipment)+'</div>'
    footer='<div class="licenses">'+esc(' ／ '.join(company['licenses']))+'</div><div class="company-row"><div class="company-name"><small>LAND STYLE</small><b>'+esc(company['name'])+'</b><small>'+esc(company['address'])+'</small></div><div class="web"><b>インターネットでラクラク検索！<br>最新情報を常時公開中！</b><strong>'+esc(company['url'])+'</strong></div><div class="contact"><strong>TEL '+esc(company['tel'])+'</strong><small>受付時間 '+esc(company['hours'])+'（定休日 '+esc(company['closed'])+'）</small></div><div class="fax">FAX '+esc(company['fax'])+'<b>営業社員 募集中！</b><small>'+esc(company['email'])+'</small></div><div class="transaction">取引態様<br><b>'+esc(d.get('transaction',company['transaction']))+'</b><small>手数料'+esc(d.get('commission',company['commission']))+'</small></div></div>'
    cover_html=None
    if args.cover:
        import cv2
        import numpy as np
        cover=d.get('cover') or {}
        photos=''
        for i in range(3):
            photo=(cover.get('photos') or [])[i] if i<len(cover.get('photos') or []) else None
            if photo and not empty(photo.get('path')):  # パスが空の枠は「写真なし」と同じ扱い
                photos+=f'<figure class="photo photo-{i+1}">'+asset(photo.get('path'),'施工例の写真',f'cover.photos[{i}].path')+'<figcaption>'+esc(photo.get('caption',''))+'</figcaption></figure>'
            elif args.stage=='rough':
                photos+=f'<figure class="photo photo-{i+1}"><div class="placeholder">施工例の写真 未設定</div></figure>'
        qrs=''
        for i, qr in enumerate(cover.get('qr',company['cover_qr'])):
            try:
                raw=cv2.QRCodeEncoder_create().encode(qr['url'])
                # Encoder versions can include their own quiet border. Trim to symbol,
                # then add exactly four white modules and upscale without smoothing.
                yy,xx=np.where(raw<128)
                raw=raw[yy.min():yy.max()+1,xx.min():xx.max()+1]
                raw=cv2.copyMakeBorder(raw,4,4,4,4,cv2.BORDER_CONSTANT,value=255)
                raw=cv2.resize(raw,None,fx=8,fy=8,interpolation=cv2.INTER_NEAREST)
                ok,encoded=cv2.imencode('.png',raw)
                if not ok: raise ValueError('PNG変換失敗')
                uri='data:image/png;base64,'+base64.b64encode(encoded.tobytes()).decode('ascii')
                qrs+='\n<figure class="qr"><figcaption>▼'+esc(qr['label'])+'</figcaption><div><img src="'+uri+'" alt="'+esc(qr['label'])+'"></div></figure>'
            except (cv2.error,ValueError,KeyError,TypeError) as exc:
                issue('COVER_QR',f'cover.qr[{i}]','QRを生成できません：'+str(exc),'QRのURLと見出しを確認してください。')
        access_map=''
        if company.get('access_map_path'):
            company_map=Path(company['access_map_path']).expanduser()
            if not company_map.is_absolute(): company_map=ROOT/'data'/company_map
            access_map='<figure class="company-map">'+asset(str(company_map),'会社への案内図','company.access_map_path')+'<figcaption>'+esc(company.get('access_map_caption',''))+'</figcaption></figure>'
        logo=asset(str(ROOT/'data/assets/site-logo.svg'),'LAND STYLE','company.logo')
        cover_html=(ROOT/'templates/cover.html.j2').read_text(encoding='utf-8')
        cover_values={'css':(ROOT/'templates/cover.css').read_text(encoding='utf-8'),
                      'logo':logo,'photos':photos,'hero':hero_html,'qrs':qrs,'access_map':access_map,
                      'footer':footer,'series':esc(d.get('series','Lancasa')),'city':esc(d.get('city','')),
                      'area':esc(d.get('area','')),'roman':esc(d.get('roman','')),'total':esc(total)}
        for key,v in cover_values.items(): cover_html=cover_html.replace('{{ '+key+' }}',v)
    def report(pdf_status):
        candidates=candidate_report(d)
        result=dict(stage=args.stage,pdf_status=pdf_status,items=items,counts={lv:sum(x['level']==lv for x in items) for lv in ['error','warning']},candidates=[{k:v for k,v in x.items() if k!='items'} for x in candidates])
        (args.out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        md=f'# 点検レポート\n\n段階: {args.stage} / PDF: {pdf_status}\n\nerror: {result["counts"]["error"]}, warning: {result["counts"]["warning"]}\n\n'
        md+='\n'.join(f'- [{x["level"]}] {x["id"]} ({x["path"]}) {x["message"]}\n  逆質問: {x["question"]}' for x in items)
        md+='\n\n## 文章の候補\n\n'
        md+='| 番号 | 種類 | 本文 | 根拠 | 禁止語 | 数値 | 文字数 |\n|---:|---|---|---|---|---|---|\n'
        for x in candidates:
            clean=lambda v: str(v).replace('|','\\|').replace('\n','<br>')
            md+=f'| {x["number"]} | {clean(x["label"])} | {clean(x["text"])} | {clean("、".join(x["basis"]) or "未指定")} | {"OK" if x["checks"]["banned"] else "NG"} | {"OK" if x["checks"]["numbers"] else "NG"} | {"OK" if x["checks"]["length"] else "NG"} |\n'
        (args.out/'report.md').write_text(md+'\n',encoding='utf-8')
    if args.stage=='final' and any(x['level']=='error' for x in items):
        report('blocked'); print('点検 error のため出力停止。report.json / report.md: '+str(args.out.resolve())); return 2
    review=''
    if args.stage=='rough' and any(x['level']=='error' for x in items):
        review='<div class="review">ラフ・要確認：'+' ／ '.join(esc(x['id']+' '+x['message']) for x in items if x['level']=='error')+'</div>'
    template=(ROOT/'templates/naka.html.j2').read_text(encoding='utf-8')
    replacements={'css':(ROOT/'templates/naka.css').read_text(encoding='utf-8'),'sidebar':sidebar,'sidebar_style':f'--life-height:{life_height}mm;--life-count:{facility_count};--life-font:{life_font}pt','title':title,'hero':hero_html,'division':division,'cards':cards,'overview':overview,'equipment':equipment,'footer':footer,'review':review,'mode':'mixed' if lands or len(active_b)>1 else 'single'}
    for key,v in replacements.items(): template=template.replace('{{ '+key+' }}',v)
    (args.out/'naka.html').write_text(template,encoding='utf-8')
    if args.stage=='final' and '要入力' in template:  # 安全装置: 仕上げに要入力の文字を残さない
        items.append(dict(id='FINAL_PLACEHOLDER',path='naka.html',level='error',message='仕上げの紙面に「要入力」が残っています。',question='未入力の項目を埋めていただけますか？'))
        report('blocked'); print('仕上げの紙面に「要入力」が残ったため出力停止。'); return 2
    if cover_html is not None:
        (args.out/'cover.html').write_text(cover_html,encoding='utf-8')
    if args.stage=='final' and not args.allow_missing_images:  # 安全装置: 仕上げに「未設定」の枠（地図・パース・間取り図・区画図など）を残さない
        left=[name for name,html in (('naka.html',template),('cover.html',cover_html or '')) if '未設定' in html]
        if left:
            items.append(dict(id='FINAL_IMAGE_MISSING',path='、'.join(left),level='error',message='仕上げの紙面に画像の「未設定」の枠が残っています。',question='地図・外観パース・間取り図・区画図のうち、まだ用意できていない画像を教えていただけますか？'))
            report('blocked'); print('仕上げの紙面に「未設定」の枠が残ったため出力停止。'); return 2
    if args.no_pdf:
        report('skipped'); print('HTML出力: '+str((args.out/'naka.html').resolve())); return 0
    for page in (['naka','cover'] if args.cover else ['naka']):
        with tempfile.TemporaryDirectory(prefix='sheet-chrome-') as profile:
            pdf_path=(args.out/(page+'.pdf')).resolve()
            command=[CHROME,'--headless=new','--disable-gpu','--no-first-run','--user-data-dir='+profile,'--no-pdf-header-footer','--print-to-pdf='+str(pdf_path),(args.out/(page+'.html')).resolve().as_uri()]
            try:
                pdf_path.unlink(missing_ok=True)
                deadline=time.monotonic()+60
                # A file-backed stderr avoids blocking on Chrome's repeated logs.
                with tempfile.TemporaryFile(mode='w+b') as chrome_log:
                    proc=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=chrome_log)
                    try:
                        last_size=0
                        stable_since=None
                        while True:
                            now=time.monotonic()
                            if now>=deadline:
                                raise subprocess.TimeoutExpired(command,60)
                            size=pdf_path.stat().st_size if pdf_path.is_file() else 0
                            if size>0:
                                if size!=last_size or stable_since is None:
                                    stable_since=now
                                elif now-stable_since>=0.5:
                                    break
                            else:
                                stable_since=None
                            last_size=size
                            if proc.poll() is not None and size==0:
                                chrome_log.seek(0,2)
                                chrome_log.seek(max(0,chrome_log.tell()-1500))
                                detail=chrome_log.read().decode('utf-8',errors='replace')
                                raise RuntimeError(f'Chrome exit={proc.returncode}: {detail}')
                            time.sleep(min(0.1,max(0,deadline-time.monotonic())))
                    finally:
                        # Stop Chrome before TemporaryDirectory removes its profile.
                        if proc.poll() is None:
                            proc.terminate()
                            try:
                                proc.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.wait()
            except (OSError,RuntimeError,subprocess.TimeoutExpired) as exc:
                pdf_path.unlink(missing_ok=True)
                report('unavailable'); print('HTML完成。PDF生成不可: '+str(exc),file=sys.stderr); return 1
    report('created'); print('出力: '+str(args.out.resolve())); return 0

if __name__=='__main__':
    raise SystemExit(main())
