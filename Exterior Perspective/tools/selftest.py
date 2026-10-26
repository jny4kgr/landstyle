"""一時フォルダの合成画像だけで外観パース用ツールを検証する。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import numpy as np
import pymupdf
from PIL import Image, ImageDraw

TOOLS = Path(__file__).resolve().parent


def run(name, *args, success=True):
    """同じ Python で CLI を実行し、終了状態を確認する。"""
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, str(TOOLS / f'{name}.py'), *map(str, args)],
                            capture_output=True, text=True, encoding='utf-8', env=env)
    if success:
        assert result.returncode == 0, result.stdout + result.stderr
        print(result.stdout, end='')
    else:
        assert result.returncode != 0, '不正入力が受理されました'
    return result.stdout


def pixels(path):
    with Image.open(path) as image:
        return np.asarray(image).copy()


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    with tempfile.TemporaryDirectory(prefix='exterior_') as directory:
        root = Path(directory)
        base = root / '元画像.png'
        image = Image.new('RGB', (1536, 1024), (180, 210, 230))
        draw = ImageDraw.Draw(image)
        draw.rectangle((300, 300, 1100, 850), fill=(140, 110, 80))
        draw.polygon([(250, 300), (700, 80), (1150, 300)], fill=(60, 70, 80))
        draw.rectangle((500, 500, 700, 850), fill=(20, 30, 50))
        image.save(base)
        source = pixels(base)
        pdfs = root / '資料'
        pdfs.mkdir()
        for name, count in [('一枚.pdf', 1), ('複数.PDF', 2)]:
            with pymupdf.open() as doc:
                for _ in range(count):
                    page = doc.new_page(width=842, height=595)
                    page.draw_rect(pymupdf.Rect(20, 20, 200, 200), color=(1, 0, 0))
                doc.save(pdfs / name)
        converted = root / '変換'
        run('pdf_to_png', pdfs, converted)
        assert {p.name for p in converted.glob('*.png')} == {'一枚.png', '複数_p1.png', '複数_p2.png'}
        with Image.open(converted / '一枚.png') as png:
            assert png.size == (2339, 1653)
        crop = root / '編集用.png'
        run('crop_for_edit', '--image', base, '--box', '300,300,900,700', '--out', crop)
        meta = Path(str(crop) + '.json')
        data = json.loads(meta.read_text(encoding='utf-8'))
        assert data['box'] == [300, 300, 900, 700]
        assert data['image'] == str(base.resolve())
        assert pixels(crop).shape == (1024, 1536, 3)
        edited = root / '編集結果.png'
        with Image.open(crop) as im:
            changed = im.copy()
        ImageDraw.Draw(changed).rectangle((384, 256, 767, 511), fill=(220, 30, 210))
        changed.save(edited)
        pasted = root / '貼り戻し.png'
        run('paste_back', '--base', base, '--edited', edited, '--meta', meta,
            '--rect', '384,256,768,512', '--out', pasted)
        result = pixels(pasted)
        assert result.shape == source.shape
        outside = np.ones(source.shape[:2], dtype=bool)
        outside[400:500, 450:600] = False
        assert np.array_equal(result[outside], source[outside])
        border = ~outside
        border[401:499, 451:599] = False
        assert np.abs(result[border].astype(np.int16) - source[border].astype(np.int16)).mean() < 3
        assert np.any(result[420:480, 470:580] != source[420:480, 470:580])
        auto = root / '自動.png'
        output = run('paste_back', '--base', base, '--edited', edited, '--meta', meta, '--out', auto)
        assert '自動判定矩形: (380, 252, 772, 516)' in output
        outside_auto = np.ones(source.shape[:2], dtype=bool)
        outside_auto[398:502, 448:602] = False
        assert np.array_equal(pixels(auto)[outside_auto], source[outside_auto])
        assert np.any(pixels(auto) != source)
        unchanged = root / '差分なし.png'
        run('paste_back', '--base', base, '--edited', crop, '--meta', meta, '--out', unchanged)
        assert np.array_equal(pixels(unchanged), source)
        edge = root / '端.png'
        run('crop_for_edit', '--image', base, '--box', '1400,700,1536,1000', '--out', edge)
        edge_box = json.loads(Path(str(edge) + '.json').read_text(encoding='utf-8'))['box']
        assert edge_box == [1086, 700, 1536, 1000]
        run('crop_for_edit', '--image', base, '--box', '0,0,1536,1024', '--out', edge)
        run('crop_for_edit', '--image', base, '--box', '0,0,1537,1024', '--out', edge, success=False)
        narrow = root / '縦長.png'
        image.crop((0, 0, 500, 1000)).save(narrow)
        run('crop_for_edit', '--image', narrow, '--box', '0,0,500,1000', '--out', edge, success=False)
        overlay = root / '輪郭.png'
        run('check_overlay', '--a', base, '--b', pasted, '--out', overlay)
        assert pixels(overlay).shape == source.shape
        output = run('check_overlay', '--a', base, '--b', base, '--out', overlay)
        assert '輪郭一致率（許容 2px）: 100.0%' in output
        shifted = root / '1pxずれ.png'
        shifted_image = image.copy()
        shifted_image.paste(image.crop((0, 0, 1535, 1024)), (1, 0))
        shifted_image.save(shifted)
        output = run('check_overlay', '--a', base, '--b', shifted, '--tol', '2', '--out', overlay)
        score = float(output.splitlines()[0].split(': ')[1].removesuffix('%'))
        assert score >= 90, output
        output = run('check_overlay', '--a', base, '--b', shifted, '--tol', '0', '--out', overlay)
        contours = pixels(overlay)
        first, second = contours[:, :, 0] >= 64, contours[:, :, 1] >= 64
        strict_score = np.count_nonzero(first & second) / np.count_nonzero(first | second)
        assert f'輪郭一致率（許容 0px）: {strict_score:.1%}' in output
        run('check_overlay', '--a', base, '--b', base, '--tol', '-1', '--out', overlay, success=False)
        final = root / '看板.png'
        output = run('finish', '--image', pasted, '--out', final, '--sign', '150x90')
        assert '8858x5315px' in output
        result = pixels(final)
        height, width = result.shape[:2]
        assert width == 1536 and abs(height - width * 3 / 5) <= 1
        assert np.array_equal(result, pixels(pasted)[:height])
        wide = root / '横長.png'
        image.crop((0, 0, 1536, 600)).save(wide)
        run('finish', '--image', wide, '--out', final)
        assert np.array_equal(pixels(final), pixels(wide)[:, 268:1268])
    print('selftest OK')


if __name__ == '__main__':
    main()
