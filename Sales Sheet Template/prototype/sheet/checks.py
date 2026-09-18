"""Checks against the local 02/03/04 specification (not a legal certification)."""
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_CEILING
import json
import re
from pathlib import Path

COMMON = [
 ('P01','address','所在地（住居表示）'), ('P02','lot_address','所在地（地番）'),
 ('P04','zoning','用途地域'), ('P05','coverage','建ぺい率'), ('P06','far','容積率'),
 ('P07','height_zone','高度地区'), ('P08','fire_zone','防火地域'), ('P09','land_category','地目'),
 ('P10','planning','都市計画'), ('P11','rights','土地権利'), ('P12','utilities','設備'),
 ('P14','private_road','私道負担'), ('P15','laws','その他の法令制限'), ('P19','valid_until','取引条件の有効期限')]
BUILDING = [('B01','name','号棟'),('B02','price','販売価格'),('B03','layout','間取り'),
 ('B04','land_area','土地面積'),('B05','building_area','建物面積'),('B06','structure','構造・階数'),
 ('B07','confirmation','建築確認番号'),('B08','completion','完成時期'),('B09','handover_month','引渡し可能年月')]
LAND = [('L01','name','区画番号'),('L02','price','土地価格'),('L03','land_area','土地面積'),
 ('L04','condition','現況'),('L05','handover','引渡し'),('L06','contract_months','請負契約期限（月数）'),('L06','contractor','請負先')]
BANNED = re.compile('完全|完璧|絶対|万全|日本一|業界一|超|当社だけ|最高級?|極|特級|格安|激安|バーゲンセール|特選|厳選|完売|掘出物|掘り出し物|二度とない|他に類を見ない')
LAWS = {'なし','景観法','航空法','宅地造成及び特定盛土等規制法','農地法','文化財保護法','都市計画法','建築基準法','国土利用計画法','土地区画整理法','都市再生特別措置法','森林法','自然公園法','河川法','砂防法','所沢市立地適正化計画'}

def empty(v):
    return v is None or v == '' or v == [] or (isinstance(v,str) and not v.strip())

def number(v):
    try:
        d = Decimal(str(v))
        return d if d.is_finite() and not isinstance(v,bool) else None
    except (InvalidOperation, ValueError):
        return None

def tsubo(v):
    return f'{(Decimal(str(v))*Decimal("0.3025")).quantize(Decimal("0.01"), rounding=ROUND_DOWN):.2f}'

def minutes(v):
    return int((Decimal(str(v))/80).to_integral_value(rounding=ROUND_CEILING))

def prose_text(value):
    """Return the printable part of a legacy string or a {text, basis} value."""
    return value.get('text', '') if isinstance(value, dict) else str(value or '')

def _json_type(value):
    if value is None: return 'null'
    if isinstance(value, bool): return 'boolean'
    if isinstance(value, int): return 'integer'
    if isinstance(value, float): return 'number'
    if isinstance(value, str): return 'string'
    if isinstance(value, list): return 'array'
    if isinstance(value, dict): return 'object'

def _schema_items(data):
    schema=json.loads((Path(__file__).resolve().parent/'property.schema.json').read_text(encoding='utf-8'))
    found=[]
    def walk(value, rule, path):
        expected=rule.get('type')
        allowed=expected if isinstance(expected,list) else [expected] if expected else []
        actual=_json_type(value)
        if allowed and actual not in allowed and not (actual=='integer' and 'number' in allowed):
            found.append(dict(level='error',id='SCHEMA_TYPE',path=path,message=f'型違い：{path} は {"/".join(allowed)} で入力してください。',question=f'{path} の型を確認していただけますか？'))
            return
        if value is None:
            return
        if 'enum' in rule and value not in rule['enum']:
            found.append(dict(level='error',id='SCHEMA_ENUM',path=path,message=f'選択肢にない値です：{value}',question=f'{path} の値を確認していただけますか？'))
        if isinstance(value,dict):
            props=rule.get('properties',{})
            for key in rule.get('required',[]):
                if key not in value:
                    found.append(dict(level='error',id='SCHEMA_REQUIRED',path=(path+'.'+key).lstrip('.'),message=f'必須キー {key} がありません。',question=f'{key} を入力していただけますか？'))
            for key,child in value.items():
                child_path=(path+'.'+key).lstrip('.')
                if key not in props:
                    found.append(dict(level='warning',id='SCHEMA_UNKNOWN',path=child_path,message=f'スキーマにないキー {key} です。',question=f'{key} は綴り間違いではありませんか？'))
                else: walk(child,props[key],child_path)
        elif isinstance(value,list) and isinstance(rule.get('items'),dict):
            for i,child in enumerate(value): walk(child,rule['items'],f'{path}[{i}]')
    walk(data,schema,'')
    return found

