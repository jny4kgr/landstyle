"""指定範囲を 3:2 に広げ、画像編集用 PNG と座標 JSON を保存する。"""
import argparse
import json
import math
from pathlib import Path
from PIL import Image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, type=Path, help='元画像')
    parser.add_argument('--box', required=True, help='矩形 x0,y0,x1,y1（右・下端は含まない）')
    parser.add_argument('--out', required=True, type=Path, help='編集用 PNG')
    args = parser.parse_args()
    with Image.open(args.image) as source:
        source.load()
        try:
            x0, y0, x1, y1 = map(int, args.box.split(','))
            if not (0 <= x0 < x1 <= source.width and 0 <= y0 < y1 <= source.height):
                raise ValueError
        except ValueError:
            parser.error('box は画像内の有効な整数座標4個で指定してください')
        unit = math.ceil(max((x1 - x0) / 3, (y1 - y0) / 2))
        width, height = 3 * unit, 2 * unit
        if width > source.width or height > source.height:
            parser.error('3:2 に広げた矩形が元画像に収まりません')
        left = min(max(0, round((x0 + x1 - width) / 2)), source.width - width)
        top = min(max(0, round((y0 + y1 - height) / 2)), source.height - height)
        box = [left, top, left + width, top + height]
        result = source.crop(box).resize((1536, 1024), Image.Resampling.LANCZOS)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        result.save(args.out, format='PNG')
        meta = Path(str(args.out) + '.json')
        meta.write_text(json.dumps({'image': str(args.image.resolve()), 'box': box,
                                    'size': list(source.size)}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'使用矩形: {box}\n元画像: {args.image.resolve()}\n{args.out}\n{meta}')


if __name__ == '__main__':
    main()
