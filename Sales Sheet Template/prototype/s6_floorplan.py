#!/usr/bin/env python3
"""S6 間取り図の試作: CAD 図面 PDF(ベクター)から販売図面風の間取り図 PNG を作る。

方式(ベクター加工 + 塗りつぶし):
  1. PDF の描画要素から黒い線だけを取り出す(赤・青の書き込み、文字は捨てる)
  2. 建物の範囲を連結成分で推定し、範囲外(寸法線・図枠・方位記号)を捨てる
  3. 端点がどこにも繋がっていない線(引出線・寸法補助線・中心線)を反復して刈る
  4. ラスタライズ → 塗り潰し記号(展開記号の黒三角)を消す
  5. 室名テキストの位置を種に flood fill。面積が想定を超えたら壁を太らせて再試行(ドアの隙間対策)
  6. 部屋でも外部でもない細い閉領域を壁とみなしてグレーで塗る
  7. 販売図面の表記(LDK / 17.0J 等)で室名を描き直し、方位記号と階表示を付ける

依存: Python 3.10+, pymupdf, opencv-python-headless, numpy, pillow
  uv venv .venv && uv pip install --python .venv/bin/python pymupdf opencv-python-headless numpy pillow

使い方:
  python s6_floorplan.py --pdf 図面.pdf --page 8 --floor 2F --out out/2F.png --svg out/2F.svg
"""
from __future__ import annotations

import argparse
import base64
import io
import json
from html import escape
import math
import re
import unicodedata
import sys
from pathlib import Path

import cv2
import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- 設定
S = 4  # ラスタ解像度 px / pt (1/50 図面で 1px ≒ 4.4mm)
PT_MM = 25.4 / 72 * 50  # 1pt の実寸 mm (S=1/50)

COLORS = {  # 完成版(hamasaki-1.png)から採った RGB
    "ldk": (253, 235, 246),
    "room": (254, 241, 225),
    "water": (217, 243, 253),
    "storage": (255, 254, 230),
    "white": (255, 255, 255),
}
WALL_RGB = (145, 143, 143)
LINE_RGB = (35, 31, 32)

# CAD 室名(NFKC 正規化後)→ (表記テンプレート, 色キー)。上から順に最初に一致したもの。
# {j} は帖数(例 17.0J)。表記 None は「塗らずに線ごと消す」(下屋・庇)。
ROOM_MAP: list[tuple[str, str | None, str]] = [
    (r"^LDK$", "LDK\n{j}", "ldk"),
    (r"^洋室", "洋室\n{j}", "room"),
    (r"^納戸$", "Service\nroom\n{j}", "room"),
    (r"^(洗面脱衣室|洗面所|脱衣室)$", "Powder\nRoom", "water"),
    (r"^(UB|ユニットバス|浴室)$", "Bath\nRoom", "water"),
    (r"^(トイレ|WC)$", "", "water"),
    (r"^(物入|収納|クローゼット|WIC|SIC)$", "Cl.", "storage"),
    (r"^玄関$", "Entrance", "white"),
    (r"^バルコニー$", "Balcony", "white"),
    (r"^(階段|ホール|廊下|車庫|階段下)$", "", "white"),
    (r"^(庇|下屋)", None, "white"),
]
KEEP_TEXT = {"UP", "DN", "冷"}  # 図面の文字のうち、そのまま残すもの

FONT_EN = "/System/Library/Fonts/Supplemental/Times New Roman.ttf"
FONT_JA = "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc"

MIN_ROOM_M2 = 0.2  # これより小さい閉領域は部屋とみなさない(収納の最小 455×910mm ≒ 0.41㎡ より十分小さい)
FLOAT_MAXLEN = 12.0  # pt: 部屋の中に浮いている線の成分で、これより小さいもの(破線の1片)は消す
FLOAT_RING = 4  # px: 浮いているかを調べる周囲の幅
DANGLE_TOL = 0.8  # pt: 端点がこれ以内に他の線があれば「繋がっている」
DASH_GAP = 3.0  # pt: 破線の片同士の最大の隙間
DANGLE_MINLEN = 3.5  # pt: これより短い線(破線の1片・円弧の1片)は刈らない


