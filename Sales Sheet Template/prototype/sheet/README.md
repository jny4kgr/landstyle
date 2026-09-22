# 販売図面「中面」の試作

設計の正本は `../../02_セクション設計.md`、`../../03_S13物件概要_項目定義.md`、`../../04_S5価格ブロック.md`。新築戸建と同一分譲地内の建築条件付売地を対象とする。入力文の生成や地図・間取り図の作図はしない。会社ロゴ・シリーズロゴは文字で代用する。

## 実行

Python 3.10 以上。追加 Python パッケージは不要。PDF は OS ごとに Chrome・Edge・Chromium を探索して使う。

```sh
python3 build_sheet.py --data data/example_property.json --out /private/tmp/sheet-example --stage final
python3 build_sheet.py --data /path/to/property.json --out /private/tmp/sheet-rough --stage rough --no-pdf
```

- 出力は `naka.html`・`naka.pdf`・`report.json`・`report.md`。`--no-pdf` では HTML とレポートのみ。
- `rough`: 点検 error があっても HTML/PDF を作る。不足欄は赤点線の「要入力」、交通は「距離未入力」。数値矛盾・禁止語は欄または下端の要確認一覧で示す。
- `final`: error があればレポートのみを書き、終了コード 2。warning のみなら出力する。
- 成功（または明示的な `--no-pdf`）は終了コード 0。Chrome の起動・PDF 生成失敗は終了コード 1、HTML とレポートは残る。`pdf_status` は `created` / `skipped` / `blocked` / `unavailable`。
- 再実行時は指定出力先の旧 `naka.html` と `naka.pdf` を取り除く。不合格の版に前の PDF が残る事故を防ぐ。入力 JSON と別の専用出力ディレクトリを指定する。
- 画像のパスは入力 JSON のディレクトリ基準の相対パス、または絶対パス。PNG/JPEG/WebP/GIF/SVG を HTML に埋め込み、単独で開けるようにする。未設定の画像は枠。指定したファイルが存在しない場合は error。
- `templates/naka.html.j2` は `{{ name }}` のトップレベル差し込みのみを使用する。Jinja2 インストール不要。入力文字列は HTML エスケープし、テンプレートとして評価しない。
- 用紙は CSS `@page size:364mm 257mm`、背景色込み。`pdfinfo` と `pdftoppm` でページ数・寸法・目視確認が必要。長大な文章や多数の棟を自動的に省略はしないため、1ページに収まるかは出力確認が必要。

## JSON と項目 ID

実際の構造は `data/example_property.json`（全て架空、実物件素材なし）を参照。数値は JSON number、価格・追加費用は万円、面積は㎡、距離・道路幅員はm。入力しない値は `null` またはキー省略。明示的な負担なし等は `"なし"` とする。

