"""編集結果の矩形内だけをぼかして元画像へ貼り戻す。"""
import argparse
import json
import math
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name, help_text in [('base', '元画像'), ('edited', '1536x1024 の編集結果'),
                            ('meta', '切り出し JSON'), ('out', '出力 PNG')]:
        parser.add_argument('--' + name, required=True, type=Path, help=help_text)
    parser.add_argument('--rect', help='編集結果上の x0,y0,x1,y1（右・下端は含まない、省略時は差分から自動判定）')
    parser.add_argument('--feather', type=float, default=14, help='編集結果座標でのぼかし半径（既定: 14）')
    args = parser.parse_args()
    if not math.isfinite(args.feather) or args.feather < 0:
        parser.error('feather は0以上の有限値を指定してください')
    with Image.open(args.base) as image:
        base = image.copy()
    with Image.open(args.edited) as image:
        edited = image.convert('RGB')
    if edited.size != (1536, 1024):
        parser.error('編集結果は 1536x1024 にしてください')
    meta = json.loads(args.meta.read_text(encoding='utf-8'))
    try:
        x0, y0, x1, y1 = meta['box']
        if not all(type(v) is int for v in (x0, y0, x1, y1)):
            raise ValueError
        if not (0 <= x0 < x1 <= base.width and 0 <= y0 < y1 <= base.height):
            raise ValueError
        if list(base.size) != meta['size']:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        parser.error('meta の矩形・元画像サイズが不正です')
    original = base.crop((x0, y0, x1, y1)).convert('RGB')
    reference = original.resize(edited.size, Image.Resampling.LANCZOS)
    a = np.asarray(reference.convert('L'), dtype=np.float32)
    b = np.asarray(edited.convert('L'), dtype=np.float32)
    difference = float(np.abs(a[:200] - b[:200]).mean())
    print(f'上端200pxの平均差: {difference:.2f}')
    if difference > 20:
        print('警告: 平均差が20を超えています。画角のずれを確認してください。')
    if args.rect:
        try:
            rect = tuple(map(int, args.rect.split(',')))
            rx0, ry0, rx1, ry1 = rect
            if not (0 <= rx0 < rx1 <= 1536 and 0 <= ry0 < ry1 <= 1024):
                raise ValueError
        except ValueError:
            parser.error('rect は編集結果内の有効な整数座標4個で指定してください')
    else:
        # 色だけの変更も拾い、9x9 の近傍へ膨張する。
        delta = np.abs(np.asarray(reference, dtype=np.int16) - np.asarray(edited, dtype=np.int16)).max(axis=2)
        changed = Image.fromarray((delta >= 40).astype(np.uint8) * 255)
        rect = changed.filter(ImageFilter.MaxFilter(9)).getbbox()
        print(f'自動判定矩形: {rect}')
    if rect is not None:
        print(f'使用矩形: {rect}')
        mask = Image.new('L', edited.size, 0)
        # 縁で急に切れないよう、塗る範囲をぼかし半径の2倍だけ内側へ寄せる。
        rx0, ry0, rx1, ry1 = rect
        inset = math.ceil(args.feather * 2)
        inner = (rx0 + inset, ry0 + inset, rx1 - inset, ry1 - inset)
        if inner[2] - inner[0] < 1 or inner[3] - inner[1] < 1:
            inner = rect
        mask.paste(255, inner)
        mask = mask.filter(ImageFilter.GaussianBlur(args.feather))
        size = original.size
        mask = mask.resize(size, Image.Resampling.LANCZOS)
        # 座標変換後の矩形外を強制的に0にし、補間・ぼかしの漏れを防ぐ。
        rx0, ry0, rx1, ry1 = rect
        local = (math.floor(rx0 * size[0] / 1536), math.floor(ry0 * size[1] / 1024),
                 math.ceil(rx1 * size[0] / 1536), math.ceil(ry1 * size[1] / 1024))
        clipped = Image.new('L', size, 0)
        clipped.paste(mask.crop(local), local)
        patch = edited.resize(size, Image.Resampling.LANCZOS).convert(base.mode)
        base.paste(patch, (x0, y0), clipped)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    base.save(args.out, format='PNG')
    print(args.out)


if __name__ == '__main__':
    main()
