#!/usr/bin/env python3
"""地理院の淡色タイルから出典つきの現地地図を作る。"""
import sys
sys.dont_write_bytecode = True
import argparse
import io
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from PIL import Image, ImageDraw, ImageFont

USER_AGENT = 'LandStyle-SalesSheet/1.0 (GSI pale map composer)'
ATTRIBUTION = '出典：国土地理院（地理院タイル）'


def request(url):
    with urlopen(Request(url, headers={'User-Agent': USER_AGENT}), timeout=20) as response:
        return response.read()


def geocode(address):
    return json.loads(request('https://msearch.gsi.go.jp/address-search/AddressSearch?' + urlencode({'q': address})))


def decode_polyline(encoded):
    """Valhalla の精度6桁の差分座標を復元する。"""
    coords, totals, index = [], [0, 0], 0
    while index < len(encoded):
        for axis in range(2):
            value = shift = 0
            while True:
                if index >= len(encoded) or shift > 30:
                    raise ValueError('invalid polyline')
                byte = ord(encoded[index]) - 63
                index += 1
                if not 0 <= byte <= 63:
                    raise ValueError('invalid polyline')
                value |= (byte & 31) << shift
                shift += 5
                if byte < 32:
                    break
            totals[axis] += ~(value >> 1) if value & 1 else value >> 1
        coords.append(tuple(v / 1e6 for v in totals))
    return coords


class RouteFetcher:
    def __init__(self, cache):
        self.cache = Path(cache)

    def fetch(self, service, url):
        folder = self.cache / service
        path = folder / (hashlib.sha256(url.encode()).hexdigest() + '.json')
        if path.is_file():
            return json.loads(path.read_text(encoding='utf-8'))
        if service == 'nominatim':
            # Persist the timestamp to respect the interval across CLI invocations.
            stamp = folder / 'last-request'
            last = float(stamp.read_text()) if stamp.exists() else 0
            time.sleep(max(0, 1 - (time.time() - last)))
            folder.mkdir(parents=True, exist_ok=True)
            stamp.write_text(str(time.time()))
        data = json.loads(request(url))
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return data

    def station(self, name, lat, lon):
        results = self.fetch('nominatim', 'https://nominatim.openstreetmap.org/search?' +
                             urlencode(dict(q=name, format='jsonv2', countrycodes='jp', limit=5)))
        if not results:
            raise ValueError('駅が見つかりません')
        chosen = next((r for r in results if r.get('type') in ('train_station', 'station')), results[0])
        end = (float(chosen['lat']), float(chosen['lon']))
        query = dict(locations=[dict(lat=lat, lon=lon), dict(lat=end[0], lon=end[1])],
                     costing='pedestrian', units='kilometers')
        trip = self.fetch('valhalla', 'https://valhalla1.openstreetmap.de/route?' +
                          urlencode({'json': json.dumps(query, separators=(',', ':'))}))['trip']  # 空白なしの JSON にする（'+' に変換された空白を Valhalla が解釈できず 400 になる）
        route = [point for leg in trip['legs'] for point in decode_polyline(leg['shape'])]
        distance = float(trip['summary']['length']) * 1000
        if len(route) < 2 or not math.isfinite(distance) or distance < 0:
            raise ValueError('歩行ルートがありません')
        return station_record(name, chosen.get('display_name', name), end, route, distance)


def station_record(name, display_name, end, route, distance):
    distance_m = int(math.floor(distance + .5))
    return dict(name=name, display_name=display_name, lat=end[0], lon=end[1],
                distance_m=distance_m, walk_min=math.ceil(distance_m / 80), route=route)


def offline_station(name, lat, lon):
    delta = 600 / math.sqrt(2)
    south = delta / 6371000 * 180 / math.pi
    west = south / math.cos(math.radians(lat))
    route = [(lat, lon), (lat, lon-west/2), (lat-south, lon-west/2), (lat-south, lon-west)]
    return station_record(name, name + '（オフライン検証用）', route[-1], route, delta*2)


