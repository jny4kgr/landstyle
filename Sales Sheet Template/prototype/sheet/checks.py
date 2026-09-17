"""Checks against the local 02/03/04 specification (not a legal certification)."""
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_CEILING
import re

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

def check(data):
    items=[]
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
            if b.get('status') not in {'販売中','商談中','済'}:
                issue('B11' if kind=='buildings' else 'L11',p+'.status','販売状況が不正です。','販売中・商談中・済のいずれですか？')
            if b.get('status')=='済': continue
            for id,key,label in fields:
                if key in {'layout','condition','handover'}: continue
                require(b,key,id,p,f'{b.get("name") or "対象"}の{label}',key in {'price','land_area','building_area','contract_months'})
            if kind=='buildings':
                for key,id in [('completion','B08'),('handover_month','B09')]:
                    if not empty(b.get(key)) and not re.search(r'(?:\d{4}[-/]\d{1,2}|(?:\d{4}|令和\d+|平成\d+)年\d{1,2}月)',str(b[key])):
                        issue(id,p+'.'+key,'年月が確認できません。','具体的な年月を教えていただけますか？')
                if b.get('completion_status') not in {'完成予定','完成済'}: issue('B08',p+'.completion_status','完成状態を1つ選択してください。','完成予定・完成済のどちらですか？')
                eq=' '.join(b.get('equipment',data.get('equipment',[])))
                for label,pattern in [('台所','キッチン|食器|浄水'),('浴室','浴室|バス'),('便所','便座|トイレ')]:
                    if not re.search(pattern,eq): issue('B10',p+'.equipment',label+'の設備概要がありません。',label+'の主な設備を教えていただけますか？')
                if b.get('has_garage') and (number(b.get('garage_area')) is None or b.get('garage_included') is not True):
                    issue('B05',p+'.garage_area','車庫面積・算入注記を確認してください。','建物面積に車庫を含みますか？ 含む車庫面積を教えてください。')
                if b.get('rooms') and b.get('layout'):
                    rooms=b['rooms']; m=re.match(r'(\d+)',b['layout'])
                    n=sum(r.get('type')=='居室' for r in rooms)
                    if m and (int(m[1])!=n or ('LDK' in b['layout'] and not any(r.get('type')=='LDK' for r in rooms))):
                        issue('B03',p+'.layout','間取りと室名一覧が一致しません。','居室数とLDKを図面で確認していただけますか？','warning')
            if kind=='lands' and b.get('reference_plan') is not None:
                ref=b['reference_plan']; rp=p+'.reference_plan'
                require(ref,'price','L07',rp,'参考建物価格（税込・万円）',True)
                fees=ref.get('extra_costs')
                valid=bool(fees) and all(f.get('label') and number(f.get('amount')) is not None and number(f['amount'])>=0 for f in fees)
                if not valid: issue('L10',rp+'.extra_costs','追加費用が未入力または不正です。','参考建物価格のほかに必要な費用の内容と金額（万円）を教えていただけますか？')
                if not valid or ref.get('adoption_optional') is not True:
                    issue('N7',rp+'.adoption_optional','自由採用・費用を含む注記を確定できません。','参考プランの採用が自由であることと追加費用を確認していただけますか？')
    def texts(value,path=''):
        if isinstance(value,dict):
            for k,v in value.items():
                if k in {'catch','subcopy','features','comments'}:
                    for t in ([v] if isinstance(v,str) else v or []):
                        yield path+'.'+k,str(t)
                elif isinstance(v,(dict,list)): yield from texts(v,path+'.'+k)
        elif isinstance(value,list):
            for i,v in enumerate(value): yield from texts(v,path+f'.{i}')
    for path,t in texts(data):
        for word in sorted(set(BANNED.findall(t))): issue('S3/S8',path.lstrip('.'),f'禁止語「{word}」が含まれます。',f'「{word}」を事実に基づく表現に修正していただけますか？')
    candidates={'m':[], '分':[], '帖':[], '㎡':[]}
    candidates['m'] += [r.get('width_m') for r in c.get('roads',[])]
    for a in data.get('access',[]):
        if number(a.get('distance_m')) is not None:
            candidates['m'].append(a['distance_m']); candidates['分'].append(minutes(a['distance_m']))
    for b in data.get('buildings',[])+data.get('lands',[]):
        for obj in [b,b.get('reference_plan') or {}]:
            candidates['㎡'] += [obj.get(k) for k in ('land_area','building_area','garage_area')]
            candidates['帖'] += [r.get('jo') for r in obj.get('rooms',[])]
    catch=data.get('catch',[])
    for val,unit in re.findall(r'(\d+(?:\.\d+)?)\s*(㎡|帖|分|m)(?![A-Za-z])',' '.join(catch) if isinstance(catch,list) else str(catch)):
        if number(val) not in [number(x) for x in candidates[unit] if number(x) is not None]:
            issue('S3','catch',f'キャッチの{val}{unit}に一致する項目がありません。',f'キャッチの{val}{unit}の根拠となる項目を確認していただけますか？')
    hero=data.get('hero') or {}
    if hero.get('path') and not hero.get('caption'): issue('N8','hero.caption','画像キャプションがありません。','画像の種類と必要な撮影日を教えていただけますか？')
    return items
