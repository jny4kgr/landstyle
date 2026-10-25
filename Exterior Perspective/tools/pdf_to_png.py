"""フォルダ内の PDF をページごとの PNG に変換する。"""
import argparse
from pathlib import Path
import pymupdf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='入力フォルダ')
    parser.add_argument('output', type=Path, help='出力フォルダ')
    parser.add_argument('--dpi', type=int, default=200, help='解像度（既定: 200）')
    args = parser.parse_args()
    if not args.input.is_dir() or args.dpi <= 0:
        parser.error('入力フォルダと正の dpi を指定してください')
    args.output.mkdir(parents=True, exist_ok=True)
    for path in sorted(args.input.iterdir()):
        if not path.is_file() or path.suffix.lower() != '.pdf':
            continue
        with pymupdf.open(path) as doc:
            for index, page in enumerate(doc):
                suffix = '' if len(doc) == 1 else f'_p{index + 1}'
                out = args.output / f'{path.stem}{suffix}.png'
                page.get_pixmap(dpi=args.dpi, colorspace=pymupdf.csRGB, alpha=False).save(out)
                print(out)


if __name__ == '__main__':
    main()