| ID | キー | 内容 |
|---|---|---|
| P01 | common.address | 住居表示 |
| P02 | common.lot_address | 地番 |
| P03 | access[].line / station / distance_m | 路線・駅・道路距離。徒歩分の入力は不要 |
| P04 | common.zoning | 用途地域 |
| P05 | common.coverage | 建ぺい率% |
| P06 | common.far | 容積率% |
| P07 | common.height_zone | 高度地区 |
| P08 | common.fire_zone | 防火地域・法22条区域 |
| P09 | common.land_category | 地目（必要なら現況も文字で併記） |
| P10 | common.planning | 都市計画 |
| P11 | common.rights | 権利。借地なら lease_type / lease_term / ground_rent も必須 |
| P12 | common.utilities[] | 電力・水道・排水・ガスの順 |
| P13 | common.roads[].direction / width_m / type / paved | 接道方位・幅員・公私道・舗装 |
| P14 | common.private_road | 私道負担（なし、または面積と内容） |
| P15 | common.laws[] | 正式な法令名。該当なしは `["なし"]` |
| P16 | buildings / lands の配列長 | 総棟数・区画数を計算 |
| P17 | 各リストの status=販売中 | 今回販売数を計算 |
| P18 | 各棟・区画の price | 概要は別記、S5 に実額 |
| P19 | common.valid_until | 取引条件の有効期限（YYYY-MM-DD） |
| B01 | buildings[].name | 号棟 |
| B02 | buildings[].price | 販売価格（万円・税込） |
| B03 | buildings[].layout | 例 3(4)LDK+S+3バルコニー |
| B04 | buildings[].land_area / area_basis | 土地面積・公簿／実測 |
| B05 | buildings[].building_area / has_garage / garage_area / garage_included | 建物面積・車庫有無・車庫面積・算入確認 |
| B06 | buildings[].structure | 構造・屋根・N階建 |
| B07 | buildings[].confirmation | 建築確認番号。パンフレット基準で完成後も必須 |
| B08 | buildings[].completion / completion_status | 年月（末など可）、完成予定／完成済の一択 |
| B09 | buildings[].handover / handover_month | 相談などの条件と可能年月。「相談」単独は不可 |
| B10 | equipment[] または buildings[].equipment[] | 主たる設備。台所・浴室・便所の設備を点検 |
| B11 | buildings[].status | 販売中／商談中／済 |
| L01 | lands[].name | 区画番号 |
| L02 | lands[].price | 土地価格（万円・非課税） |
| L03 | lands[].land_area / area_basis | 面積・公簿／実測 |
| L04 | lands[].condition | 更地など |
| L05 | lands[].handover | 引渡し |
| L06 | lands[].contract_months / contractor | 請負契約期限の月数と請負先 |
| L07 | lands[].reference_plan.price | 参考建物価格（万円・税込） |
| L08 | lands[].reference_plan.building_area | 参考建物面積 |
| L09 | L02+L07 を自動計算 | 合計。土地非課税・建物税込を併記 |
| L10 | lands[].reference_plan.extra_costs[].label / amount | 建築代金以外の費用と金額（万円）。不要なら「追加費用なし」・0 を明示 |
| L11 | lands[].status | 販売中／商談中／済 |

`済` の棟・区画は総数と済印だけに使い、価格表示・販売用必須項目点検から除く。`商談中` は表示するが、今回販売数（販売中）には含めない。概要の棟別差分は「別記」と対応値を示し、価格・面積等は S5 を参照する。

| 注記 | 自動挿入条件・補助入力 |
|---|---|
| N1 | buildings があるとき、TV 等の費用負担 |
| N2 | utility_poles_possible=true のとき電柱・支線 |
| N3 | 常時、指定司法書士・家屋調査士 |
| N4 | 常時、図面・設備変更 |
| N5 | 常時、現況優先 |
| N6 | 販売用 lands ごと、contract_months と contractor を差し込み |
| N7 | reference_plan があるとき、adoption_optional=true と extra_costs が必要。自由採用と追加費用を表示 |
| N8 | hero.path を載せるとき hero.caption（イメージパース・弊社施工例・撮影日等）必須 |

その他の入力:

- `series / city / area / roman`: 左上のシリーズ・市名・エリア・ローマ字。
- `catch`: 行ごとの配列。各行、`subcopy`、`buildings[].features[]`、`buildings[].comments[]` は、従来の文字列に加えて `{"text":"LDK16帖","basis":["buildings[0].rooms"]}` の形を使える。AI が書く文章には、事実の参照先を JSON のドット記法で `basis` に付ける。配列要素は `buildings[0]` のように指定する。`highlight[]` の語だけ赤く大きく表示する。
- `hero: {path, caption}`: 外観。`map_path / division_path`: 地図・区画図画像。`navigation`: カーナビ住所。
- `facilities[]: {name, distance_m}`: 周辺施設。徒歩は道路距離÷80の切上げ。
- `buildings[].features[] / comments[]`: 入力済みの特長とコメント。価格付近は3ラベルまで、残りは間取り下部。
- `floorplans[]: {label, path}`: 棟または reference_plan に置く。SVG 等を縦横比維持で縮小。
- `buildings[].rooms[]: {type, jo}`: 任意の図面照合用データ。type は居室／LDK／納戸等。S を居室に数えない。室数・LDK の矛盾は warning。
- `warranties[]`: 保証文。`transaction / commission`: 会社帯の既定値を変更する場合。

## 点検と計算

`checks.py` の `check(data)` が `level / id / path / message / question` の一覧を返す。入力全体は `property.schema.json`（JSON Schema draft 2020-12）に照らし、型違いと必須キー不足を error、定義外のキーを warning にする。外部ライブラリは使わず、`type / required / properties / items / enum` を検査する。

文章は次を点検する。