# ---------------------------------------------------------------- PDF 読み取り
def norm(t: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", t))


def read_page(pdf: str, pno: int):
    page = pymupdf.open(pdf)[pno - 1]
    segs, widths = [], []
    for dr in page.get_drawings():
        col = dr.get("color")
        if col is None or max(col) > 0.05:  # 黒以外(赤・青の書き込み)は捨てる
            continue
        for it in dr["items"]:
            if it[0] == "l":
                pts = [it[1], it[2]]
            elif it[0] == "c":
                pts = [it[1], it[2], it[3], it[4]]
            elif it[0] == "re":
                r = it[1]
                pts = [r.tl, r.tr, r.br, r.bl, r.tl]
            elif it[0] == "qu":
                q = it[1]
                pts = [q.ul, q.ur, q.lr, q.ll, q.ul]
            else:
                continue
            for a, b in zip(pts, pts[1:]):
                segs.append((a.x, a.y, b.x, b.y))
                widths.append(dr.get("width") or 0.3)
    spans = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            for s in ln["spans"]:
                if s["text"].strip():
                    spans.append(dict(text=norm(s["text"]), raw=unicodedata.normalize("NFKC", s["text"]),
                                      bbox=s["bbox"], color=s["color"], dir=ln["dir"]))
    return page.rect, np.array(segs, np.float64), np.array(widths), spans


def raster_segments(segs, shape, origin, scale, thick=1):
    img = np.zeros(shape, np.uint8)
    ox, oy = origin
    for x1, y1, x2, y2 in segs:
        p1 = (int(round((x1 - ox) * scale * 4)), int(round((y1 - oy) * scale * 4)))
        p2 = (int(round((x2 - ox) * scale * 4)), int(round((y2 - oy) * scale * 4)))
        cv2.line(img, p1, p2, 255, thick, lineType=cv2.LINE_8, shift=2)
    return img


def _component_bbox(rect, segs, anchors):
    """線を低解像度で描いて膨張し、図枠以外の連結成分から建物らしいものを選ぶ。
    室名(anchors)を最も多く囲む成分 → 同数なら面積最大。囲む室名の数も返す。"""
    sc = 2
    img = raster_segments(segs, (int(rect.height * sc), int(rect.width * sc)), (0, 0), sc, thick=1)
    img = cv2.dilate(img, np.ones((7, 7), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats((img > 0).astype(np.uint8))
    best, best_key = None, None
    page_area = img.shape[0] * img.shape[1]
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if w * h > 0.5 * page_area:  # 図枠
            continue
        cnt = sum(x <= ax * sc <= x + w and y <= ay * sc <= y + h for ax, ay in anchors)
        key = (cnt, a)
        if best is None or key > best_key:
            best, best_key = i, key
    if best is None:
        return None, 0
    x, y, w, h, _ = st[best]
    return (x / sc, y / sc, (x + w) / sc, (y + h) / sc), best_key[0]


def building_bbox(rect, segs, widths=None, anchors=()):
    """建物の範囲を推定する。
    1. 全ての黒線の連結成分のうち、室名を最も多く囲むもの(図枠は除く)
    2. 1 が室名の過半を囲めない場合(通り芯・敷地境界線・引出線で建物が図枠と繋がっている図面)は、
       最も細い線幅の線(通り芯・寸法線・引出線・ハッチ)を除いた太い線だけで同じ選び方をする"""
    anchors = list(anchors)
    bbox, cnt = _component_bbox(rect, segs, anchors)
    need = (len(anchors) + 1) // 2
    if not anchors or cnt >= need or widths is None:
        return bbox
    classes = np.unique(np.round(widths, 2))
    if len(classes) >= 2:
        thick = np.round(widths, 2) > classes[0]
        bbox2, cnt2 = _component_bbox(rect, segs[thick], anchors)
        if bbox2 is not None and cnt2 > cnt:
            grown = grow_bbox(rect, segs, bbox2, anchors)
            print(f"  building bbox: thin lines excluded ({cnt} -> {cnt2}/{len(anchors)} room names), "
                  f"core {[round(v) for v in bbox2]} -> grown {[round(v) for v in grown]}")
            return grown
    return bbox


GROW_MM = 800  # 太線だけで求めた建物範囲を、細線(バルコニーの手すりなど)で広げてよい最大距離
EDGE_MM = 400  # 室名がこの距離より範囲の辺に近い(または外にある)側だけ広げる


def grow_bbox(rect, segs, core, anchors=()):
    """太線だけで求めた範囲(core)を、細線を含む全ての線で広げる。
    バルコニーの手すりが細線で描かれている図面では、太線の範囲の辺にバルコニーの室名が接してしまう。
    室名が辺から EDGE_MM 以内(または外)にある側だけ、core を GROW_MM 広げた枠に収まる線
    (枠をまたぐ実線・破線は除く)のうち core に掛かる連結成分の外接矩形まで広げる。"""
    e, edge = GROW_MM / PT_MM, EDGE_MM / PT_MM
    sides = [any(ax < core[0] + edge for ax, _ in anchors), any(ay < core[1] + edge for _, ay in anchors),
             any(ax > core[2] - edge for ax, _ in anchors), any(ay > core[3] - edge for _, ay in anchors)]
    if not any(sides):
        return core
    box = (core[0] - e, core[1] - e, core[2] + e, core[3] + e)
    inb = ((segs[:, [0, 2]] >= box[0]).all(1) & (segs[:, [0, 2]] <= box[2]).all(1)
           & (segs[:, [1, 3]] >= box[1]).all(1) & (segs[:, [1, 3]] <= box[3]).all(1))
    s = segs[inb & ~dashed_crossing(segs, box)]
    if not len(s):
        return core
    sc = 2
    img = raster_segments(s, (int(rect.height * sc), int(rect.width * sc)), (0, 0), sc, thick=1)
    img = cv2.dilate(img, np.ones((3, 3), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats((img > 0).astype(np.uint8))
    g = list(core)
    for i in range(1, n):
        x, y, w, h, _ = st[i]
        if x / sc > core[2] or (x + w) / sc < core[0] or y / sc > core[3] or (y + h) / sc < core[1]:
            continue
        g = [min(g[0], x / sc), min(g[1], y / sc), max(g[2], (x + w) / sc), max(g[3], (y + h) / sc)]
    return tuple(g[k] if sides[k] else core[k] for k in range(4))


def dashed_crossing(segs, bbox, min_pieces=4, gap_min=0.3, gap_max=2.5, span=0.7):
    """建物範囲をまたぐ破線・一点鎖線(通り芯など)の片を返す(bool マスク)。
    同じ直線上で gap_max 以内に続く片(接触も含む)を1本の鎖にまとめ、
    gap_min〜gap_max の「破線の隙間」が min_pieces 以上ある鎖のうち、範囲の内外にまたがるもの、
    または範囲の幅(高さ)の span 倍以上に伸びるもの(通り芯の丸記号の手前で途切れて範囲内に収まる場合)を対象にする。
    鎖の途中に重なる別の線(壁線など)は鎖に入れない(消さない)。
    範囲をまたぐ実線は build() の範囲判定で消えるが、破線は1片ずつ範囲に収まってしまうため。"""
    x0, y0, x1, y1 = bbox
    dx, dy = segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1]
    L = np.hypot(dx, dy)
    ok = L > 1e-6
    th = np.mod(np.arctan2(dy, dx), np.pi)
    th = np.where(np.abs(th - np.pi) < 0.004, 0.0, th)
    nx, ny = -np.sin(th), np.cos(th)
    rho = nx * segs[:, 0] + ny * segs[:, 1]
    ux, uy = np.cos(th), np.sin(th)
    t1 = ux * segs[:, 0] + uy * segs[:, 1]
    t2 = ux * segs[:, 2] + uy * segs[:, 3]
    ta, tb = np.minimum(t1, t2), np.maximum(t1, t2)
    inside = ((segs[:, [0, 2]] >= x0).all(1) & (segs[:, [0, 2]] <= x1).all(1)
              & (segs[:, [1, 3]] >= y0).all(1) & (segs[:, [1, 3]] <= y1).all(1))
    drop = np.zeros(len(segs), bool)
    groups: dict[tuple[int, int], list[int]] = {}
    for i in np.where(ok)[0]:
        groups.setdefault((int(round(th[i] / 0.004)), int(round(rho[i] / 0.3))), []).append(i)
    for idx in groups.values():
        if len(idx) < min_pieces:
            continue
        idx = sorted(idx, key=lambda i: ta[i])
        chains = []
        chain, end, gaps = [idx[0]], tb[idx[0]], 0
        for i in idx[1:]:
            gap = ta[i] - end
            if gap < -gap_min:  # 鎖に重なる別の線(壁線など)は鎖に入れず読み飛ばす
                continue
            if gap <= gap_max:
                chain.append(i)
                gaps += gap > gap_min
            else:
                chains.append((chain, gaps))
                chain, gaps = [i], 0
            end = tb[i]
        chains.append((chain, gaps))
        for c, g in chains:
            if g < min_pieces:
                continue
            xs, ys = segs[c][:, [0, 2]], segs[c][:, [1, 3]]
            long_ = (xs.max() - xs.min() >= span * (x1 - x0)) or (ys.max() - ys.min() >= span * (y1 - y0))
            if long_ or (inside[c].any() and not inside[c].all()):
                drop[c] = True
    return drop


def hatch_lines(segs, min_len=12.0, min_rows=6, regular=0.6):
    """斜めのハッチ(床断熱範囲・勾配天井・下屋の斜線)を返す(bool マスク)。
    縦横以外の同じ角度の長い線が、ほぼ等間隔に min_rows 列以上並ぶものをハッチとみなす。
    同じ列(同じ直線上)の短い片も合わせて消す。"""
    dx, dy = segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1]
    L = np.hypot(dx, dy)
    th = np.mod(np.degrees(np.arctan2(dy, dx)), 180.0)
    off_axis = np.minimum(np.minimum(th, 180 - th), np.abs(th - 90)) > 2
    thr = np.radians(th)
    rho = -np.sin(thr) * segs[:, 0] + np.cos(thr) * segs[:, 1]
    drop = np.zeros(len(segs), bool)
    long_ = off_axis & (L >= min_len)
    for a in np.unique(np.round(th[long_])):
        grp = long_ & (np.abs(th - a) <= 1.0)
        rows = []  # 列(rho)を 2.5pt でまとめる
        for r in np.sort(rho[grp]):
            if not rows or r - rows[-1][-1] > 2.5:
                rows.append([r])
            else:
                rows[-1].append(r)
        if len(rows) < min_rows:
            continue
        centers = np.array([np.mean(r) for r in rows])
        d = np.diff(centers)
        step = np.median(d)
        if step < 3 or np.mean(np.abs(d - step) <= 0.25 * step) < regular:
            continue
        near = np.abs(rho[:, None] - centers[None, :]).min(axis=1) <= 1.5
        drop |= off_axis & (np.abs(th - a) <= 1.0) & near
    return drop


def point_seg_dist(px, py, segs):
    x1, y1, x2, y2 = segs[:, 0], segs[:, 1], segs[:, 2], segs[:, 3]
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    L2 = np.where(L2 == 0, 1e-9, L2)
    t = np.clip(((px[:, None] - x1) * dx + (py[:, None] - y1) * dy) / L2, 0, 1)
    qx, qy = x1 + t * dx, y1 + t * dy
    return np.hypot(px[:, None] - qx, py[:, None] - qy)


def remove_dimensions(segs, spans):
    """寸法値の文字(例 2,590)の近くにあって、長さが寸法値/縮尺と一致する平行線を寸法線として消す"""
    keep = np.ones(len(segs), bool)
    dx, dy = segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1]
    lens = np.hypot(dx, dy)
    mx, my = (segs[:, 0] + segs[:, 2]) / 2, (segs[:, 1] + segs[:, 3]) / 2
    n = 0
    for s in spans:
        if not re.fullmatch(r"\d{1,2},?\d{2,3}", s["text"]):
            continue
        v = float(s["text"].replace(",", ""))
        if v < 30:
            continue
        L = v / PT_MM
        tx, ty = (s["bbox"][0] + s["bbox"][2]) / 2, (s["bbox"][1] + s["bbox"][3]) / 2
        horiz = abs(s["dir"][0]) > 0.7
        par = (np.abs(dy) < 0.02 * lens) if horiz else (np.abs(dx) < 0.02 * lens)
        along = np.abs(mx - tx) if horiz else np.abs(my - ty)
        perp = np.abs(my - ty) if horiz else np.abs(mx - tx)
        cand = keep & par & (np.abs(lens - L) < max(0.8, 0.02 * L)) & (along < 0.2 * L + 3) & (perp < 9)
        if cand.any():
            i = np.where(cand)[0][np.argmin(perp[cand])]
            keep[i] = False
            n += 1
    print(f"  dimension lines removed: {n}")
    return segs[keep]


def prune_dangling(segs, iters=30):
    """端点が他のどの線にも触れていない線を反復して刈る(引出線・寸法補助線・中心線)"""
    keep = np.ones(len(segs), bool)
    lens = np.hypot(segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1])
    for _ in range(iters):
        idx = np.where(keep)[0]
        cur = segs[idx]
        removed = False
        for end in (0, 2):
            px, py = cur[:, end], cur[:, end + 1]
            free = np.ones(len(idx), bool)
            for c0 in range(0, len(idx), 400):
                d = point_seg_dist(px[c0:c0 + 400], py[c0:c0 + 400], cur)
                rows = np.arange(d.shape[0])
                d[rows, rows + c0] = np.inf  # 自分自身
                free[c0:c0 + 400] = d.min(axis=1) > DANGLE_TOL
            # 破線: 同じ直線上に DASH_GAP 以内で次の破片があれば繋がっているとみなす
            for ii in np.where(free)[0]:
                x1, y1, x2, y2 = cur[ii]
                L = math.hypot(x2 - x1, y2 - y1)
                if L == 0:
                    continue
                ux, uy = (x2 - x1) / L, (y2 - y1) / L
                ex, ey = cur[ii, end], cur[ii, end + 1]
                ox = cur[:, [0, 2]] - ex
                oy = cur[:, [1, 3]] - ey
                perp = np.abs(ox * uy - oy * ux).max(axis=1)
                gap = np.hypot(ox, oy).min(axis=1)
                olen = np.hypot(cur[:, 2] - cur[:, 0], cur[:, 3] - cur[:, 1])
                par = np.abs((cur[:, 2] - cur[:, 0]) * uy - (cur[:, 3] - cur[:, 1]) * ux) < 0.05 * np.maximum(olen, 1e-9)
                hit = (perp < 0.3) & (gap < DASH_GAP) & par
                hit[ii] = False
                if hit.any():
                    free[ii] = False
            kill = idx[free & (lens[idx] >= DANGLE_MINLEN)]
            if len(kill):
                keep[kill] = False
                removed = True
                idx = np.where(keep)[0]
                cur = segs[idx]
                break
        if not removed:
            break
    return keep


# ---------------------------------------------------------------- 室名
def parse_rooms(spans, bbox):
    x0, y0, x1, y1 = bbox
    inside = [s for s in spans if s["color"] == 0 and x0 <= (s["bbox"][0] + s["bbox"][2]) / 2 <= x1
              and y0 <= (s["bbox"][1] + s["bbox"][3]) / 2 <= y1]
    rooms = []
    for s in inside:
        name = re.sub(r"[A-Z]$", "", s["text"]) if s["text"].startswith("洋室") else s["text"]
        for pat, tmpl, ckey in ROOM_MAP:
            if re.search(pat, name):
                break
        else:
            continue
        # 直下の「(28.16㎡ 17帖)」を拾う
        cx = (s["bbox"][0] + s["bbox"][2]) / 2
        m2 = jo = None
        for t in inside:
            tcx = (t["bbox"][0] + t["bbox"][2]) / 2
            if abs(tcx - cx) < 20 and 0 <= t["bbox"][1] - s["bbox"][3] < 8:
                mm = re.search(r"([\d.]+)\s*(?:㎡|m2)", t["raw"])
                mj = re.search(r"([\d.]+)\s*帖", t["raw"])
                if mm:
                    m2 = float(mm.group(1))
                if mj:
                    jo = float(mj.group(1))
        rooms.append(dict(cad=s["text"], label=tmpl, ckey=ckey, bbox=s["bbox"], dir=s["dir"], m2=m2, jo=jo,
                          erase=tmpl is None))
    keep_text = [s for s in inside if s["text"] in KEEP_TEXT]
    return rooms, keep_text


# ---------------------------------------------------------------- SVG 出力（解析結果を読み取るだけ）
def svg_color(rgb):
    return "#" + "".join(f"{v:02x}" for v in rgb)


def svg_mask(mask, color, room=None):
    """外周と穴を1つの複合パスにまとめ、0.5px の許容誤差で簡略化する。"""
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_TREE,
                                   cv2.CHAIN_APPROX_SIMPLE)
    parts = []
    for contour in contours:
        points = cv2.approxPolyDP(contour, 0.5, True).reshape(-1, 2)
        if len(points):
            parts.append("M" + " L".join(f"{x},{y}" for x, y in points) + " Z")
    attr = f' data-room="{escape(room, quote=True)}"' if room is not None else ""
    return f'<path{attr} fill="{svg_color(color)}" fill-rule="evenodd" d="{" ".join(parts)}"/>'


