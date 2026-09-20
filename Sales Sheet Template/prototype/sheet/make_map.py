#!/usr/bin/env python3
"""地理院の淡色タイルから出典つきの現地地図を作る。"""
import sys
sys.dont_write_bytecode = True
import argparse
import io
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


def compose_map(lat, lon, zoom, size, label='現地', fetch_tile=None):
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
    px = (lon + 180) / 360 * world
    py = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * world
    left, top = round(px - width / 2), round(py - height / 2)
    canvas = Image.new('RGB', size, 'white')
    for ty in range(top // 256, (top + height - 1) // 256 + 1):
        for tx in range(left // 256, (left + width - 1) // 256 + 1):
            if 0 <= ty < 2**zoom:
                canvas.paste(fetch_tile(zoom, tx % (2**zoom), ty), (tx * 256 - left, ty * 256 - top))
    overlay = Image.new('RGBA', size)
    draw = ImageDraw.Draw(overlay)
    cx, cy = px - left, py - top
    points = [(cx + (28 if i % 2 == 0 else 12) * math.sin(i * math.pi / 5),
               cy - (28 if i % 2 == 0 else 12) * math.cos(i * math.pi / 5)) for i in range(10)]
    draw.polygon(points, fill='#d7141a', outline='white', width=3)
    font = japanese_font(34, bold=True)
    draw.text((cx + 34, cy - 22), f'【{label}】', font=font, fill='#d7141a', stroke_width=3, stroke_fill='white')
    font = japanese_font(17)
    box = draw.textbbox((0, 0), ATTRIBUTION, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    x, y = width - tw - 10, height - th - 10
    draw.rectangle((x - 6, y - 5, width, height), fill=(255, 255, 255, 220))
    draw.text((x - box[0], y - box[1]), ATTRIBUTION, font=font, fill='#222222')
    return Image.alpha_composite(canvas.convert('RGBA'), overlay).convert('RGB')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address')
    parser.add_argument('--lat', type=float)
    parser.add_argument('--lon', type=float)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--zoom', type=int, default=17)
    parser.add_argument('--size', default='900x600')
    parser.add_argument('--label', default='現地')
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--offline-test', action='store_true')
    args = parser.parse_args()
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
        fetch = dummy_tile if args.offline_test else TileFetcher(args.cache or args.out.parent / '.gsi-cache')
        result = compose_map(args.lat, args.lon, args.zoom, size, args.label, fetch)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        result.save(args.out, format='PNG')
        metadata = dict(lat=args.lat, lon=args.lon, zoom=args.zoom, address=title,
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
