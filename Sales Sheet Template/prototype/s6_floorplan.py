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
  python s6_floorplan.py --pdf 図面.pdf --page 8 --floor 2F --out out/2F.png
"""
from __future__ import annotations

import argparse
import math
import re
import unicodedata
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


def building_bbox(rect, segs):
    """黒線を低解像度で描いて膨張 → 図枠を除いた最大の連結成分を建物とみなす"""
    sc = 2
    img = raster_segments(segs, (int(rect.height * sc), int(rect.width * sc)), (0, 0), sc, thick=1)
    img = cv2.dilate(img, np.ones((7, 7), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats((img > 0).astype(np.uint8))
    best = None
    page_area = img.shape[0] * img.shape[1]
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if w * h > 0.5 * page_area:  # 図枠
            continue
        if best is None or a > st[best][4]:
            best = i
    x, y, w, h, _ = st[best]
    return x / sc, y / sc, (x + w) / sc, (y + h) / sc


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


# ---------------------------------------------------------------- 本体
def build(pdf, pno, floor, out, debug_dir=None):
    rect, segs, widths, spans = read_page(pdf, pno)
    bx0, by0, bx1, by1 = building_bbox(rect, segs)
    m = 6
    bx0, by0, bx1, by1 = bx0 - m, by0 - m, bx1 + m, by1 + m

    # 建物範囲に収まる線だけ残す(範囲をまたぐ線 = 寸法補助線・引出線は捨てる)
    inb = ((segs[:, [0, 2]] >= bx0).all(1) & (segs[:, [0, 2]] <= bx1).all(1)
           & (segs[:, [1, 3]] >= by0).all(1) & (segs[:, [1, 3]] <= by1).all(1))
    segs = segs[inb]
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
        chosen = None
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
                chosen = (k, msk)
                break
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
    base_fs = int(28 * S)  # 主要室名の文字高さの基準(pt)
    for r in rooms:
        if r["erase"] or not r["label"] or id(r) not in region:
            continue
        text = r["label"].format(j=f"{r['jo']:.1f}J" if r["jo"] else "").strip()
        msk = region[id(r)]
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
            tmp = tmp.rotate(90, expand=True)
            img.paste(tmp, (int(cx - tmp.width / 2), int(cy - tmp.height / 2)), tmp)
        else:
            drw.multiline_text((cx, cy), text, font=font, fill=LINE_RGB, anchor="mm", align="center", spacing=0,
                               stroke_width=max(2, fs // 14), stroke_fill=(255, 255, 255))
    small = ImageFont.truetype(FONT_EN, int(9 * S))
    small_ja = ImageFont.truetype(FONT_JA, int(12 * S))
    for t in keep_text:
        b = t["bbox"]
        x, y = to_px((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
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
    full.save(out)
    print(f"  -> {out} {full.size}")
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
    a = ap.parse_args()
    build(a.pdf, a.page, a.floor, a.out, a.debug_dir)