def project(lat, lon, zoom):
    world = 256 * 2**zoom
    return ((lon+180)/360*world, (1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*world)


def fit_route(lat, lon, station, size):
    coords = [(lat, lon), (station['lat'], station['lon']), *station['route']]
    for zoom in range(18, -1, -1):
        points = [project(a, b, zoom) for a,b in coords]
        xs, ys = zip(*points)
        # Each edge keeps at least 12% of the canvas (and 80px for markers).
        # One extra pixel covers rounding of the tile origin.
        margin_x, margin_y = max(80, size[0]*.12)+1, max(80, size[1]*.12)+1
        if max(xs)-min(xs) <= size[0]-2*margin_x and max(ys)-min(ys) <= size[1]-2*margin_y:
            break
    x, y = (min(xs)+max(xs))/2, (min(ys)+max(ys))/2
    world = 256 * 2**zoom
    return zoom, (math.degrees(math.atan(math.sinh(math.pi*(1-2*y/world)))), x/world*360-180)


class TileFetcher:
    def __init__(self, cache):
        self.cache = Path(cache)
        self.last_request = None

    def __call__(self, zoom, x, y):
        path = self.cache / str(zoom) / str(x) / f'{y}.png'
        if path.is_file():
            with Image.open(path) as tile:
                tile.load()
                if tile.size == (256, 256):
                    return tile.convert('RGB')
        if self.last_request is not None:
            time.sleep(max(0, .2 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        try:
            payload = request(f'https://cyberjapandata.gsi.go.jp/xyz/pale/{zoom}/{x}/{y}.png')
            with Image.open(io.BytesIO(payload)) as source:
                source.load()
                if source.size != (256, 256):
                    raise ValueError('タイルのサイズが256pxではありません')
                tile = source.convert('RGB')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            return tile
        except Exception as exc:
            raise RuntimeError(f'地図タイル取得失敗 z={zoom} x={x} y={y}（タイムアウト20秒）: {exc}') from exc


def dummy_tile(zoom, x, y):
    tile = Image.new('RGB', (256, 256), '#edf0ed')
    draw = ImageDraw.Draw(tile)
    for n in range(0, 256, 32):
        draw.line((n, 0, n, 255), fill='#bac9c0')
        draw.line((0, n, 255, n), fill='#bac9c0')
    draw.text((8, 8), f'OFFLINE {zoom}/{x}/{y}', fill='#333333')
    return tile


def japanese_font(size, bold=False):
    # Use installed system fonts only; never download fonts.
    bold_paths = ['/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc',
                  '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'] if bold else []
    for path in bold_paths + ['/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc',
                 '/System/Library/Fonts/ヒラギノ丸ゴ ProN W4.ttc',
                 '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc']:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise RuntimeError('出典表示に必要な日本語システムフォントが見つかりません')


def _label_layout(draw, text, point, size, occupied, font_size, stroke, radius):
    """Place the entire ink box inside the image and clear of earlier labels."""
    width, height = size
    x, y = point
    gap = 6
    for fs in range(font_size, 9, -2):
        font = japanese_font(fs, bold=True)
        ink = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
        w, h = ink[2]-ink[0], ink[3]-ink[1]
        if w > width-12 or h > height-12:
            continue
        # Prefer the other side of the marker before moving farther away.
        preferred = [(x+radius+gap, y-h/2), (x-radius-gap-w, y-h/2),
                     (x-w/2, y-radius-gap-h), (x-w/2, y+radius+gap)]
        xs = {6, width-w-6, max(6, min(x-w/2, width-w-6))}
        ys = {6, height-h-6, max(6, min(y-h/2, height-h-6))}
        for a,b,c,d in occupied:
            xs.update((a-gap-w, c+gap))
            ys.update((b-gap-h, d+gap))
        candidates = preferred + sorted(((a,b) for a in xs for b in ys),
                                        key=lambda p: ((p[0]+w/2-x)**2+(p[1]+h/2-y)**2, p))
        for a,b in candidates:
            box = (a,b,a+w,b+h)
            if a < 6 or b < 6 or a+w > width-6 or b+h > height-6:
                continue
            if any(not (a+w+gap <= l or a >= r+gap or b+h+gap <= t or b >= bottom+gap)
                   for l,t,r,bottom in occupied):
                continue
            occupied.append(box)
            return (a-ink[0], b-ink[1]), font, box
    print(f'warning: ラベルを重ならずに配置できません: {text}', file=sys.stderr)
    return None


def compose_map(lat, lon, zoom, size, label='現地', fetch_tile=None, stations=(), center=None):
    """fetch_tile(z, x, y) -> PIL.Image. Center pixel is the supplied location."""
    if not (-85.05112878 <= lat <= 85.05112878 and -180 <= lon <= 180):
        raise ValueError('緯度・経度がWebメルカトルの範囲外です')
    if not 0 <= zoom <= 18:
        raise ValueError('zoom は0〜18で指定してください')
    width, height = size
    if not (360 <= width <= 6000 and 200 <= height <= 6000):
        raise ValueError('size は幅360〜6000、高さ200〜6000pxで指定してください')
    if fetch_tile is None:
        raise ValueError('タイル取得関数が必要です')
    world = 256 * 2**zoom
    px, py = project(*(center or (lat, lon)), zoom)
    left, top = round(px - width / 2), round(py - height / 2)
    canvas = Image.new('RGB', size, 'white')
    for ty in range(top // 256, (top + height - 1) // 256 + 1):
        for tx in range(left // 256, (left + width - 1) // 256 + 1):
            if 0 <= ty < 2**zoom:
                canvas.paste(fetch_tile(zoom, tx % (2**zoom), ty), (tx * 256 - left, ty * 256 - top))
    overlay = Image.new('RGBA', size)
    draw = ImageDraw.Draw(overlay)
    def pixel(a, b):
        x, y = project(a, b, zoom)
        return x-left, y-top
    for station in stations:
        route = [pixel(a, b) for a,b in station['route']]
        draw.line(route, fill='white', width=12, joint='curve')
        draw.line(route, fill='#1e6fd9', width=7, joint='curve')
        x, y = pixel(station['lat'], station['lon'])
        draw.ellipse((x-9,y-9,x+9,y+9), fill='#1e6fd9', outline='white', width=3)
    cx, cy = pixel(lat, lon)
    points = [(cx + (28 if i % 2 == 0 else 12) * math.sin(i * math.pi / 5),
               cy - (28 if i % 2 == 0 else 12) * math.cos(i * math.pi / 5)) for i in range(10)]
    draw.polygon(points, fill='#d7141a', outline='white', width=3)
    attribution = ATTRIBUTION + ('／© OpenStreetMap contributors' if stations else '')
    font = japanese_font(17)
    while font.getlength(attribution) > width-20:
        font = japanese_font(font.size-1)
    box = draw.textbbox((0, 0), attribution, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    x, y = width - tw - 10, height - th - 10
    occupied = [(x-6, y-5, width, height), (cx-28, cy-28, cx+28, cy+28)]
    for station in stations:
        sx, sy = pixel(station['lat'], station['lon'])
        occupied.append((sx-9, sy-9, sx+9, sy+9))
    labels = [(f'【{label}】', (cx,cy), 34, 3, 28, '#d7141a')]
    labels.extend((station['name'], pixel(station['lat'], station['lon']), 24, 2, 9, '#1e6fd9')
                  for station in stations)
    for text, point, fs, stroke, radius, color in labels:
        layout = _label_layout(draw, text, point, size, occupied, fs, stroke, radius)
        if layout:
            position, label_font, _ = layout
            draw.text(position, text, font=label_font, fill=color, stroke_width=stroke, stroke_fill='white')
    draw.rectangle((x - 6, y - 5, width, height), fill=(255, 255, 255, 220))
    draw.text((x - box[0], y - box[1]), attribution, font=font, fill='#222222')
    return Image.alpha_composite(canvas.convert('RGBA'), overlay).convert('RGB')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address')
    parser.add_argument('--lat', type=float)
    parser.add_argument('--lon', type=float)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--zoom', type=int)
    parser.add_argument('--size', default='900x600')
    parser.add_argument('--label', default='現地')
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--offline-test', action='store_true')
    parser.add_argument('--station', action='append', default=[])
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        assert decode_polyline('_izlhA~rlgdF_{geC~ywl@_kwzCn`{nI') == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
        print('polyline precision 6: OK')
        return 0
    if args.out is None:
        parser.error('--out が必要です')
    if bool(args.address) == (args.lat is not None or args.lon is not None):
        parser.error('--address または --lat と --lon の組を指定してください')
    if not args.address and (args.lat is None or args.lon is None):
        parser.error('--lat と --lon は両方必要です')
    if args.offline_test and args.address:
        parser.error('--offline-test は --lat と --lon で指定してください')
    try:
        title = None
        if args.address:
            results = geocode(args.address)
            if not results:
                print('住所検索の結果が0件でした。住所を確認してください。', file=sys.stderr)
                return 3
            args.lon, args.lat = results[0]['geometry']['coordinates']
            title = results[0]['properties']['title']
            print(f'採用した住所: {title} / 緯度 {args.lat} / 経度 {args.lon}（検索結果{len(results)}件）')
        size = tuple(map(int, args.size.lower().split('x')))
        stations = []
        first_station = None
        cache = args.cache or args.out.parent / '.gsi-cache'
        routing = RouteFetcher(cache)
        for index, name in enumerate(args.station):
            try:
                station = offline_station(name, args.lat, args.lon) if args.offline_test else routing.station(name, args.lat, args.lon)
                stations.append(station)
                if index == 0:
                    first_station = station
                print(f"{name}まで 道路距離 約{station['distance_m']}m・徒歩{station['walk_min']}分（OpenStreetMap の歩行ルート）。この距離で合っていますか？")
                print(f"採用した検索結果: {station['display_name']}")
            except Exception as exc:
                print(f'warning: {name}: {exc}', file=sys.stderr)
        zoom, center = args.zoom if args.zoom is not None else 17, (args.lat, args.lon)
        if args.zoom is None and first_station:
            zoom, center = fit_route(args.lat, args.lon, first_station, size)
        fetch = dummy_tile if args.offline_test else TileFetcher(args.cache or args.out.parent / '.gsi-cache')
        result = compose_map(args.lat, args.lon, zoom, size, args.label, fetch, stations, center)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        result.save(args.out, format='PNG')
        metadata = dict(lat=args.lat, lon=args.lon, zoom=zoom, center=list(center), address=title,
                        stations=stations, sources=["gsi", "osm"] if stations else ["gsi"],
                        retrieved_at=datetime.now(timezone.utc).isoformat(), offline_test=args.offline_test)
        sidecar = Path(str(args.out) + '.json')
        sidecar.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'出力: {args.out.resolve()}\nメタデータ: {sidecar.resolve()}')
        return 0
    except Exception as exc:
        print(f'地図作成に失敗しました: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