- `basis` がない文章は warning。パスが存在しない、または参照値が空の場合も warning。
- 禁止語はキャッチ・副文・特長・コメントと、その候補すべてで error。
- `帖 / J / m / ㎡ / 分 / Nバルコニー` の数値は `basis` の参照先に照合し、不一致は error。`basis` がなければ入力全体を照合する。
- 文字数は warning。キャッチは各行28字・2行、サブコピーは8〜16字、特長は4〜15字、コメントは各行16字・2行が基準。

### 文章候補から選ぶ流れ

トップレベルの `copy_candidates` に `catch / subcopy / features / comments` を置く。キャッチは候補ごとに行の配列、コメント候補は `building / room / text / side / basis` を持つ。候補は紙面に出ず、`report.md` の「文章の候補」表と `report.json` の `candidates` に番号付きで出る。営業は本文・根拠と禁止語／数値／文字数の OK・NG を見て番号を選び、選んだ値を本文側の `catch` 等へ移す。

法令名辞書外は warning。坪は Decimal により㎡×0.3025を小数第2位切捨て。価格合計も Decimal で計算する。

この試作は指定設計資料のルールを実装したもの。規約全体の適法性を判定するものではない。法令名辞書は `checks.py` の `LAWS`。数値の文脈判定、画像内の文字・部屋数の自動読取、参照先と無関係な同値の区別は行わない。

## この環境での検証（2026-09-17）

TASK.md の verification を実行。禁止語入り final は exit=2、PDF 未出力、report.md の「最高」は2行。浜崎 final は exit=2、rough の点検は error=6 / warning=0（2駅の道路距離、号棟、私道負担、有効期限、引渡し可能年月）。HTML の `24.99坪`・`36.44坪`・`徒歩9分` を確認。

Chrome は sandbox 内で exit=-6。通常実行の架空 final と浜崎 rough は PDF 失敗を示す exit=1。HTML とレポートは完成、`--no-pdf` で両方 exit=0。`pdfinfo`・`pdftoppm` は PDF 不在で失敗したため、1ページ B4 の実測と preview PNG の目視は未検証。

完成版画像を開いて配色・配置・文字比率を観察し、黒い左帯、明朝キャッチ、赤い強調、価格、SVG3階分、概要、設備・保証、会社帯に反映した。完成版の間取りを上下に分けた配置に対し、本試作は3階を横並びにする。地図・区画図は未設定枠、ロゴは文字、設備は文字バッジ。ブラウザのローカル URL 表示もポリシーで拒否されたため、生成 HTML のはみ出し・可読性を目視確認できていない。サンドボックス外の Chrome で TASK.md の PDF 生成・寸法・PNG 目視確認を継続する。

未選択のキャッチ・サブコピー・特長ラベル（null・空文字・空配列）は、rough で項目ごとに未選択 warning を1件表示し、根拠・文字数は点検しない。final ではキャッチ・販売対象棟の特長ラベルは error、任意のサブコピーは warning。済の棟と、図面に焼き込み済みの場合もある空のコメントは選択要求の対象外。

室数照合は居室・洋室・和室・主寝室・子供部屋を数える。納戸・サービスルーム・Sは居室から除き、間取りに +S があるとき存在を確認する。3(4)LDK は3室か4室なら一致。逆質問では号棟名、未入力なら「この新築の棟（N番目）」を示す。


## 地図・区画図・表紙の作成手順（追加）

以下は上記の「未対応」の記述に対する更新です。地図・区画図・設備アイコン・表紙は作成できます。Python は macOS では `prototype/.venv/bin/python`、Windows では `prototype\.venv\Scripts\python.exe` を使います（既存の Pillow・OpenCV・NumPy が必要です）。実物件のデータと出力は引き続きリポジトリ外へ置いてください。

### 手順 3.5: 地図と区画図

駅へのルートは `--station "北朝霞駅" --station "朝霞台駅"` のように複数指定できます。最初の駅のルート全体が入る範囲と zoom を自動選択します。`--zoom` を指定すると現地中心の指定倍率になります。駅検索・ルート取得が失敗した駅は警告して省略し、現地の地図は作ります。

**出た距離は必ず営業に確認してから `access[].distance_m` に書いてください。** 規約は道路距離での表示を求め、駅のどの出入口を起点にするかで距離が変わるためです。スクリプトは property.json を更新しません。標準出力に確認文と採用した駅の表示名、`map.png.json` の `stations` に `name`・`display_name`・`distance_m`・`walk_min` を出します。

