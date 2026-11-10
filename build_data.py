"""상가 입지 리포트 데이터 생성기 (mapo-data.js)

사용법
  python build_data.py            # 원본데이터 폴더의 최신 파일로 mapo-data.js를 다시 만듭니다
  python build_data.py --check    # 파일은 쓰지 않고, 기존 mapo-data.js와 무엇이 달라지는지만 보여 줍니다
  python build_data.py --seoul    # 서울시 상권분석서비스(상권별 점포·개폐업·매출)를 API로 새로 받습니다 (seoul_key.txt)
  python build_data.py --osm      # OpenStreetMap 역·도로 자료를 새로 받습니다 (없으면 원본데이터 폴더의 저장본을 씁니다)
  python build_data.py --api      # 상가정보를 공공데이터포털 API로 직접 받아 원본데이터 폴더에 저장한 뒤 진행합니다
                                  #   API 키는 이 폴더의 api_key.txt 파일(한 줄) 또는 환경변수 DATA_GO_KR_KEY에 넣습니다

원본 파일 (이 폴더 또는 '원본데이터' 폴더에 넣으면 이름으로 자동으로 찾습니다)
  1. 소상공인 상가(상권)정보   : 이름에 '상가' 와 '서울' 이 들어간 .csv 또는 .zip   (공공데이터포털 15083033, 분기)
  2. 길단위인구 - 행정동        : 이름에 '길단위인구-행정동' 이 들어간 .csv          (서울 열린데이터광장, 분기)
  3. 길단위인구 - 서울시        : 이름에 '길단위인구-서울시' 가 들어간 .csv          (서울 열린데이터광장, 분기)
  4. 임대동향 지역별 임대료     : 이름에 '임대동향' 과 '소규모' 가 들어간 .csv        (한국부동산원 R-ONE, 분기)

상가정보 파일이 없으면 기존 mapo-data.js의 상가·주소 자료를 그대로 쓰고, 나머지만 새로 계산합니다.
권역 경계·약도(base, tf), 주요상권 경계(areas)는 손으로 만든 자료라 기존 파일에서 그대로 가져옵니다.
"""
import copy
import csv
import io
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "mapo-data.js"
BACKUP = HERE / "_backup"
SEARCH_DIRS = [HERE / "원본데이터", HERE]

# ── 설정 ─────────────────────────────────────────────────────────
GU_CODE = "11440"            # 마포구 시군구 코드
GU_NAME = "마포구"
LAT0, LNG0 = 37.5, 126.85    # 좌표 압축 기준점 (1 단위 = 0.00001도)
RADIUS = 300                 # 경쟁 기준값을 셀 반경 (m), HTML의 RADIUS와 같아야 합니다
SAMPLE_STEP = 8              # 기준값 계산 때 주소를 몇 개마다 하나씩 볼지, HTML과 같아야 합니다

BIZ_EXACT = {"cafe": {"카페"}, "hair": {"미용실"}, "laundry": {"세탁소"}, "store": {"편의점"}}
FOOD_EXCLUDE = {"카페", "구내식당", "뷔페", "무도 유흥 주점", "생맥주 전문", "요리 주점", "일반 유흥 주점"}
LIFE_TOPS = {"소매", "음식", "수리·개인"}   # 지도 밀도와 '많은 업종' 집계에 쓰는 대분류
ZONE_DONGS = {"연남동", "동교동"}         # 연남동 권역을 붙일 법정동 (권역 지도 범위)

REB_AREAS = ["동교/연남", "홍대/합정", "망원역", "공덕역", "신촌/이대", "영등포신촌"]

TIME_COLS = ["시간대_00_06", "시간대_06_11", "시간대_11_14", "시간대_14_17", "시간대_17_21", "시간대_21_24"]
AGE_COLS = ["연령대_10", "연령대_20", "연령대_30", "연령대_40", "연령대_50", "연령대_60_이상"]
WEEKDAYS = ["월요일", "화요일", "수요일", "목요일", "금요일"]
WEEKEND = ["토요일", "일요일"]


# ── 공통 도구 ────────────────────────────────────────────────────
def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    raise ValueError("인코딩을 알 수 없는 파일입니다 (UTF-8, CP949 아님)")


def find_file(*words, exts=(".csv",)):
    hits = []
    for d in SEARCH_DIRS:
        if d.is_dir():
            hits += [p for p in d.iterdir() if p.suffix.lower() in exts and all(w in p.name for w in words)]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def read_csv(path: Path):
    return list(csv.DictReader(io.StringIO(decode(path.read_bytes()))))