def svg_text(x, y, text, font, anchor="mm", stroke=0, transform=""):
    """Pillow のアンカーと行間を SVG のベースライン座標へ変換する。"""
    lines = text.split("\n")
    step = font.getbbox("A", stroke_width=stroke)[3] + stroke
    top = y - (len(lines) - 1) * step / 2 if anchor[1] == "m" else y
    family = ('"Hiragino Mincho ProN", serif' if "ヒラギノ" in str(font.path)
              else '"Times New Roman", serif')
    attrs = f' transform="{escape(transform, quote=True)}"' if transform else ""
    result = []
    for i, line in enumerate(lines):
        if not line:
            continue
        baseline = top + i * step + font.getbbox(line, anchor=anchor)[1] - font.getbbox(line, anchor=anchor[0] + "s")[1]
        result.append(
            f'<text x="{x}" y="{baseline}" font-family="{escape(family, quote=True)}" '
            f'font-size="{font.size}" text-anchor="{dict(l="start", m="middle", r="end")[anchor[0]]}" '
            f'fill="{svg_color(LINE_RGB)}" stroke="white" stroke-width="{stroke * 2}" '
            f'stroke-linejoin="round" paint-order="stroke fill"{attrs}>{escape(line)}</text>')
    return "\n".join(result)


def write_svg(path, size, crop, rooms, region, wall, line, labels, floor, ang,
              offset=(0, 0), annotations=()):
    cx0, cy0, cx1, cy1 = crop
    w, h = cx1 - cx0, cy1 - cy0
    cut = np.s_[cy0:cy1, cx0:cx1]
    elements = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size[0]}" height="{size[1]}" '
        f'viewBox="0 0 {size[0]} {size[1]}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<g transform="translate({offset[0]} {offset[1]})">', '<g id="rooms">']
    for room in rooms:
        if id(room) in region and not room["erase"]:
            elements.append(svg_mask(region[id(room)][cut], COLORS[room["ckey"]], room["cad"]))
    elements.extend(['</g>', '<g id="walls">', svg_mask(wall[cut], WALL_RGB),
                     '</g>', '<g id="lines">'])
    rgba = np.zeros((h, w, 4), np.uint8)
    rgba[:, :, :3] = LINE_RGB
    rgba[:, :, 3] = line[cut].astype(np.uint8) * 255
    buffer = io.BytesIO()
    Image.fromarray(rgba).save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    elements.extend([f'<image width="{w}" height="{h}" href="data:image/png;base64,{encoded}"/>',
                     '</g>', '<g id="labels">',
                     f'<svg width="{w}" height="{h}" viewBox="{cx0} {cy0} {w} {h}" overflow="hidden">',
                     *labels, '</svg>'])
    # PNG と同じ座標・寸法の方位記号。
    cx, cy, r = w + 22 * S // 2, int(20 * S), int(7 * S)
    a = math.radians(ang)
    ux, uy = math.sin(a), -math.cos(a)
    px, py = -uy, ux
    tip = (cx + ux * r, cy + uy * r)
    tail = (cx - ux * r, cy - uy * r)
    left = (cx + px * r * 0.45 - ux * r * 0.2, cy + py * r * 0.45 - uy * r * 0.2)
    right = (cx - px * r * 0.45 - ux * r * 0.2, cy - py * r * 0.45 - uy * r * 0.2)
    color, stroke = svg_color(LINE_RGB), max(2, r // 14)
    elements.extend([
        f'<circle cx="{cx}" cy="{cy}" r="{r * 1.15}" fill="none" stroke="{color}" stroke-width="{stroke}"/>',
        f'<polygon points="{" ".join(f"{x},{y}" for x, y in (tip, left, (cx, cy), right))}" fill="{color}"/>',
        f'<path d="M{tail[0]},{tail[1]} L{cx},{cy}" fill="none" stroke="{color}" stroke-width="{stroke}"/>',
        svg_text(cx + ux * r * 1.6, cy + uy * r * 1.6, "N", ImageFont.truetype(FONT_EN, int(r * 0.7))),
        svg_text(w - 2 * S, h + S, floor, ImageFont.truetype(FONT_EN, int(16 * S)), anchor="ra"),
        '</g>', '</g>'])
    if annotations:
        elements.extend(['<g id="annotations">', *annotations, '</g>'])
    elements.append('</svg>')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(elements) + "\n", encoding="utf-8")
    print(f"  -> {path} {size}")