駅検索は Nominatim（User-Agent つき、呼び出し間隔1秒以上）、歩行ルートは Valhalla を使い、両方とも `--cache` の下に保存して再利用します。ルートつき画像の出典は「出典：国土地理院（地理院タイル）／© OpenStreetMap contributors」、メタデータは `sources: ["gsi", "osm"]` です。紙面も `map_source: "gsi+osm"` で同じ出典になり、未指定なら map_path に隣接する `<png>.json` の sources から自動判定します。`--offline-test` は南西600m先のダミー駅と3区間の折れ線を使用し、`--self-test` で polyline の復元を確認できます。


```sh
python "$S/sheet/make_map.py" --address "<物件住所>" --out "$P/map.png"
# 座標が分かっている場合
python "$S/sheet/make_map.py" --lat 35.816887 --lon 139.592133 --out "$P/map.png" --zoom 17 --size 900x600
python "$S/sheet/make_siteplan.py" --data "$P/property.json" --out "$P/siteplan.svg"
```

`$S` は prototype、`$P` はリポジトリ外の物件フォルダです。検索で採用した住所・座標と地図上の位置を営業に確認してください。住所検索は先頭の結果を採用するため、現地そのものを指すとは限りません。地図は `map_path` に設定します。出典は画像右下に入り、座標・zoom・採用住所・取得日は `map.png.json` に残ります。`--cache <dir>` でタイルの保存先を指定できます。取得間隔は0.2秒、タイムアウトは20秒です。住所検索0件は終了コード3、その他の失敗は1です。`--offline-test --lat ... --lon ...` はネットワークを使わず格子タイルで検証します。

区画図は `site_plan` に、メートル単位（x右・y上）の `roads[].polygon` / `label` と `lots[].polygon` / `name` を入力します。`north_deg` は上から時計回りの北方向の角度です。区画名を buildings / lands の name と一致させ、販売状況・面積・種別は元データで管理します。3%以上の面積差と名前の不一致は warning です。`build_sheet.py` は `site_plan` から出力先へ `siteplan.svg` を自動生成します。手動の `division_path` があれば優先します。寸法・形状・販売状況は営業が図面と照合してください。

### 手順 6: 表紙

```sh
python "$S/sheet/build_sheet.py" --data "$P/property.json" --out "$P/out/<日付>" --stage final --cover
```

中面に加え、B4横1ページの `cover.html` / `cover.pdf`（右半分が表紙・左半分が裏表紙）を作ります。`--no-pdf` ならHTMLとレポートのみです。再実行時は旧版の中面・表紙HTML/PDFと自動区画図を取り除きます。

`cover.photos` は `{ "path": "写真のパス", "caption": "弊社施工例" }` の配列（最大3枚）です。写真は営業が選びます。写真がない枠は rough で点線、final で非表示です。外観は既存の `hero` を使い、写真・パースには説明が必要です。`cover.qr` は `{ "label": "見出し", "url": "https://..." }` の配列です。キー省略時は company.json の最新情報・施工例の2件を使います。QRは8px/モジュール・4モジュール余白で生成します。

会社への案内図は `make_map.py --address <会社住所> --label 当社 --out <png>` で作り、`data/company.json` の `access_map_path` に設定します（相対パスは company.json 基準）。最寄り駅からの所要時間は、確認した文を `access_map_caption` に入力します。案内図がなければ枠は出ません。

設備は `templates/icons/icons.json` のキーワード部分一致で自作SVGと対応付けます。未対応の設備は文字バッジです。PDF化後はページ数・B4寸法・写真の説明・設備名の可読性・区画図の寸法・QRの読み取りを確認してください。


### 地図・区画図・パースの表示サイズ