def _resolve(data,path):
    current=data
    for name,index in re.findall(r'(?:^|\.)([^.\[]+)|\[(\d+)\]',path):
        try: current=current[int(index)] if index else current[name]
        except (KeyError,IndexError,TypeError,ValueError): return None
    return current

def _number_matches(value, unit, target):
    wanted=number(value)
    def scan(v,key=''):
        if isinstance(v,dict):
            for k,x in v.items():
                if unit=='分' and k=='distance_m' and number(x) is not None and number(minutes(x))==wanted: return True
                if scan(x,k): return True
        elif isinstance(v,list):
            return any(scan(x,key) for x in v)
        elif isinstance(v,str):
            pattern=rf'(?<!\d){re.escape(str(value))}(?:\.0)?\s*{re.escape(unit)}'
            return bool(re.search(pattern,v,re.I))
        elif number(v) is not None and number(v)==wanted:
            return unit!='分'
        return False
    return scan(target)

def _prose_checks(data,value,kind,path,require_basis=True):
    text=prose_text(value); basis=value.get('basis') if isinstance(value,dict) else None
    checks=[]
    def add(level,id,message): checks.append(dict(level=level,id=id,path=path,message=message,question='文章と根拠を確認していただけますか？'))
    if empty(text):
        return [], {'banned':True,'numbers':True,'length':True}
    if require_basis and not basis: add('warning','COPY_BASIS','根拠の項目が未指定です。')
    targets=[]
    for bp in basis or []:
        target=_resolve(data,bp)
        if empty(target): add('warning','COPY_BASIS',f'根拠の項目が見つからない：{bp}'); continue
        targets.append(target)
    for word in sorted(set(BANNED.findall(text))): add('error','S3/S8',f'禁止語「{word}」が含まれます。')
    scope=targets or [data]
    for val,unit in re.findall(r'(\d+(?:\.\d+)?)\s*(バルコニー|㎡|帖|J|分|m)(?![A-Za-z])',text,re.I):
        normalized='帖' if unit.upper()=='J' else unit
        if not any(_number_matches(val,normalized,t) for t in scope):
            add('error','COPY_NUMBER',f'文章の数値が物件データと合わない：{val}{unit}')
    lengths=[]
    if kind=='catch':
        lines=text.split('\n'); lengths += [('2行以内',len(lines)<=2),('各行28字以内',all(len(x)<=28 for x in lines))]
    elif kind=='subcopy': lengths.append(('8〜16字',8<=len(text)<=16))
    elif kind=='features': lengths.append(('4〜15字',4<=len(text)<=15))
    elif kind=='comments':
        lines=text.split('\n'); lengths += [('2行以内',len(lines)<=2),('各行16字以内',all(len(x)<=16 for x in lines))]
    for label,ok in lengths:
        if not ok: add('warning','COPY_LENGTH',f'文字数が基準外です（{label}）。')
    return checks, {'banned':not any(x['id']=='S3/S8' for x in checks),'numbers':not any(x['id']=='COPY_NUMBER' for x in checks),'length':not any(x['id']=='COPY_LENGTH' for x in checks)}