# ---------------------------------------------------------------- 本体
def _room_masks(rooms, region, crop):
    """注釈用に、同名の部屋領域を出力画像座標の外接矩形へまとめる。"""
    cx0, cy0, cx1, cy1 = crop
    found = {}
    for room in rooms:
        if id(room) not in region:
            continue
        ys, xs = np.nonzero(region[id(room)])
        if not len(xs):
            continue
        box = (int(xs.min() - cx0), int(ys.min() - cy0), int(xs.max() - cx0), int(ys.max() - cy0))
        found.setdefault(room["cad"], []).append((box, region[id(room)][cy0:cy1, cx0:cx1]))
    return found


def _annotation_plan(path, room_masks, building_size):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    valid, sides = [], {"left": 0, "right": 0, "top": 0, "bottom": 0}
    for item in data.get("items", []):
        typ = item.get("type")
        room = item.get("room")
        if room and room not in room_masks:
            print(f"warning: annotation room not found: {room}", file=sys.stderr)
            continue
        if typ == "comment":
            side = item.get("side", "bottom")
            if side not in sides:
                print(f"warning: invalid annotation side: {side}", file=sys.stderr); continue
            sides[side] = max(sides[side], 150 * S)
        elif typ == "car" and item.get("outside"):
            side = item["outside"]
            if side not in sides:
                print(f"warning: invalid annotation side: {side}", file=sys.stderr); continue
            # 車の短辺に少し余白を足す。
            sides[side] = max(sides[side], int(2200 * S / PT_MM))
        elif typ not in ("car", "comment"):
            print(f"warning: invalid annotation type: {typ}", file=sys.stderr); continue
        valid.append(item)
    return valid, sides


