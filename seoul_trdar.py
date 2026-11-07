"""서울시 상권분석서비스 (상권 영역 · 점포/개폐업 · 추정매출) 수집과 가공

build_data.py가 불러 씁니다. 단독 실행하면 마포구 자료를 새로 받아 원본데이터 폴더에 저장합니다.
  python seoul_trdar.py

인증키: 이 폴더의 seoul_key.txt 파일(한 줄) 또는 환경변수 SEOUL_KEY
"""
import json
import math
import os
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "원본데이터" / "서울상권_마포구.json"
API = "http://openapi.seoul.go.kr:8088/{key}/json/{svc}/{a}/{b}/{extra}"
GU = "11440"

# 리포트 업종 → 서울시 서비스 업종 코드
BIZ_CODES = {
    "cafe": ["CS100010"],                                   # 커피-음료
    "food": [f"CS10000{i}" for i in range(1, 9)],          # 한식~분식 (호프 제외)
    "hair": ["CS200028"],                                   # 미용실
    "laundry": ["CS200031"],                                # 세탁소
    "store": ["CS300002"],                                  # 편의점
}
CODE_TO_BIZ = {c: b for b, cs in BIZ_CODES.items() for c in cs}


# ── API ──────────────────────────────────────────────────────────
def seoul_key():
    key = os.environ.get("SEOUL_KEY", "").strip()
    f = HERE / "seoul_key.txt"
    if not key and f.exists():
        key = f.read_text(encoding="utf-8").strip()
    if not key:
        sys.exit("서울 열린데이터광장 인증키가 없습니다. 이 폴더에 seoul_key.txt 파일을 만들고 키를 붙여 넣으세요.")
    return key


def call(key, svc, a, b, extra=""):
    url = API.format(key=key, svc=svc, a=a, b=b, extra=extra)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as res:
                body = json.loads(res.read().decode("utf-8"))
            break
        except Exception as e:
            if attempt == 3:
                sys.exit(f"서울시 API 요청 실패 ({svc}): {e}")
            time.sleep(3 * (attempt + 1))
    data = body.get(svc)
    if data is None:
        code = body.get("RESULT", {}).get("CODE")
        if code == "INFO-200":           # 해당 데이터 없음
            return 0, []
        sys.exit(f"서울시 API 오류 ({svc}): {body.get('RESULT')}")
    return int(data.get("list_total_count", 0)), data.get("row", [])


def call_all(key, svc, extra=""):
    total, rows = call(key, svc, 1, 1000, extra)
    start = 1001
    while start <= total:
        _, more = call(key, svc, start, start + 999, extra)
        rows += more
        start += 1000
        time.sleep(0.05)
    return rows


def fetch():
    key = seoul_key()
    print("서울시 상권: 상권 영역 받는 중")
    areas = [r for r in call_all(key, "TbgisTrdarRelm") if r["SIGNGU_CD"] == GU]
    codes = {r["TRDAR_CD"] for r in areas}
    print(f"  마포구 상권 {len(areas)}개")

    stores = []
    for i, a in enumerate(areas, 1):
        rows = call_all(key, "VwsmTrdarStorQq", f"%20/{a['TRDAR_CD']}")
        stores += [r for r in rows if r["SVC_INDUTY_CD"] in CODE_TO_BIZ]
        if i % 10 == 0 or i == len(areas):
            print(f"  점포·개폐업 {i}/{len(areas)} 상권")
    quarters = sorted({r["STDR_YYQU_CD"] for r in stores})

    sales = []
    for q in quarters:
        rows = call_all(key, "VwsmTrdarSelngQq", q)
        sales += [r for r in rows if r["TRDAR_CD"] in codes and r["SVC_INDUTY_CD"] in CODE_TO_BIZ]
        print(f"  추정매출 {q} (마포구 누적 {len(sales):,}행)")

    keep_s = ["STDR_YYQU_CD", "TRDAR_CD", "SVC_INDUTY_CD", "STOR_CO", "SIMILR_INDUTY_STOR_CO", "FRC_STOR_CO",
              "OPBIZ_RT", "OPBIZ_STOR_CO", "CLSBIZ_RT", "CLSBIZ_STOR_CO"]
    keep_m = ["STDR_YYQU_CD", "TRDAR_CD", "SVC_INDUTY_CD", "THSMON_SELNG_AMT", "THSMON_SELNG_CO",
              "MDWK_SELNG_AMT", "WKEND_SELNG_AMT"]
    out = {
        "fetched": date.today().isoformat(),
        "areas": areas,
        "stores": [{k: r[k] for k in keep_s} for r in stores],
        "sales": [{k: r[k] for k in keep_m} for r in sales],
    }
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"  저장: {CACHE.relative_to(HERE)} (점포 {len(stores):,}행, 매출 {len(sales):,}행)")
    return out


