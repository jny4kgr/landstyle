"""販売図面用のシステムフォントと Chromium 系ブラウザの探索。"""
import argparse
import ntpath
import os
import platform
import shutil
import sys


def _safe_console():
    """Windows で出力がパイプ（Codex 経由など）のとき、Python は Shift_JIS（cp932）で書き出す。
    cp932 に無い文字（〜 など）で止まらないよう、書けない文字は置き換える。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(errors='replace')
            except (ValueError, OSError):
                pass


_safe_console()

MAC_FONTS = {
    'ja_serif': ['/System/Library/Fonts/ヒラギノ明朝 ProN.ttc'],
    'ja_sans': ['/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc',
                '/System/Library/Fonts/ヒラギノ丸ゴ ProN W4.ttc'],
    'ja_sans_bold': ['/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc'],
    'en_serif': ['/System/Library/Fonts/Supplemental/Times New Roman.ttf'],
}
WINDOWS_FONTS = {
    'ja_serif': ['yumin.ttf', 'msmincho.ttc'],
    'ja_sans': ['YuGothM.ttc', 'YuGothR.ttc', 'meiryo.ttc', 'msgothic.ttc'],
    'ja_sans_bold': ['YuGothB.ttc', 'meiryob.ttc'],
    'en_serif': ['times.ttf'],
}
LINUX_FONTS = {
    'ja_serif': ['/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc'],
    'ja_sans': ['/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'],
    'ja_sans_bold': ['/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'],
    'en_serif': ['/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman.ttf',
                 '/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf',
                 '/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc'],
}


def _find(candidates, variable, label):
    override = os.environ.get(variable)
    paths = ([override] if override else []) + candidates
    for path in paths:
        if os.path.isfile(path):
            return path
    raise RuntimeError(f'{label}が見つかりません。探索したパス: ' + '、'.join(paths)
                       + f'。環境変数 {variable} にファイルのパスを指定してください。')


def find_font(kind):
    """環境変数を優先し、OS の候補から最初に存在するファイルを返す。"""
    if kind not in MAC_FONTS:
        raise ValueError(f'未知のフォント種別: {kind}')
    system = platform.system()
    if system == 'Windows':
        root = ntpath.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
        paths = [ntpath.join(root, name) for name in WINDOWS_FONTS[kind]]
    else:
        paths = (MAC_FONTS if system == 'Darwin' else LINUX_FONTS)[kind]
    return _find(paths, 'SHEET_FONT_' + kind.upper(), kind + ' フォント')


def find_browser():
    """Chrome、Edge、Chromium を OS ごとのインストール先から探す。"""
    system = platform.system()
    if system == 'Darwin':
        paths = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                 '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
                 '/Applications/Chromium.app/Contents/MacOS/Chromium']
    elif system == 'Windows':
        roots = [os.environ.get('ProgramFiles', r'C:\Program Files'),
                 os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                 os.environ.get('LocalAppData')]
        paths = [ntpath.join(root, suffix) for suffix in
                 (r'Google\Chrome\Application\chrome.exe',
                  r'Microsoft\Edge\Application\msedge.exe') for root in roots if root]
    else:
        names = ['google-chrome', 'chromium', 'microsoft-edge']
        paths = [shutil.which(name) or name for name in names]
    return _find(paths, 'SHEET_BROWSER', 'ブラウザ')


def css_font_stack(kind):
    """SVG/CSS 共通の、Mac の名前を先頭にしたフォント一覧。"""
    if kind == 'ja_serif':
        return '"Hiragino Mincho ProN", "Yu Mincho", "YuMincho", "MS PMincho", "MS Mincho", "Noto Serif CJK JP", serif'
    if kind in ('ja_sans', 'ja_sans_bold'):
        return '"Hiragino Kaku Gothic ProN", "Hiragino Sans", "Yu Gothic", "YuGothic", "Meiryo", "MS Gothic", "Noto Sans CJK JP", sans-serif'
    if kind == 'en_serif':
        return '"Times New Roman", "Liberation Serif", serif'
    raise ValueError(f'未知のフォント種別: {kind}')


def self_test():
    from unittest.mock import patch

    def check(system, env, files, operation, expected):
        with patch.dict(os.environ, env, clear=True), \
                patch.object(platform, 'system', return_value=system), \
                patch.object(os.path, 'isfile', side_effect=lambda p: p in files), \
                patch.object(shutil, 'which', side_effect=lambda n: '/bin/' + n):
            assert operation() == expected

    chrome = r'D:\Apps\Google\Chrome\Application\chrome.exe'
    edge = r'D:\Apps\Microsoft\Edge\Application\msedge.exe'
    env = {'ProgramFiles': r'D:\Apps'}
    check('Windows', env, [chrome, edge], find_browser, chrome)
    check('Windows', env, [edge], find_browser, edge)
    check('Windows', {**env, 'SHEET_BROWSER': r'E:\Browser\custom.exe'},
          [chrome, r'E:\Browser\custom.exe'], find_browser, r'E:\Browser\custom.exe')
    for kind, names in WINDOWS_FONTS.items():
        for name in names:
            path = ntpath.join(r'D:\Windows\Fonts', name)
            check('Windows', {'WINDIR': r'D:\Windows'}, [path], lambda: find_font(kind), path)
        override = r'E:\Fonts\custom.ttf'
        check('Windows', {'SHEET_FONT_' + kind.upper(): override}, [override],
              lambda: find_font(kind), override)
    for kind, paths in MAC_FONTS.items():
        check('Darwin', {}, paths, lambda: find_font(kind), paths[0])
    mac = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
    check('Darwin', {}, [mac], find_browser, mac)
    check('Linux', {}, ['/bin/chromium'], find_browser, '/bin/chromium')
    for kind, paths in LINUX_FONTS.items():
        check('Linux', {}, paths, lambda: find_font(kind), paths[0])
    with patch.dict(os.environ, {}, clear=True), \
            patch.object(platform, 'system', return_value='Windows'), \
            patch.object(os.path, 'isfile', return_value=False):
        for operation, variable, filename in [
                (lambda: find_font('ja_serif'), 'SHEET_FONT_JA_SERIF', 'yumin.ttf'),
                (find_browser, 'SHEET_BROWSER', 'msedge.exe')]:
            try:
                operation()
            except RuntimeError as exc:
                assert all(s in str(exc) for s in ('見つかりません', variable, filename))
            else:
                raise AssertionError('未検出時のエラーがありません')
    print('self-test: Windows Chrome / Edge / 環境変数、macOS、Linux、フォント候補・未検出エラー OK')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        try:
            for kind in MAC_FONTS:
                print(f'{kind}: {find_font(kind)}')
            print(f'browser: {find_browser()}')
        except RuntimeError as exc:
            parser.exit(1, str(exc) + '\n')