def _draw_car(draw, center, size, vertical=True):
    """上から見た車を PIL で描き、同じ形の SVG 要素を返す。"""
    long_, short = size
    w, h = (short, long_) if vertical else (long_, short)
    x0, y0 = center[0] - w / 2, center[1] - h / 2
    x1, y1 = center[0] + w / 2, center[1] + h / 2
    sw, rad = max(2, int(1.5 * S)), int(min(w, h) * .18)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=rad, fill="white", outline=LINE_RGB, width=sw)
    if vertical:
        glass = [(x0+w*.18, y0+h*.20, x1-w*.18, y0+h*.38), (x0+w*.18, y0+h*.64, x1-w*.18, y0+h*.82)]
        mirrors = [(x0-w*.10,y0+h*.32,x0,y0+h*.43),(x1,y0+h*.32,x1+w*.10,y0+h*.43)]
    else:
        glass = [(x0+w*.20, y0+h*.18, x0+w*.38, y1-h*.18), (x0+w*.64, y0+h*.18, x0+w*.82, y1-h*.18)]
        mirrors = [(x0+w*.32,y0-h*.10,x0+w*.43,y0),(x0+w*.32,y1,x0+w*.43,y1+h*.10)]
    for b in glass: draw.rounded_rectangle(b, radius=max(2, rad//3), fill=(235,240,242), outline=LINE_RGB, width=sw)
    for b in mirrors: draw.ellipse(b, fill="white", outline=LINE_RGB, width=sw)
    color = svg_color(LINE_RGB)
    svg = [f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rad}" fill="white" stroke="{color}" stroke-width="{sw}"/>']
    for b in glass:
        svg.append(f'<rect x="{b[0]:.1f}" y="{b[1]:.1f}" width="{b[2]-b[0]:.1f}" height="{b[3]-b[1]:.1f}" rx="{max(2,rad//3)}" fill="#ebf0f2" stroke="{color}" stroke-width="{sw}"/>')
    for b in mirrors:
        svg.append(f'<ellipse cx="{(b[0]+b[2])/2:.1f}" cy="{(b[1]+b[3])/2:.1f}" rx="{(b[2]-b[0])/2:.1f}" ry="{(b[3]-b[1])/2:.1f}" fill="white" stroke="{color}" stroke-width="{sw}"/>')
    return "\n".join(svg)


def _comment_origin(mask, box, at, side, avoid):
    """全マスク画素から、文字と side 方向の直線が干渉しない最寄り点を選ぶ。"""
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    x0, y0, x1, y1 = box
    target = (xs.mean(), ys.mean()) if at is None else (
        x0 + (x1-x0)*float(at[0]), y0 + (y1-y0)*float(at[1]))
    good = np.ones(len(xs), bool)
    # 黒丸の半径も含めて避ける。avoid 自体は文字外接矩形 + 8px。
    for left, top, right, bottom in avoid:
        left, top, right, bottom = left-3, top-3, right+3, bottom+3
        if side == "bottom":
            hit = (xs >= left) & (xs <= right) & (ys <= bottom)
        elif side == "top":
            hit = (xs >= left) & (xs <= right) & (ys >= top)
        elif side == "left":
            hit = (ys >= top) & (ys <= bottom) & (xs >= left)
        else:
            hit = (ys >= top) & (ys <= bottom) & (xs <= right)
        good &= ~hit
    if not good.any():
        return None
    xs, ys = xs[good], ys[good]
    index = np.argmin((xs-target[0])**2 + (ys-target[1])**2)
    return float(xs[index]), float(ys[index])


def draw_annotations(base, annot_path, rooms, region, crop, label_boxes, building_box):
    masks = _room_masks(rooms, region, crop)
    items, _ = _annotation_plan(annot_path, masks, base.size)
    left, top, right, bottom = building_box
    font = ImageFont.truetype("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc", 11*S)
    measure = ImageDraw.Draw(base)
    avoid = [(a-8, b-8, c+8, d+8) for a,b,c,d in label_boxes]
    cars, comments = [], []
    bounds = [0., 0., float(base.width), float(base.height)]

    def include(box):
        bounds[0] = min(bounds[0], box[0]-24)
        bounds[1] = min(bounds[1], box[1]-24)
        bounds[2] = max(bounds[2], box[2]+24)
        bounds[3] = max(bounds[3], box[3]+24)

    for item in items:
        room = item.get("room")
        if room:
            box, mask = max(masks[room], key=lambda v: int(v[1].sum()))
            x0, y0, x1, y1 = box
        if item["type"] == "car":
            length, width = int(4700*S/PT_MM), int(1800*S/PT_MM)
            if room:
                rw, rh = max(1, x1-x0), max(1, y1-y0)
                vertical = rh >= rw
                scale = min(1., (rh if vertical else rw)*.86/length,
                            (rw if vertical else rh)*.86/width)
                length, width = int(length*scale), int(width*scale)
                center = ((x0+x1)/2, (y0+y1)/2)
            else:
                side = item["outside"]
                vertical = side in ("left", "right")
                gap = 500*S/PT_MM
                # ミラーの外端から建物外接矩形までを実寸500mmにする。
                centers = {
                    "left": (left-gap-width*.6, (top+bottom)/2),
                    "right": (right+gap+width*.6, (top+bottom)/2),
                    "top": ((left+right)/2, top-gap-width*.6),
                    "bottom": ((left+right)/2, bottom+gap+width*.6)}
                center = centers[side]
            half_w, half_h = (width*.6, length/2) if vertical else (length/2, width*.6)
            include((center[0]-half_w, center[1]-half_h, center[0]+half_w, center[1]+half_h))
            cars.append((center, (length, width), vertical))
            continue
        side = item.get("side", "bottom")
        origin = _comment_origin(mask, box, item.get("at"), side, avoid)
        if origin is None:
            print(f"warning: no clear annotation leader for room: {room}", file=sys.stderr)
            continue
        lines = item.get("text", "").split("\n")[:2]
        # 各行の実際のインク外接矩形を基準にPNG/SVG共通の座標を作る。
        metrics = [measure.textbbox((0, 0), line, font=font, anchor="ls") for line in lines]
        width = max(b[2]-b[0] for b in metrics)
        step = font.size + S
        height = max(i*step+b[3]-b[1] for i,b in enumerate(metrics))
        px, py = origin
        if side == "bottom":
            tx, ty = px-width/2, bottom+60
        elif side == "top":
            tx, ty = px-width/2, top-60-height
        elif side == "left":
            tx, ty = left-60-width, py-height/2
        else:
            tx, ty = right+60, py-height/2
        comments.append(dict(side=side, origin=origin, lines=lines, metrics=metrics,
                             width=width, height=height, tx=tx, ty=ty, step=step))

    # 同じ辺は起点順に配置。衝突時だけ文字を接線方向にずらす。
    for side in ("left", "right", "top", "bottom"):
        horizontal = side in ("top", "bottom")
        coordinate, extent = ("tx", "width") if horizontal else ("ty", "height")
        previous = -math.inf
        for comment in sorted((c for c in comments if c["side"] == side),
                              key=lambda c: c[coordinate]):
            comment[coordinate] = max(comment[coordinate], previous+24)
            previous = comment[coordinate]+comment[extent]
            include((comment["tx"], comment["ty"],
                     comment["tx"]+comment["width"], comment["ty"]+comment["height"]))

    ox, oy = -math.floor(bounds[0]), -math.floor(bounds[1])
    result = Image.new("RGB", (math.ceil(bounds[2])+ox, math.ceil(bounds[3])+oy), "white")
    result.paste(base, (ox, oy))
    draw = ImageDraw.Draw(result)
    elements = []
    color, sw = svg_color(LINE_RGB), max(2, S)
    for center, size, vertical in cars:
        body = _draw_car(draw, (center[0]+ox, center[1]+oy), size, vertical)
        elements.append(f'<g data-annot="car">{body}</g>')
    for c in comments:
        px, py = c["origin"]
        tx, ty, width, height = c["tx"], c["ty"], c["width"], c["height"]
        side = c["side"]
        if side == "bottom":
            end, target = (px, bottom+36), (tx+width/2, ty-8)
        elif side == "top":
            end, target = (px, top-36), (tx+width/2, ty+height+8)
        elif side == "left":
            end, target = (left-36, py), (tx+width+8, ty+height/2)
        else:
            end, target = (right+36, py), (tx-8, ty+height/2)
        horizontal = side in ("top", "bottom")
        shifted = abs((target[0]-px) if horizontal else (target[1]-py)) > .01
        points = [(px, py), end, (target[0], end[1]), target] if shifted else [(px, py), target]
        points = [(x+ox, y+oy) for x,y in points]
        draw.line(points, fill=LINE_RGB, width=sw)
        dr = 2.5 * S  # 黒丸の半径（完成版の見た目に合わせ、解像度 S に比例させる）
        draw.ellipse((px+ox-dr, py+oy-dr, px+ox+dr, py+oy+dr), fill=LINE_RGB)
        svg = [f'<circle cx="{px+ox}" cy="{py+oy}" r="{dr}" fill="{color}"/>']
        for a,b in zip(points, points[1:]):
            svg.append(f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" stroke="{color}" stroke-width="{sw}"/>')
        for i,(line, metric) in enumerate(zip(c["lines"], c["metrics"])):
            x = tx+(width-(metric[2]-metric[0]))/2-metric[0]+ox
            y = ty+i*c["step"]-metric[1]+oy
            draw.text((x,y), line, font=font, fill=LINE_RGB, anchor="ls")
            svg.append(f'<text x="{x}" y="{y}" font-family="Hiragino Sans, Hiragino Kaku Gothic ProN, sans-serif" font-size="{font.size}" fill="{color}">{escape(line)}</text>')
        elements.append('<g data-annot="comment">'+"".join(svg)+'</g>')
    return result, (ox, oy), elements


def build(pdf, pno, floor, out, debug_dir=None, svg=None, annot=None):
    rect, segs, widths, spans = read_page(pdf, pno)
    names, _ = parse_rooms(spans, (rect.x0, rect.y0, rect.x1, rect.y1))
    anchors = [((r["bbox"][0] + r["bbox"][2]) / 2, (r["bbox"][1] + r["bbox"][3]) / 2) for r in names]
    bx0, by0, bx1, by1 = building_bbox(rect, segs, widths, anchors)
    m = 6
    bx0, by0, bx1, by1 = bx0 - m, by0 - m, bx1 + m, by1 + m

    # 建物範囲に収まる線だけ残す(範囲をまたぐ線 = 寸法補助線・引出線は捨てる)
    inb = ((segs[:, [0, 2]] >= bx0).all(1) & (segs[:, [0, 2]] <= bx1).all(1)
           & (segs[:, [1, 3]] >= by0).all(1) & (segs[:, [1, 3]] <= by1).all(1))
    dashed = dashed_crossing(segs, (bx0, by0, bx1, by1))
    print(f"  dashed lines crossing the bbox (grid lines): {int((dashed & inb).sum())} pieces")
    segs = segs[inb & ~dashed]
    hatch = hatch_lines(segs)
    print(f"  hatch lines removed: {int(hatch.sum())}")
    segs = segs[~hatch]
    segs = remove_dimensions(segs, spans)
    keep = prune_dangling(segs)
    segs_k = segs[keep]
    print(f"[p{pno}] lines in bbox {len(segs)} -> after prune {len(segs_k)}")

    W, H = int((bx1 - bx0) * S), int((by1 - by0) * S)
    ink = raster_segments(segs_k, (H, W), (bx0, by0), S, thick=2)

    # 小さな孤立成分のうち、細長いもの(中心線の残り)と数字文字に重なるもの(室内寸法)を消す
    n, lab, st, _ = cv2.connectedComponentsWithStats((cv2.dilate(ink, np.ones((3, 3))) > 0).astype(np.uint8))
    main = np.argmax(st[1:, 4]) + 1
    num_boxes = [s["bbox"] for s in spans if re.fullmatch(r"[\d,]+", s["text"])]
    for i in range(1, n):
        if i == main:
            continue
        x, y, w, h, a = st[i]
        thin = min(w, h) < 3 * S and 8 * S <= max(w, h) < 60 * S  # 8pt 未満は破線の1片として残す
        px0, py0 = bx0 + x / S, by0 + y / S
        px1, py1 = px0 + w / S, py0 + h / S
        near_num = any(not (b[2] < px0 - 4 or b[0] > px1 + 4 or b[3] < py0 - 4 or b[1] > py1 + 4) for b in num_boxes)
        dot = max(w, h) < 3.0 * S  # 中心線の端の小円・点(膨張後の寸法)
        if thin or near_num or dot:
            ink[lab == i] = 0

    # 塗り潰し記号(黒三角など)を消す: 線幅より太い部分だけが開閉処理で残る
    solid = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
    ink[cv2.dilate(solid, np.ones((5, 5), np.uint8)) > 0] = 0

    rooms, keep_text = parse_rooms(spans, (bx0, by0, bx1, by1))
    px_per_m2 = (1000 / (PT_MM / S)) ** 2

    def to_px(x, y):
        return int((x - bx0) * S), int((y - by0) * S)

    def fill_from(barrier, seed):
        """seed を含む空き領域のマスク。seed が線の上なら近くの空き画素から塗る"""
        free = (barrier == 0).astype(np.uint8)
        n_, lab_ = cv2.connectedComponents(free, connectivity=4)
        sx, sy = seed
        # 種が線の上なら近くの空き画素を探す
        best = None
        for r in range(0, 12 * S, 2):
            ys, xs = np.mgrid[max(0, sy - r):min(H, sy + r + 1), max(0, sx - r):min(W, sx + r + 1)]
            ok = free[ys, xs] > 0
            if ok.any():
                d = (xs[ok] - sx) ** 2 + (ys[ok] - sy) ** 2
                k = np.argmin(d)
                best = (xs[ok][k], ys[ok][k])
                break
        if best is None:
            return None
        return lab_ == lab_[best[1], best[0]]

    # 外部: 余白を付けた端から塗る
    seeds = {}
    for r in rooms:
        b = r["bbox"]
        seeds[id(r)] = to_px((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)

    region = {}
    base_barrier = ink.copy()
    label_cache = {}

    def labels_for(k):
        if k not in label_cache:
            bar = base_barrier if k == 0 else cv2.dilate(base_barrier, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
            label_cache[k] = (bar, cv2.connectedComponents((bar == 0).astype(np.uint8), connectivity=4)[1])
        return label_cache[k]

    for r in rooms:
        exp = None
        if r["m2"]:
            exp = r["m2"] * px_per_m2
        elif r["jo"]:
            exp = r["jo"] * 1.62 * px_per_m2
        others = [seeds[id(o)] for o in rooms if o is not r and not o["erase"]]
        b = r["bbox"]
        tw, th = b[2] - b[0], b[3] - b[1]
        cxp, cyp = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        # 室名の中心だけでなく周囲も種にする(文字が便器・浴槽などの図形の中に掛かっている場合)
        cand_seeds = [to_px(cxp + dx, cyp + dy) for dx, dy in
                      ((0, 0), (0, -1.3 * min(tw, th)), (0, 1.3 * min(tw, th)), (-1.3 * min(tw, th), 0), (1.3 * min(tw, th), 0))]
        def search(cand_seeds):
            """壁を k px 太らせながら、種から塗った閉領域のうち漏れていない最大のものを探す"""
            chosen = fallback = None
            for k in (0, 3, 5, 7, 9, 13, 17):  # 壁を k px 太らせて隙間を塞ぐ
                bar, lab_k = labels_for(k)
                valid = []
                for sx, sy in cand_seeds:
                    if not (0 <= sx < W and 0 <= sy < H) or bar[sy, sx]:
                        continue
                    msk = lab_k == lab_k[sy, sx]
                    area = msk.sum()
                    touches_border = msk[0].any() or msk[-1].any() or msk[:, 0].any() or msk[:, -1].any()
                    leak_other = any(msk[y, x] for x, y in others if 0 <= y < H and 0 <= x < W)
                    too_big = exp is not None and area > exp * 1.25
                    if not (touches_border or leak_other or too_big):
                        valid.append((area, msk))
                if valid:
                    area, msk = max(valid, key=lambda v: v[0])
                    if k:  # 太らせた分を戻す(元の線の内側まで広げる)
                        grown = cv2.dilate(msk.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k + 2, k + 2)))
                        msk = (grown > 0) & (base_barrier == 0)
                    if msk.sum() < MIN_ROOM_M2 * px_per_m2:
                        # 部屋として小さすぎる = 室名が便器・浴槽などの図形の中に掛かっている。
                        # 部屋そのものは扉の隙間で漏れているので、壁を太らせて探し続ける
                        fallback = fallback or (k, msk)
                        continue
                    chosen = (k, msk)
                    break
            return chosen, fallback

        chosen, fallback = search(cand_seeds)
        if chosen is None and fallback is not None:
            # 小さな領域しか取れない(文字が大きな図形の中にある)ときは、さらに離れた8方向を種にする
            h_ = min(tw, th)
            far = [to_px(cxp + f * h_ * ux, cyp + f * h_ * uy) for f in (2.5, 4.0)
                   for ux, uy in ((0, -1), (0, 1), (-1, 0), (1, 0), (-.7, -.7), (.7, -.7), (-.7, .7), (.7, .7))]
            chosen = search(far)[0]
        chosen = chosen or fallback
        if chosen is None:
            print(f"  ! {r['cad']}: 閉じた領域を作れず(漏れ)")
            continue
        k, msk = chosen
        a_m2 = msk.sum() / px_per_m2
        print(f"  {r['cad']:<8} dilate={k:<2} area={a_m2:6.2f}m2" + (f" (図面 {r['m2']}m2)" if r["m2"] else ""))
        region[id(r)] = msk

    # 下屋・庇: 自分の領域と外部にしか接していない線を消す
    ext = fill_from(ink, (1, 1))
    for r in rooms:
        if r["erase"] and id(r) in region:
            zone = cv2.dilate((region[id(r)] | ext).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
            others = np.zeros((H, W), bool)
            for o in rooms:
                if o is not r and id(o) in region:
                    others |= region[id(o)]
            others = cv2.dilate(others.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
            ink[zone & ~others & (ink > 0)] = 0
            # 建物の外壁線(下屋と壁の中空部の間)は残る
    ext = fill_from(ink, (1, 1))

    # 壁: 部屋でも外部でもない閉領域を分類する
    #   細い(1.6〜9pt)     → 壁の候補
    #   極細で小さい       → 柱の×印の三角など → 壁の候補
    #   極細で長い         → サッシの隙間 → 白
    #   太い               → 名前の無い空間(ホール・出窓の三角) → 白
    # 壁の候補のうち、室内(バルコニー以外の部屋・名前の無い空間)に接するものから始めて、
    # 隣り合う候補へ広げる。バルコニーにしか接しない候補(手すり壁)は白のまま。
    claimed = ext.copy()
    for msk in region.values():
        claimed |= msk
    rest = ((ink == 0) & ~claimed).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(rest, connectivity=4)
    dist = cv2.distanceTransform(rest, cv2.DIST_L2, 3)
    kind = np.zeros(n, np.uint8)  # 1 壁候補 2 サッシ隙間 3 空間 4 設備の輪(浴槽・シンクの縁) 5 窓
    for i in range(1, n):
        x, y, w, h, a = st[i]
        sub = lab[y:y + h, x:x + w] == i
        thick = dist[y:y + h, x:x + w][sub].max() * 2 / S  # pt
        fill = a / float(w * h)
        if thick > 9:
            kind[i] = 3
        elif thick >= 1.6:
            kind[i] = 1 if (fill > 0.55 or max(w, h) < 12 * S) else 4
        elif max(w, h) < 12 * S:
            kind[i] = 1
        else:
            kind[i] = 2
    # 窓: 長い辺に沿ってサッシの隙間(kind 2)が並んでいる壁候補
    near_ext = cv2.dilate(ext.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41))) > 0
    near_sash = cv2.dilate(np.isin(lab, np.where(kind == 2)[0]).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    for i in np.where(kind == 1)[0]:
        x, y, w, h, a = st[i]
        if max(w, h) < 12 * S:
            continue
        sub = lab[y:y + h, x:x + w] == i
        if (near_sash[y:y + h, x:x + w] & sub).sum() >= 0.6 * max(w, h) * 2 and (near_ext[y:y + h, x:x + w] & sub).any():
            kind[i] = 5
    interior = np.zeros((H, W), bool)
    balcony = np.zeros((H, W), bool)
    for r in rooms:
        if id(r) in region and not r["erase"]:
            (balcony if r["cad"] == "バルコニー" else interior)[:] |= region[id(r)]
    k7 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13))  # 石膏ボード線などで線が二重になる分を跨ぐ
    # バルコニーの中で破線などに区切られた空間はバルコニーに含める
    near_rooms = cv2.dilate(interior.astype(np.uint8), k7) > 0
    for _ in range(3):
        near_bal = cv2.dilate(balcony.astype(np.uint8), k7) > 0
        for i in np.where(kind == 3)[0]:
            m_ = lab == i
            if near_bal[m_].any() and not near_rooms[m_].any():
                balcony |= m_
                kind[i] = 0
    # 1つの水回りの部屋に囲まれた設備(浴槽など)はその部屋の色で塗る
    for r in rooms:
        if id(r) in region and r["ckey"] == "water":
            rm = cv2.dilate(region[id(r)].astype(np.uint8), k7) > 0
            for i in np.where((kind == 3) | (kind == 4))[0]:
                x, y, w, h, a = st[i]
                m_ = lab[y:y + h, x:x + w] == i
                ring = cv2.dilate(m_.astype(np.uint8), k7) > 0
                ring &= ~m_
                if rm[y:y + h, x:x + w][ring].mean() > 0.6:
                    region[id(r)][y:y + h, x:x + w] |= m_
                    kind[i] = 0
    interior |= np.isin(lab, np.where(kind == 3)[0])
    near_int = cv2.dilate(interior.astype(np.uint8), k7) > 0
    near_bal = cv2.dilate(balcony.astype(np.uint8), k7) > 0
    cand = np.where(kind == 1)[0]
    accepted = set(int(i) for i in cand if near_int[lab == i].any())
    changed = True
    while changed:
        changed = False
        acc_mask = cv2.dilate(np.isin(lab, list(accepted)).astype(np.uint8), k7) > 0
        for i in cand:
            if int(i) in accepted:
                continue
            m_ = lab == i
            if acc_mask[m_].any() and not near_bal[m_].any():
                accepted.add(int(i))
                changed = True
    wall = np.isin(lab, list(accepted))
    print(f"  rest comps {n-1}: kinds", np.bincount(kind, minlength=4).tolist(), "accepted", len(accepted))
    if debug_dir:
        dbg = np.dstack([ink] * 3).copy()
        dbg[np.isin(lab, cand)] = (0, 0, 255)
        dbg[wall] = (0, 180, 0)
        dbg[np.isin(lab, np.where(kind == 3)[0])] = (255, 200, 0)
        dbg[np.isin(lab, np.where(kind == 2)[0])] = (255, 0, 255)
        dbg[interior & ~np.isin(lab, np.where(kind == 3)[0])] = (200, 200, 255)
        cv2.imwrite(str(Path(debug_dir) / f"walls_{floor}.png"), cv2.resize(dbg, None, fx=0.6, fy=0.6, interpolation=cv2.INTER_AREA))

    # 建物の外で、建物に接していない線(引出線・メーター・下屋の輪郭)を消す
    building = interior | balcony | wall | np.isin(lab, np.where((kind == 2) | (kind == 4) | (kind == 5))[0])
    for msk in region.values():
        building |= msk
    near_b = cv2.dilate(building.astype(np.uint8), k7) > 0
    ink[(ink > 0) & ~near_b] = 0
    # 壁の中の線(柱の×印・間柱)はグレーに: 近傍に壁以外の空間が無い線
    open_space = (building & ~wall) | ext | ((rest > 0) & ~building)
    near_open = cv2.dilate(open_space.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    inwall = (ink > 0) & ~near_open & (cv2.dilate(wall.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0)
    wall |= inwall
    ink[inwall] = 0
    ext = fill_from(ink, (1, 1))

    # 部屋の中に浮いている破線の図形(家具・天井範囲・エアコン位置などの想定線)を消す。
    # 破線の1片程度の大きさの線の成分を、破線の隙間程度の距離でまとめて1つの図形とし、
    # 図形の全ての片が「周囲がほぼ1つの部屋の塗り領域だけ」で、図形が他の線から離れているときだけ図形ごと消す
    # (一部だけ欠けた図形を残さない)
    label_region = {k: v.copy() for k, v in region.items()}  # 室名の配置はこの処理の前の領域で決める
    n, lab, st, _ = cv2.connectedComponentsWithStats((ink > 0).astype(np.uint8), connectivity=8)
    ring_k = np.ones((2 * FLOAT_RING + 1, 2 * FLOAT_RING + 1), np.uint8)
    room_id = np.zeros((H, W), np.int32)
    room_list = [r for r in rooms if id(r) in region and not r["erase"]]
    for j, r in enumerate(room_list, 1):
        room_id[region[id(r)]] = j
    small = np.zeros(n, bool)
    small[1:] = np.maximum(st[1:, 2], st[1:, 3]) <= FLOAT_MAXLEN * S
    float_room = np.zeros(n, np.int32)  # 0 = 浮いていない
    for i in np.where(small)[0]:
        x, y, w, h, a = st[i]
        x0_, y0_ = max(0, x - FLOAT_RING - 1), max(0, y - FLOAT_RING - 1)
        x1_, y1_ = min(W, x + w + FLOAT_RING + 1), min(H, y + h + FLOAT_RING + 1)
        comp = lab[y0_:y1_, x0_:x1_] == i
        ring = (cv2.dilate(comp.astype(np.uint8), ring_k) > 0) & ~comp
        ids = room_id[y0_:y1_, x0_:x1_][ring]
        if len(ids):
            j = np.bincount(ids).argmax()
            if j and (ids == j).mean() >= 0.97:
                float_room[i] = j
    gk = int(DASH_GAP * S) * 2 + 1
    groups = cv2.connectedComponents((cv2.dilate(np.isin(lab, np.where(small)[0]).astype(np.uint8),
                                                 np.ones((gk, gk), np.uint8)) > 0).astype(np.uint8))[1]
    members: dict[int, list[int]] = {}
    for i in np.where(small)[0]:
        x, y, w, h, a = st[i]
        yy, xx = np.nonzero(lab[y:y + h, x:x + w] == i)
        members.setdefault(int(groups[y + yy[0], x + xx[0]]), []).append(int(i))
    n_float = 0
    for g, idx in members.items():
        js = {int(float_room[i]) for i in idx}
        if 0 in js or len(js) != 1:
            continue
        # 図形が他の線(壁・棚板・設備)に破線の隙間程度まで近づいていれば、その線の一部とみなして残す
        xs_ = [st[i][0] for i in idx]; ys_ = [st[i][1] for i in idx]
        xe_ = [st[i][0] + st[i][2] for i in idx]; ye_ = [st[i][1] + st[i][3] for i in idx]
        x0_, y0_ = max(0, min(xs_) - gk), max(0, min(ys_) - gk)
        x1_, y1_ = min(W, max(xe_) + gk), min(H, max(ye_) + gk)
        sub = lab[y0_:y1_, x0_:x1_]
        mine = np.isin(sub, idx)
        near = cv2.dilate(mine.astype(np.uint8), np.ones((gk, gk), np.uint8)) > 0
        if ((sub > 0) & ~mine & near).any():
            continue
        j = js.pop()
        for i in idx:
            x, y, w, h, a = st[i]
            comp = lab[y:y + h, x:x + w] == i
            ink[y:y + h, x:x + w][comp] = 0
            x0_, y0_ = max(0, x - 1), max(0, y - 1)
            grown = cv2.dilate((lab[y0_:y + h + 1, x0_:x + w + 1] == i).astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
            region[id(room_list[j - 1])][y0_:y + h + 1, x0_:x + w + 1] |= grown
        n_float += 1
    print(f"  floating dashed figures removed: {n_float}")

    # ---------------------------------------------------------------- 描画
    canvas = np.full((H, W, 3), 255, np.uint8)
    for r in rooms:
        if id(r) in region and not r["erase"]:
            canvas[region[id(r)]] = COLORS[r["ckey"]]
    canvas[wall] = WALL_RGB
    line = cv2.dilate(ink, np.ones((3, 3), np.uint8)) > 0
    canvas[line] = LINE_RGB

    img = Image.fromarray(canvas)
    drw = ImageDraw.Draw(img)
    svg_labels = []
    label_boxes = []
    base_fs = int(28 * S)  # 主要室名の文字高さの基準(pt)
    for r in rooms:
        if r["erase"] or not r["label"] or id(r) not in region:
            continue
        text = r["label"].format(j=f"{r['jo']:.1f}J" if r["jo"] else "").strip()
        msk = label_region[id(r)]
        ys, xs = np.nonzero(msk)
        rw, rh = xs.max() - xs.min(), ys.max() - ys.min()
        vertical = rh > rw * 2.2 and "\n" not in text
        fs = base_fs if r["ckey"] in ("ldk", "room") else int(base_fs * 0.7)
        # 部屋の内接に近い位置: 距離変換の最大点
        dt = cv2.distanceTransform(msk.astype(np.uint8), cv2.DIST_L2, 5)
        cy, cx = np.unravel_index(np.argmax(dt), dt.shape)
        my_, mx_ = int(ys.mean()), int(xs.mean())
        if dt[my_, mx_] >= 0.7 * dt.max():  # 重心が十分内側なら重心に置く
            cy, cx = my_, mx_
        inner = dt.max() * 2
        while fs > 6 * S:  # 6pt 未満には縮めない(はみ出しても読める方を優先)
            font = ImageFont.truetype(FONT_JA if re.search(r"[^\x00-\x7f]", text) else FONT_EN, fs)
            tb = drw.multiline_textbbox((0, 0), text, font=font, align="center", spacing=0)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            if vertical:
                tw, th = th, tw
            if tw <= min(rw, inner * 1.6) * 0.92 and th <= min(rh, inner * 1.6) * 0.92:
                break
            fs = int(fs * 0.9)
        if vertical:
            tmp = Image.new("RGBA", (tb[2] + 8, tb[3] + 8), (0, 0, 0, 0))
            ImageDraw.Draw(tmp).text((4, 4), text, font=font, fill=LINE_RGB + (255,),
                                     stroke_width=max(2, fs // 14), stroke_fill=(255, 255, 255, 255))
            if svg:
                # Pillow の90度回転後の貼付位置をそのまま SVG の変換行列にする。
                tx = int(cx - tmp.height / 2)
                ty = int(cy - tmp.width / 2) + tmp.width
                svg_labels.append(svg_text(4, 4, text, font, anchor="la",
                                           stroke=max(2, fs // 14),
                                           transform=f"matrix(0 -1 1 0 {tx} {ty})"))
            tmp = tmp.rotate(90, expand=True)
            img.paste(tmp, (int(cx - tmp.width / 2), int(cy - tmp.height / 2)), tmp)
            if annot:
                a, b, c, d = tmp.getbbox()
                label_boxes.append((int(cx-tmp.width/2)+a, int(cy-tmp.height/2)+b,
                                    int(cx-tmp.width/2)+c, int(cy-tmp.height/2)+d))
        else:
            if annot:
                label_boxes.append(drw.multiline_textbbox(
                    (cx, cy), text, font=font, anchor="mm", align="center", spacing=0,
                    stroke_width=max(2, fs//14)))
            if svg:
                svg_labels.append(svg_text(cx, cy, text, font, stroke=max(2, fs // 14)))
            drw.multiline_text((cx, cy), text, font=font, fill=LINE_RGB, anchor="mm", align="center", spacing=0,
                               stroke_width=max(2, fs // 14), stroke_fill=(255, 255, 255))
    small = ImageFont.truetype(FONT_EN, int(9 * S))
    small_ja = ImageFont.truetype(FONT_JA, int(12 * S))
    for t in keep_text:
        b = t["bbox"]
        x, y = to_px((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        if svg:
            svg_labels.append(svg_text(x, y, t["text"],
                                       small if t["text"].isascii() else small_ja, stroke=S))
        if annot:
            label_boxes.append(drw.textbbox((x, y), t["text"],
                               font=small if t["text"].isascii() else small_ja,
                               anchor="mm", stroke_width=S))
        drw.text((x, y), t["text"], font=small if t["text"].isascii() else small_ja, fill=LINE_RGB, anchor="mm",
                 stroke_width=S, stroke_fill=(255, 255, 255))

    # 建物外形で切り抜き(外部以外の画素の外接矩形 + 余白)
    nz = np.nonzero(~ext)
    pad = 6 * S
    cx0, cy0 = max(0, nz[1].min() - pad), max(0, nz[0].min() - pad)
    cx1, cy1 = min(W, nz[1].max() + pad), min(H, nz[0].max() + pad)
    img = img.crop((cx0, cy0, cx1, cy1))

    # 余白を足して方位記号と階表示
    ang = north_angle(segs_all=read_page(pdf, pno)[1], spans=spans)
    margin = 22 * S
    full = Image.new("RGB", (img.width + margin, img.height + int(18 * S)), "white")
    full.paste(img, (0, 0))
    d2 = ImageDraw.Draw(full)
    draw_north(d2, (img.width + margin // 2, int(20 * S)), int(7 * S), ang)
    d2.text((img.width - 2 * S, img.height + 1 * S), floor, font=ImageFont.truetype(FONT_EN, int(16 * S)),
            fill=LINE_RGB, anchor="ra")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    svg_offset, svg_annotations = (0, 0), []
    if annot:
        label_boxes = [(a-cx0, b-cy0, c-cx0, d-cy0) for a,b,c,d in label_boxes]
        label_boxes.append(d2.textbbox((img.width-2*S, img.height+S), floor,
                           font=ImageFont.truetype(FONT_EN, 16*S), anchor="ra"))
        # 方位記号の N も labels 層の文字として避ける。
        radius = 7*S
        angle = math.radians(ang)
        nx = img.width+margin//2+math.sin(angle)*radius*1.6
        ny = 20*S-math.cos(angle)*radius*1.6
        label_boxes.append(d2.textbbox((nx, ny), "N",
                           font=ImageFont.truetype(FONT_EN, int(radius*.7)), anchor="mm"))
        building_box = (float(nz[1].min()-cx0), float(nz[0].min()-cy0),
                        float(nz[1].max()-cx0), float(nz[0].max()-cy0))
        full, svg_offset, svg_annotations = draw_annotations(
            full, annot, rooms, region, (cx0,cy0,cx1,cy1), label_boxes, building_box)
    full.save(out)
    print(f"  -> {out} {full.size}")
    if svg:
        write_svg(svg, full.size, (cx0, cy0, cx1, cy1), rooms, region,
                  wall, line, svg_labels, floor, ang, svg_offset, svg_annotations)
    if debug_dir:
        dbg = np.dstack([ink] * 3)
        dbg[wall] = (0, 160, 0)
        for msk in region.values():
            dbg[msk] = (0, 0, 200)
        cv2.imwrite(str(Path(debug_dir) / f"debug_{floor}.png"), dbg)
    return out


def north_angle(segs_all, spans):
    """'真北' の文字に最も近い端点を持つ長い線の向き(画面座標、上=0°、時計回り)"""
    t = next((s for s in spans if "真北" in s["text"]), None)
    if t is None:
        return 0.0
    tx, ty = (t["bbox"][0] + t["bbox"][2]) / 2, (t["bbox"][1] + t["bbox"][3]) / 2
    best, bd = None, 1e9
    for x1, y1, x2, y2 in segs_all:
        L = math.hypot(x2 - x1, y2 - y1)
        if L < 25:
            continue
        for (ax, ay), (bx, by) in (((x1, y1), (x2, y2)), ((x2, y2), (x1, y1))):
            d = math.hypot(ax - tx, ay - ty)
            if d < bd and d < 30:
                bd, best = d, (bx, by, ax, ay)  # b → a が北向き
    if best is None:
        return 0.0
    bx, by, ax, ay = best
    return math.degrees(math.atan2(ax - bx, -(ay - by)))


def draw_north(d, center, r, ang_deg):
    cx, cy = center
    a = math.radians(ang_deg)
    ux, uy = math.sin(a), -math.cos(a)
    px, py = -uy, ux
    tip = (cx + ux * r, cy + uy * r)
    tail = (cx - ux * r, cy - uy * r)
    left = (cx + px * r * 0.45 - ux * r * 0.2, cy + py * r * 0.45 - uy * r * 0.2)
    right = (cx - px * r * 0.45 - ux * r * 0.2, cy - py * r * 0.45 - uy * r * 0.2)
    d.ellipse((cx - r * 1.15, cy - r * 1.15, cx + r * 1.15, cy + r * 1.15), outline=LINE_RGB, width=max(2, r // 14))
    d.polygon([tip, left, (cx, cy), right], fill=LINE_RGB)
    d.line([tail, (cx, cy)], fill=LINE_RGB, width=max(2, r // 14))
    f = ImageFont.truetype(FONT_EN, int(r * 0.7))
    d.text((cx + ux * r * 1.6, cy + uy * r * 1.6), "N", font=f, fill=LINE_RGB, anchor="mm")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--page", type=int, required=True)
    ap.add_argument("--floor", default="2F")
    ap.add_argument("--out", required=True)
    ap.add_argument("--debug-dir")
    ap.add_argument("--svg", help="PNG と同時に、編集用の4層 SVG を出力")
    ap.add_argument("--annot", help="車・引出しコメントを指定する JSON")
    a = ap.parse_args()
    build(a.pdf, a.page, a.floor, a.out, a.debug_dir, a.svg, a.annot)