def quarter_label(code: str) -> str:
    return f"{code[:4]}년 {code[4]}분기"


def load_previous():
    if not OUT.exists():
        sys.exit("기존 mapo-data.js가 없습니다. 권역 지도와 주요상권 경계를 가져올 곳이 필요합니다.")
    text = OUT.read_text(encoding="utf-8").strip()
    return json.loads(text[text.index("=") + 1:].rstrip(";"))


def compress(lat, lng):
    return round((float(lat) - LAT0) * 1e5), round((float(lng) - LNG0) * 1e5)


M_LAT = 1.11195
M_LNG = 1.11195 * math.cos(37.55 * math.pi / 180)


# ── 0. 상가정보 API로 받기 (공공데이터포털 '소상공인시장진흥공단_상가(상권)정보' API) ──
API_URL = "https://apis.data.go.kr/B553077/api/open/sdsc2/storeListInDong"
API_FIELDS = {  # API 필드 → 파일 데이터와 같은 컬럼 이름
    "bizesNm": "상호명", "indsLclsNm": "상권업종대분류명", "indsSclsNm": "상권업종소분류명",
    "signguCd": "시군구코드", "signguNm": "시군구명", "adongCd": "행정동코드",
    "lnoAdr": "지번주소", "rdnmAdr": "도로명주소", "flrNo": "층정보", "lon": "경도", "lat": "위도",
}


def api_key():
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    f = HERE / "api_key.txt"
    if not key and f.exists():
        key = f.read_text(encoding="utf-8").strip()
    if not key:
        sys.exit("API 키가 없습니다. 이 폴더에 api_key.txt 파일을 만들고 키 한 줄을 붙여 넣으세요.")
    return urllib.parse.unquote(key)  # 인코딩 키를 넣어도 일반 키로 바꿔 씁니다