# ── 좌표 변환: EPSG:5181 (GRS80 중부원점, 가산값 200000/500000) → 위경도 ──
def tm5181_to_wgs84(x, y):
    a, f = 6378137.0, 1 / 298.257222101
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    lat0, lon0, k0, fe, fn = math.radians(38), math.radians(127), 1.0, 200000.0, 500000.0

    def merid(phi):
        return a * ((1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256) * phi
                    - (3 * e2 / 8 + 3 * e2**2 / 32 + 45 * e2**3 / 1024) * math.sin(2 * phi)
                    + (15 * e2**2 / 256 + 45 * e2**3 / 1024) * math.sin(4 * phi)
                    - (35 * e2**3 / 3072) * math.sin(6 * phi))

    m = merid(lat0) + (y - fn) / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
            + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
            + (151 * e1**3 / 96) * math.sin(6 * mu) + (1097 * e1**4 / 512) * math.sin(8 * mu))
    n1 = a / math.sqrt(1 - e2 * math.sin(phi1)**2)
    t1 = math.tan(phi1)**2
    c1 = ep2 * math.cos(phi1)**2
    r1 = a * (1 - e2) / (1 - e2 * math.sin(phi1)**2)**1.5
    d = (x - fe) / (n1 * k0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (d**2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * ep2) * d**4 / 24
                                              + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * ep2 - 3 * c1**2) * d**6 / 720)
    lon = lon0 + (d - (1 + 2 * t1 + c1) * d**3 / 6
                  + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * ep2 + 24 * t1**2) * d**5 / 120) / math.cos(phi1)
    return math.degrees(lat), math.degrees(lon)


# ── 리포트용 자료로 가공 ─────────────────────────────────────────
def build(raw, compress):
    """반환: (trdar, tq, tstat, meta)
    trdar : [[상권코드, 상권명, 상권구분, 위도압축, 경도압축, 반경m], ...]
    tq    : 분기 코드 목록 (예: ["20213", ..., "20262"])
    tstat : {상권코드: {업종: {"n":[점포수], "o":[개업수], "c":[폐업수], "s":[분기매출(만원)], "t":[분기매출건수]}}}
    """
    trdar = []
    for r in sorted(raw["areas"], key=lambda r: r["TRDAR_CD"]):
        lat, lng = tm5181_to_wgs84(float(r["XCNTS_VALUE"]), float(r["YDNTS_VALUE"]))
        y, x = compress(lat, lng)
        radius = round(math.sqrt(float(r["RELM_AR"]) / math.pi))
        trdar.append([r["TRDAR_CD"], r["TRDAR_CD_NM"], r["TRDAR_SE_CD_NM"], y, x, radius])

    tq = sorted({r["STDR_YYQU_CD"] for r in raw["stores"]})
    qi = {q: i for i, q in enumerate(tq)}
    tstat = {}

    def slot(code, biz):
        b = tstat.setdefault(code, {}).setdefault(biz, {})
        for k in "nocst":
            b.setdefault(k, [0] * len(tq))
        return b

    for r in raw["stores"]:
        if r["STDR_YYQU_CD"] not in qi:
            continue
        b = slot(r["TRDAR_CD"], CODE_TO_BIZ[r["SVC_INDUTY_CD"]])
        i = qi[r["STDR_YYQU_CD"]]
        b["n"][i] += int(r["SIMILR_INDUTY_STOR_CO"] or 0)
        b["o"][i] += int(r["OPBIZ_STOR_CO"] or 0)
        b["c"][i] += int(r["CLSBIZ_STOR_CO"] or 0)
    for r in raw["sales"]:
        if r["STDR_YYQU_CD"] not in qi:
            continue
        b = slot(r["TRDAR_CD"], CODE_TO_BIZ[r["SVC_INDUTY_CD"]])
        i = qi[r["STDR_YYQU_CD"]]
        b["s"][i] += round(float(r["THSMON_SELNG_AMT"] or 0) / 10000)
        b["t"][i] += int(float(r["THSMON_SELNG_CO"] or 0))
    meta = {"trdar": f"{tq[0][:4]}년 {tq[0][4]}분기~{tq[-1][:4]}년 {tq[-1][4]}분기", "trdarFetched": raw["fetched"]}
    return trdar, tq, tstat, meta


if __name__ == "__main__":
    fetch()
