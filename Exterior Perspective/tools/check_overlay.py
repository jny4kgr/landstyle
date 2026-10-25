"""元画像の輪郭を赤、新画像を緑で重ね、一致率を表示する。"""
import argparse
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter, ImageOps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--a', required=True, type=Path, help='元画像')
    parser.add_argument('--b', required=True, type=Path, help='新画像')
    parser.add_argument('--out', required=True, type=Path, help='重ね画像 PNG')
    parser.add_argument('--tol', type=int, default=2, help='輪郭のずれの許容幅（px、既定 2、0 以上）')
    args = parser.parse_args()
    if args.tol < 0:
        parser.error('--tol は 0 以上にしてください')
    with Image.open(args.a) as image:
        a = image.convert('L')
    with Image.open(args.b) as image:
        b = image.convert('L').resize(a.size, Image.Resampling.LANCZOS)
    edges = [np.asarray(ImageOps.autocontrast(im.filter(ImageFilter.FIND_EDGES))) for im in (a, b)]
    first, second = [edge >= 64 for edge in edges]
    if args.tol == 0:
        union = np.count_nonzero(first | second)
        score = np.count_nonzero(first & second) / union if union else 1.0
    else:
        expanded = [np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).filter(
            ImageFilter.MaxFilter(2 * args.tol + 1))) > 0 for mask in (first, second)]
        total = np.count_nonzero(first) + np.count_nonzero(second)
        matched = np.count_nonzero(first & expanded[1]) + np.count_nonzero(second & expanded[0])
        score = matched / total if total else 1.0
    output = Image.fromarray(np.stack((edges[0], edges[1], np.zeros_like(edges[0])), axis=2))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    output.save(args.out, format='PNG')
    print(f'輪郭一致率（許容 {args.tol}px）: {score:.1%}')
    print('目安: 90% 以上。最後は重ね画像を目で見て判断')
    print(args.out)


if __name__ == '__main__':
    main()