def fetch_shops_api() -> Path:
    key = api_key()
    rows, page, total = [], 1, None
    while total is None or len(rows) < total:
        query = urllib.parse.urlencode({"serviceKey": key, "divId": "signguCd", "key": GU_CODE,
                                        "numOfRows": 1000, "pageNo": page, "type": "json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(f"{API_URL}?{query}", timeout=60) as res:
                    raw = res.read().decode("utf-8")
                break
            except Exception as e:
                if attempt == 2:
                    sys.exit(f"API 요청 실패 ({page}쪽): {e}")
                time.sleep(3)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            sys.exit("API가 JSON이 아닌 응답을 보냈습니다. 키가 맞는지, 활용신청이 승인됐는지 확인하세요.\n" + raw[:300])
        head = body.get("header", {})
        if head.get("resultCode") not in ("00", None):
            sys.exit(f"API 오류: {head.get('resultCode')} {head.get('resultMsg')}")
        data = body.get("body", {})
        total = int(data.get("totalCount", 0))
        items = data.get("items") or []
        if not items:
            break
        rows += [{col: it.get(f, "") for f, col in API_FIELDS.items()} for it in items]
        print(f"  상가정보 API {page}쪽: {len(rows):,} / {total:,}")
        page += 1
        time.sleep(0.2)
    if not rows:
        sys.exit("API에서 받은 상가가 없습니다.")
    out_dir = HERE / "원본데이터"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"상가정보_서울_{GU_NAME}_API_{date.today():%Y%m}.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(API_FIELDS.values()))
        w.writeheader(); w.writerows(rows)
    print(f"  저장: {out.relative_to(HERE)} ({len(rows):,}건)")
    return out


# ── 1. 상가 자료 ─────────────────────────────────────────────────
def shop_rows(path: Path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            pick = [n for n in names if "서울" in n] or names
            if not pick:
                sys.exit(f"{path.name} 안에 csv가 없습니다.")
            text = decode(z.read(pick[0]))
    else:
        text = decode(path.read_bytes())
    return csv.DictReader(io.StringIO(text))


def strip_gu(addr: str) -> str:
    return re.sub(r"^\s*(서울특별시|서울시|서울)\s*마포구\s*", "", addr or "").strip()


def build_shops(path: Path):
    shops_raw, top_of = [], {}
    for r in shop_rows(path):
        if r.get("시군구코드", "")[:5] != GU_CODE and r.get("시군구명") != GU_NAME:
            continue
        try:
            lat, lng = float(r["위도"]), float(r["경도"])
        except (KeyError, ValueError):
            continue
        cat, top = r["상권업종소분류명"].strip(), r["상권업종대분류명"].strip()
        top_of[cat] = top
        shops_raw.append({
            "lat": lat, "lng": lng, "cat": cat, "name": r.get("상호명", "").strip(),
            "floor": (r.get("층정보") or "").strip(),
            "jibun": strip_gu(r.get("지번주소", "")), "road": strip_gu(r.get("도로명주소", "")),
            "hdong": (r.get("행정동코드") or "")[:8],
        })
    if not shops_raw:
        sys.exit(f"{path.name}에서 {GU_NAME} 상가를 찾지 못했습니다. 컬럼 이름(시군구코드, 위도, 경도 등)을 확인하세요.")

    cats = sorted(top_of)
    idx = {c: i for i, c in enumerate(cats)}

    def biz_key(cat):
        for key, names in BIZ_EXACT.items():
            if cat in names:
                return key
        return "food" if top_of[cat] == "음식" and cat not in FOOD_EXCLUDE else ""

    shops = []
    for s in shops_raw:
        y, x = compress(s["lat"], s["lng"])
        shops.append([y, x, idx[s["cat"]], biz_key(s["cat"]), s["name"], s["floor"]])

    # 주소 목록: 지번 하나에 점포가 여럿이면 좌표 평균
    by_addr = {}
    for s in shops_raw:
        if not s["jibun"]:
            continue
        a = by_addr.setdefault(s["jibun"], {"roads": {}, "hdong": s["hdong"], "lat": [], "lng": []})
        a["lat"].append(s["lat"]); a["lng"].append(s["lng"])
        if s["road"]:
            a["roads"][s["road"]] = a["roads"].get(s["road"], 0) + 1
    addr = []
    for jibun in sorted(by_addr):
        a = by_addr[jibun]
        y, x = compress(sum(a["lat"]) / len(a["lat"]), sum(a["lng"]) / len(a["lng"]))
        road = max(a["roads"], key=a["roads"].get) if a["roads"] else ""   # 가장 많이 쓰인 도로명
        addr.append([jibun, road, y, x, a["hdong"], 0])

    return {
        "cats": cats,
        "catTop": [top_of[c] for c in cats],
        "catLife": [1 if top_of[c] in LIFE_TOPS else 0 for c in cats],
        "shops": shops,
        "addr": addr,
    }


# ── 2. 연남동 권역 판정 ──────────────────────────────────────────
def parse_path(d: str):
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", d)]
    return list(zip(nums[0::2], nums[1::2]))


def inside(px, py, poly):
    hit = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]; xj, yj = poly[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def assign_zones(addr, base, tf):
    polys = {int(k): parse_path(v) for k, v in base["zones"].items()}
    for a in addr:
        a[5] = 0
        if a[0].split(" ")[0] not in ZONE_DONGS:
            continue
        x = tf[0][0] * a[2] + tf[1][0] * a[3] + tf[2][0]
        y = tf[0][1] * a[2] + tf[1][1] * a[3] + tf[2][1]
        a[5] = next((z for z, p in sorted(polys.items()) if inside(x, y, p)), 0)


# ── 3. 업종별 경쟁 기준값 (HTML thresholds()와 같은 방식) ─────────
def thresholds(addr, shops):
    try:
        import numpy as np
    except ImportError:
        np = None
    sample = addr[::SAMPLE_STEP]
    result = {}
    for key in ["cafe", "food", "hair", "laundry", "store"]:
        pts = [(s[0], s[1]) for s in shops if s[3] == key]
        if not pts:
            continue
        if np is not None:
            P = np.array(pts, dtype=float)
            counts = [int((np.hypot((a[2] - P[:, 0]) * M_LAT, (a[3] - P[:, 1]) * M_LNG) <= RADIUS).sum()) for a in sample]
        else:
            counts = [sum(1 for y, x in pts if math.hypot((a[2] - y) * M_LAT, (a[3] - x) * M_LNG) <= RADIUS) for a in sample]
        counts.sort()
        n = len(counts)
        result[key] = {"low": counts[n // 3], "high": counts[n * 2 // 3]}
    return result


# ── 4. 행정동 유동인구 ──────────────────────────────────────────
def build_population(dong_path: Path, seoul_path: Path):
    rows = read_csv(dong_path)
    q = max(r["기준_년분기_코드"] for r in rows)
    prev_q = f"{int(q[:4]) - 1}{q[4]}"
    cur = [r for r in rows if r["기준_년분기_코드"] == q]
    prev = {r["행정동_코드"]: r for r in rows if r["기준_년분기_코드"] == prev_q}
    f = lambda r, k: float(r[k + "_유동인구_수"])
    total = lambda r: float(r["총_유동인구_수"])
    pm = lambda r: f(r, "시간대_11_14") + f(r, "시간대_14_17")
    eve = lambda r: f(r, "시간대_17_21")
    end = lambda r: sum(f(r, d) for d in WEEKEND)
    young = lambda r: (f(r, "연령대_20") + f(r, "연령대_30")) / max(1.0, total(r))
    am = lambda r: f(r, "시간대_06_11")
    wk = lambda r: sum(f(r, d) for d in WEEKDAYS) / 5
    we = lambda r: sum(f(r, d) for d in WEEKEND) / 2

    def ranks(fn):
        order = sorted(cur, key=lambda r: -fn(r))
        return {r["행정동_코드"]: i + 1 for i, r in enumerate(order)}

    rk = {name: ranks(fn) for name, fn in [("day", pm), ("eve", eve), ("end", end), ("ry", young)]}
    avg = {name: sum(fn(r) for r in cur) / len(cur) for name, fn in [("am", am), ("pm", pm), ("eve", eve), ("wk", wk), ("we", we)]}
    fns = {"am": am, "pm": pm, "eve": eve, "wk": wk, "we": we}

    dongs = {}
    for r in cur:
        code = r["행정동_코드"]
        if not code.startswith(GU_CODE):
            continue
        t = total(r)
        p = prev.get(code)
        dongs[code] = {
            "name": r["행정동_코드_명"],
            "day": rk["day"][code], "eve": rk["eve"][code], "end": rk["end"][code],
            "young": round(young(r) * 100, 1), "ry": rk["ry"][code],
            "yoy": round((t / total(p) - 1) * 100, 1) if p and total(p) else 0,
            "time": [round(f(r, c) / t * 100, 1) for c in TIME_COLS],
            "age": [round(f(r, c) / t * 100, 1) for c in AGE_COLS],
            "dense": {k: round(fn(r) / avg[k], 2) for k, fn in fns.items()},
        }
    dongs = dict(sorted(dongs.items()))

    srows = read_csv(seoul_path)
    s = next(r for r in srows if r["기준_년분기_코드"] == q)
    st = total(s)
    seoul = {"time": [round(f(s, c) / st * 100, 1) for c in TIME_COLS],
             "age": [round(f(s, c) / st * 100, 1) for c in AGE_COLS]}
    return dongs, seoul, {"n": len(cur), "q": quarter_label(q)}


# ── 5. 한국부동산원 임대동향 ─────────────────────────────────────
def build_reb(path: Path):
    rows = list(csv.reader(io.StringIO(decode(path.read_bytes()))))
    header = rows[0]
    cols = [i for i, h in enumerate(header) if re.match(r"\d{4}년 \d분기", h)]
    last = cols[-1]
    year_ago = cols[-5] if len(cols) >= 5 else cols[0]
    reb = {}
    for r in rows[3:]:
        if len(r) > last and r[1] == "서울" and r[3] in REB_AREAS:
            reb[r[3]] = [float(r[last]), float(r[year_ago])]
    missing = set(REB_AREAS) - set(reb)
    if missing:
        print(f"  ! 임대동향에서 찾지 못한 상권: {', '.join(sorted(missing))}")
    return reb, header[last]


# ── 실행 ────────────────────────────────────────────────────────
def main():
    check = "--check" in sys.argv
    old = load_previous()
    data = {k: old[k] for k in ["areas", "base", "tf", "unitsPerM", "zoneOK"]}
    meta = dict(old.get("meta", {}))

    shop_file = fetch_shops_api() if "--api" in sys.argv else find_file("상가", "서울", exts=(".csv", ".zip"))
    if shop_file:
        print(f"상가정보: {shop_file.name}")
        data.update(build_shops(shop_file))
        m = re.search(r"(20\d{2})(\d{2})", shop_file.name)
        meta["shops"] = f"{m.group(1)}년 {int(m.group(2))}월{' 조회' if 'API' in shop_file.name else ''}" if m else date.today().strftime("%Y년 %#m월" if sys.platform == "win32" else "%Y년 %-m월")
    else:
        print("상가정보: 새 파일 없음 → 기존 상가·주소 자료를 그대로 씁니다")
        data.update({k: copy.deepcopy(old[k]) for k in ["cats", "catTop", "catLife", "shops", "addr"]})
        meta.setdefault("shops", "2026년 9월")
    meta.setdefault("areas", "2024년 1월 1일")

    assign_zones(data["addr"], data["base"], data["tf"])
    data["th"] = thresholds(data["addr"], data["shops"])

    import osm_base
    if "--osm" in sys.argv:
        osm_raw = osm_base.fetch()
    elif osm_base.CACHE.exists():
        osm_raw = json.loads(osm_base.CACHE.read_text(encoding="utf-8"))
        print(f"OSM 역·도로: {osm_base.CACHE.name} ({osm_raw['fetched']} 받은 자료)")
    else:
        osm_raw = None
    if osm_raw:
        data["stations"], data["roads"] = osm_base.build(osm_raw, compress, data["addr"])
        meta["osm"] = f"{osm_raw['fetched'][:4]}년 {int(osm_raw['fetched'][5:7])}월"
    else:
        print("OSM 역·도로: 캐시 없음 → 기존 자료를 그대로 씁니다")
        before = {a[0]: a[6:8] for a in old["addr"]}
        for a in data["addr"]:
            del a[6:]
            a += before.get(a[0]) or [-1, -1]
        for k in ["stations", "roads"]:
            if k in old:
                data[k] = old[k]

    dong_file, seoul_file = find_file("길단위인구-행정동"), find_file("길단위인구-서울시")
    if dong_file and seoul_file:
        print(f"유동인구: {dong_file.name}, {seoul_file.name}")
        data["dongs"], data["seoul"], data["pop"] = build_population(dong_file, seoul_file)
    else:
        print("유동인구: 파일 없음 → 기존 자료를 그대로 씁니다")
        data.update({k: old[k] for k in ["dongs", "seoul", "pop"]})

    reb_file = find_file("임대동향", "소규모")
    if reb_file:
        print(f"임대동향: {reb_file.name}")
        data["reb"], meta["reb"] = build_reb(reb_file)
    elif "reb" in old:
        data["reb"] = old["reb"]
    import seoul_trdar
    cache = seoul_trdar.CACHE
    if "--seoul" in sys.argv or not cache.exists():
        raw = seoul_trdar.fetch() if ("--seoul" in sys.argv or os.environ.get("SEOUL_KEY") or (HERE / "seoul_key.txt").exists()) else None
    else:
        raw = json.loads(cache.read_text(encoding="utf-8"))
        print(f"서울시 상권: {cache.name} ({raw['fetched']} 받은 자료)")
    if raw:
        data["trdar"], data["tq"], data["tstat"], m2 = seoul_trdar.build(raw, compress)
        meta.update(m2)
    else:
        for k in ["trdar", "tq", "tstat"]:
            if k in old:
                data[k] = old[k]
    data["meta"] = meta

    order = ["cats", "addr", "areas", "th", "dongs", "seoul", "pop", "catTop", "catLife", "shops",
             "zoneOK", "base", "tf", "unitsPerM", "reb", "trdar", "tq", "tstat", "stations", "roads", "meta"]
    data = {k: data[k] for k in order if k in data}

    print("\n변경 요약 (기존 → 새 자료)")
    for k in ["cats", "addr", "shops"]:
        print(f"  {k:6s} {len(old[k]):>7,} → {len(data[k]):>7,}")
    zones = lambda a: [sum(1 for x in a if x[5] == z) for z in range(1, 6)]
    print(f"  권역별 주소 {zones(old['addr'])} → {zones(data['addr'])}")
    for k, v in data["th"].items():
        o = old["th"].get(k, {})
        print(f"  경쟁 기준 {k:7s} 적음≤{o.get('low')}/많음≥{o.get('high')} → 적음≤{v['low']}/많음≥{v['high']}")
    print(f"  유동인구 {old['pop']['q']} → {data['pop']['q']}  (행정동 {data['pop']['n']}개)")
    if "reb" in data:
        print(f"  임대동향 {meta.get('reb')}: " + ", ".join(f"{k} {v[0]}" for k, v in data["reb"].items()))
    if "tq" in data:
        print(f"  서울시 상권 {len(data['trdar'])}개, {meta.get('trdar')}")
    changed = [k for k in data if k in old and old[k] != data[k]]
    print(f"  달라진 항목: {', '.join(changed) or '없음'}")

    if check:
        print("\n--check: 파일은 쓰지 않았습니다.")
        return
    BACKUP.mkdir(exist_ok=True)
    backup = BACKUP / f"mapo-data_{date.today():%Y%m%d}.js"
    if not backup.exists():
        shutil.copy2(OUT, backup)
    OUT.write_text("const DATA=" + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";", encoding="utf-8")
    print(f"\n저장: {OUT.name} ({OUT.stat().st_size / 1e6:.1f}MB), 이전 파일은 {backup.relative_to(HERE)}")
    print("HTML의 mapo-data.js?v=... 숫자를 바꾸면 브라우저가 새 자료를 받습니다.")


if __name__ == "__main__":
    main()