- 中面の地図枠は幅71mm・高さ約47.33mm、縦横比 **3:2**。画像を中央（現地）基準の `object-fit: cover` で枠いっぱいに表示する。`make_map.py` の既定は **zoom 17 / 900×600px**。900px幅で星の半径28px、ラベルは34pxの太字＋白フチ。従来の900×680画像は上下が切れ、右下の出典も切れるため、既定サイズで作り直す。独自の `--size` も3:2にそろえる。出典を含む画像を使い、位置は営業に確認する。
- 区画図は図形・寸法・方位・文字の描画範囲から viewBox を求め、各辺に約6%の余白を付ける。表示枠はCLIの既定が60×45mm、中面への自動生成は実際の画像枠に合わせて57.4×44.4mm。枠の縦横両方と余白を含めて計算し、区画名・面積は6.6pt（最低6.5pt）、辺長は5.6pt（最低5.5pt）を確保する。面積と坪数は2行に分ける。
- `make_siteplan.py --data "$P/property.json" --out "$P/siteplan.svg" --rotate auto --frame 60x45`。`--rotate auto`（既定）は0度／時計回り90度のうち、枠に大きく収まる向きを選ぶ。`--rotate 0` / `--rotate 90` で固定でき、`--frame 幅x高さ` はmm。回転時は方位も同じ角度だけ回し、区画名・面積の文字は正立させる。別の枠に置く場合は、その枠寸法を指定して再生成する。
- 区画図の下の凡例は表示しない。赤丸の「済」は `site_plan.lots[].name` に対応する区画が `status: "済"` の場合、その区画内だけに表示する。共有辺の寸法は重複させず1つ表示する。
- 表紙の外観パースは中央基準の `object-fit: cover`。キャプションは画像内の左下に7pt・白フチ付きで重ねる。画像の端がトリミングされるため、PDFの構図は営業が確認する。

従来と同じ TASK.md の verification を実行した場合、地図サイズは今回の既定変更により `(900, 600)`、メタデータの zoom は17となる。終了コードの期待値は変わらない。


## final の安全装置

`--stage final` は、紙面に「要入力」または画像の「未設定」の枠（地図・外観パース・間取り図・区画図・施工例）が残っていると、PDF を出さずに終了コード 2 で止まる。リポジトリの架空データ（`data/example_property.json`）は画像を持たないので、検証するときは `--allow-missing-images` を付ける。実物件では付けない。

地図は `map_source` が `gsi`（既定）のとき、画像が切り抜かれても出典が消えないよう、紙面側にも「出典：国土地理院（地理院タイル）」を重ねて表示する。

### 手順 5: 家具・家事動線の書き足し

家具と家事動線の矢印は、営業が希望したときだけ `--annot` で入れます。`prototype/annot_example.json` の furniture / arrow を参考に、部屋名・種類・位置・回転、矢印の折れ点と色を指定してください。配置できない家具は警告して省略されます。


## Windows での動作

Python と `prototype/requirements.txt` の依存を入れた環境で実行します。
`prototype` ディレクトリで `python platform_paths.py` を実行すると、この PC で見つかったフォントとブラウザを表示します。`python platform_paths.py --self-test` は実機のファイルに依存しない模擬テストです。

フォントは `%WINDIR%\Fonts`（既定 `C:\Windows\Fonts`）から、明朝は yumin.ttf → msmincho.ttc、ゴシックは YuGothM.ttc → YuGothR.ttc → meiryo.ttc → msgothic.ttc、太字は YuGothB.ttc → meiryob.ttc、英字は times.ttf の順に探します。macOS は従来のヒラギノと Times New Roman を優先し、Linux は Noto CJK などを探します。

ブラウザは `%ProgramFiles%`・`%ProgramFiles(x86)%`・`%LocalAppData%` の Chrome を順に探し、次に同じ場所の Edge を探します。macOS は Chrome → Edge → Chromium、Linux は PATH 上の google-chrome → chromium → microsoft-edge の順です。PDF 化は Chrome と Edge 共通の `--headless=new --print-to-pdf` を使用します。

見つからない場合は、エラーに探索したパスを表示します。環境変数 `SHEET_FONT_JA_SERIF`、`SHEET_FONT_JA_SANS`、`SHEET_FONT_JA_SANS_BOLD`、`SHEET_FONT_EN_SERIF` にフォントファイルのパス、`SHEET_BROWSER` にブラウザ実行ファイルのパスを指定できます。これらは OS の候補より優先し、指定先が存在しない場合は通常の候補も探します。PowerShell の指定例:

```powershell
$env:SHEET_BROWSER = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
$env:SHEET_FONT_JA_SERIF = "C:\Windows\Fonts\yumin.ttf"
python platform_paths.py
```
