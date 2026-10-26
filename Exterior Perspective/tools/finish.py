"""看板比率で上側を残して切り出し、印刷に必要な画素数と倍率を示す。"""
import argparse
import math
from pathlib import Path
from PIL import Image


def pair(value, separator):
    """正の有限値2個を読み取る。"""
    a, b = map(float, value.split(separator))
    if not all(math.isfinite(v) and v > 0 for v in (a, b)):
        raise ValueError
    return a, b


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, type=Path, help='入力画像')
    parser.add_argument('--out', required=True, type=Path, help='出力 PNG')
    parser.add_argument('--ratio', default='5:3', help='横:縦（既定: 5:3）')
    parser.add_argument('--sign', help='看板の横x縦 cm（例: 150x90）')
    parser.add_argument('--dpi', type=float, default=150, help='印刷解像度（既定: 150）')
    args = parser.parse_args()
    try:
        rw, rh = pair(args.ratio, ':')
        sign = pair(args.sign, 'x') if args.sign else None
        if not math.isfinite(args.dpi) or args.dpi <= 0:
            raise ValueError
    except ValueError:
        parser.error('比率・看板サイズ・dpi は正の有限値で指定してください')
    with Image.open(args.image) as image:
        if image.width / image.height <= rw / rh:
            width, height = image.width, max(1, round(image.width * rh / rw))
        else:
            width, height = max(1, round(image.height * rw / rh)), image.height
        left = (image.width - width) // 2
        output = image.crop((left, 0, left + width, height))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        output.save(args.out, format='PNG')
    print(f'出力サイズ: {width}x{height}px\n{args.out}')
    if sign:
        pixels = [round(cm / 2.54 * args.dpi) for cm in sign]
        scale = max(pixels[0] / width, pixels[1] / height)
        print(f'必要画素数: {pixels[0]}x{pixels[1]}px\n必要拡大倍率: {scale:.2f}倍')


if __name__ == '__main__':
    main()