def candidate_report(data):
    result=[]; source=data.get('copy_candidates') or {}
    def append(kind,value,label,meta=None):
        checks,status=_prose_checks(data,value,kind,f'copy_candidates.{kind}',True)
        result.append(dict(number=len(result)+1,kind=kind,label=label,text=prose_text(value),basis=value.get('basis',[]) if isinstance(value,dict) else [],checks=status,items=checks,**(meta or {})))
    for value in source.get('catch',[]):
        lines=value if isinstance(value,list) else [value]
        combined={'text':'\n'.join(prose_text(x) for x in lines),'basis':list(dict.fromkeys(bp for x in lines if isinstance(x,dict) for bp in x.get('basis',[])))}
        append('catch',combined,'キャッチ')
    for kind in ('subcopy','features'):
        for value in source.get(kind,[]): append(kind,value,{'subcopy':'サブコピー','features':'特長ラベル'}[kind])
    for value in source.get('comments',[]):
        if isinstance(value,dict):
            copy={'text':value.get('text',''),'basis':value.get('basis',[])}
            append('comments',copy,'間取りコメント',{k:value.get(k,'') for k in ('building','room','side')})
        else: append('comments',value,'間取りコメント')
    return result

def check(data, stage='rough'):
    items=_schema_items(data)
    def issue(id,path,message,question,level='error'):
        items.append(dict(level=level,id=id,path=path,message=message,question=question))
    def require(obj,key,id,path,label,numeric=False):
        v=obj.get(key)
        if empty(v) or (numeric and (number(v) is None or number(v)<=0)):
            issue(id,path+'.'+key,label+'が未入力または不正です。',label+'を教えていただけますか？')
    c=data.get('common',{})
    for id,key,label in COMMON:
        if id in {'P01','P02','P04','P11','P14','P19'} or (id=='P15' and data.get('lands')):
            require(c,key,id,'common',label)
    if c.get('rights')=='借地権':
        for key,label in [('lease_type','借地権の種類'),('lease_term','借地期間'),('ground_rent','地代')]:
            require(c,key,'P11','common',label)
    if not data.get('access'): issue('P03','access','交通が未入力です。','路線・駅名・道路距離を教えていただけますか？')
    for i,a in enumerate(data.get('access',[])):
        p=f'access.{i}'
        for key,label in [('line','路線'),('station','駅名')]: require(a,key,'P03',p,label)
        if number(a.get('distance_m')) is None or number(a.get('distance_m'))<=0:
            issue('P03',p+'.distance_m','道路距離が未入力または不正です。',f'{a.get("station","対象")}駅までの道路距離（m）を教えていただけますか？ 徒歩分は80m＝1分で計算します。')
    if not c.get('roads'): issue('P13','common.roads','接道が未入力です。','接道の方位・幅員・公道／私道を教えていただけますか？')
    for i,r in enumerate(c.get('roads',[])): require(r,'width_m','P13',f'common.roads.{i}','道路幅員（m）',True)
    for law in c.get('laws',[]):
        if law not in LAWS:
            issue('P15','common.laws',f'正式名の一覧にない法令名：{law}', '文化財保護法ではありませんか？' if law=='埋蔵文化保護法' else f'「{law}」の正式名称を確認していただけますか？','warning')
    if not data.get('buildings'): issue('P16','buildings','対象の新築棟がありません。','新築の棟リストを教えていただけますか？')
    for kind,fields in [('buildings',BUILDING),('lands',LAND)]:
        for i,b in enumerate(data.get(kind,[])):
            p=f'{kind}.{i}'
            subject=b.get('name') if not empty(b.get('name')) else f'この新築の棟（{i+1}番目）' if kind=='buildings' else f'この土地の区画（{i+1}番目）'
            if b.get('status') not in {'販売中','商談中','済'}:
                issue('B11' if kind=='buildings' else 'L11',p+'.status','販売状況が不正です。','販売中・商談中・済のいずれですか？')
            if b.get('status')=='済': continue
            for id,key,label in fields:
                if key in {'layout','condition','handover'}: continue
                if key=='name' and empty(b.get(key)) and kind=='buildings':
                    issue(id,p+'.name',subject+'の号棟が未入力です。',subject+'は何号棟ですか？（例: 1号棟）')
                else:
                    require(b,key,id,p,f'{subject}の{label}',key in {'price','land_area','building_area','contract_months'})
            if kind=='buildings':
                for key,id in [('completion','B08'),('handover_month','B09')]:
                    if not empty(b.get(key)) and not re.search(r'(?:\d{4}[-/]\d{1,2}|(?:\d{4}|令和\d+|平成\d+)年\d{1,2}月)',str(b[key])):
                        issue(id,p+'.'+key,'年月が確認できません。',f'{subject}の'+('完成時期' if key=='completion' else '引渡し可能年月')+'を具体的な年月で教えていただけますか？')
                if b.get('completion_status') not in {'完成予定','完成済'}: issue('B08',p+'.completion_status','完成状態を1つ選択してください。',f'{subject}は完成予定・完成済のどちらですか？')
                eq=' '.join(b.get('equipment',data.get('equipment',[])))
                for label,pattern in [('台所','キッチン|食器|浄水'),('浴室','浴室|バス'),('便所','便座|トイレ')]:
                    if not re.search(pattern,eq): issue('B10',p+'.equipment',label+'の設備概要がありません。',subject+'の'+label+'の主な設備を教えていただけますか？')
                if b.get('has_garage') and (number(b.get('garage_area')) is None or b.get('garage_included') is not True):
                    issue('B05',p+'.garage_area','車庫面積・算入注記を確認してください。',subject+'の建物面積に車庫を含みますか？ 含む車庫面積を教えてください。')
                if b.get('rooms') and b.get('layout'):
                    rooms=b['rooms']; layout=b['layout']
                    m=re.match(r'(\d+)(?:\((\d+)\))?',layout)
                    n=sum(r.get('type') in {'居室','洋室','和室','主寝室','子供部屋'} for r in rooms)
                    counts={int(x) for x in m.groups() if x is not None} if m else set()
                    missing_ldk='LDK' in layout and not any(r.get('type')=='LDK' for r in rooms)
                    missing_service='+S' in layout and not any(r.get('type') in {'納戸','サービスルーム','S'} for r in rooms)
                    if (m and n not in counts) or missing_ldk or missing_service:
                        issue('B03',p+'.layout','間取りと室名一覧が一致しません。',subject+'の居室数・LDK・納戸等を図面で確認していただけますか？','warning')
            if kind=='lands' and b.get('reference_plan') is not None:
                ref=b['reference_plan']; rp=p+'.reference_plan'
                require(ref,'price','L07',rp,'参考建物価格（税込・万円）',True)
                fees=ref.get('extra_costs')
                valid=bool(fees) and all(f.get('label') and number(f.get('amount')) is not None and number(f['amount'])>=0 for f in fees)
                if not valid: issue('L10',rp+'.extra_costs','追加費用が未入力または不正です。','参考建物価格のほかに必要な費用の内容と金額（万円）を教えていただけますか？')
                if not valid or ref.get('adoption_optional') is not True:
                    issue('N7',rp+'.adoption_optional','自由採用・費用を含む注記を確定できません。','参考プランの採用が自由であることと追加費用を確認していただけますか？')
    def selected(kind, value, path, label):
        values=value if isinstance(value,list) else [value]
        chosen=[v for v in values if not empty(prose_text(v))]
        if not chosen:
            level='error' if stage=='final' and kind in {'catch','features'} else 'warning'
            issue('COPY_UNSELECTED',path,label+'が未選択（文章の候補から選んでください）',
                  label+'を文章の候補から選んでください。',level)
            return
        for i,v in enumerate(values):
            if not empty(prose_text(v)):
                items.extend(_prose_checks(data,v,kind,path+f'[{i}]' if isinstance(value,list) else path)[0])
    selected('catch',data.get('catch'),'catch','キャッチ')
    selected('subcopy',data.get('subcopy'),'subcopy','サブコピー')
    for bi,b in enumerate(data.get('buildings',[])):
        if b.get('status')=='済': continue
        subject=b.get('name') or f'この新築の棟（{bi+1}番目）'
        selected('features',b.get('features'),f'buildings[{bi}].features',subject+'の特長ラベル')
        # Comments may already be embedded in the floorplan; absence is optional.
        for i,v in enumerate(b.get('comments') or []):
            items.extend(_prose_checks(data,v,'comments',f'buildings[{bi}].comments[{i}]')[0])
    for candidate in candidate_report(data): items.extend(candidate['items'])
    hero=data.get('hero') or {}
    if hero.get('path') and not hero.get('caption'): issue('N8','hero.caption','画像キャプションがありません。','画像の種類と必要な撮影日を教えていただけますか？')
    return items
