"""OpenStreetMap 자료: 지하철역, 도로망 (위치 지도 + 역까지 도보 거리 계산)

build_data.py가 불러 씁니다. 단독 실행하면 마포구 자료를 새로 받아 원본데이터 폴더에 저장합니다.
  python osm_base.py

자료 출처 표기 의무: © OpenStreetMap contributors (ODbL). 리포트 하단과 지도 설명에 표시합니다.
"""
import heapq
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "원본데이터" / "OSM_마포구.json"
BBOX = (37.520, 126.855, 37.605, 126.980)   # 남, 서, 북, 동 (마포구 + 경계 밖 역까지 여유 약 1km)
SERVERS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]

DRAW_MAJOR = {"trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link"}
DRAW_MINOR = {"unclassified", "residential", "living_street", "pedestrian"}
NO_WALK = {"trunk", "trunk_link"}             # 강변북로·내부순환 같은 자동차 전용 구간은 걷는 길에서 뺍니다
WALK_MPM = 80                                 # 도보 속도 (분당 m), HTML의 WALK_MPM과 같아야 합니다

M_LAT = 111195.0
M_LNG = 111195.0 * math.cos(37.55 * math.pi / 180)


# ── 받기 ─────────────────────────────────────────────────────────
def fetch():
    s, w, n, e = BBOX
    kinds = "|".join(sorted(DRAW_MAJOR | DRAW_MINOR | {"service", "footway", "path", "steps", "cycleway", "track"}))
    query = (f'[out:json][timeout:240];('
             f'way["highway"~"^({kinds})$"]({s},{w},{n},{e});'
             f'node["railway"~"^(station|subway_entrance)$"]({s},{w},{n},{e});'
             f');out body geom;')
    body = urllib.parse.urlencode({"data": query}).encode()
    raw = None
    for url in SERVERS:
        for attempt in range(2):
            try:
                print(f"OSM: {url.split('/')[2]} 에서 받는 중 (1~3분 걸릴 수 있습니다)")
                req = urllib.request.Request(url, data=body, headers={"User-Agent": "osm-extract/1.0"})
                with urllib.request.urlopen(req, timeout=300) as res:
                    raw = json.loads(res.read().decode("utf-8"))
                break
            except Exception as err:
                print(f"  실패: {err}")
                time.sleep(10)
        if raw:
            break
    if not raw:
        sys.exit("OSM 자료를 받지 못했습니다. 잠시 뒤 다시 실행하세요.")

    ways, stations, exits = [], [], []
    for el in raw["elements"]:
        tags = el.get("tags", {})
        if el["type"] == "node":
            item = [round(el["lat"], 6), round(el["lon"], 6)]
            if tags.get("railway") == "subway_entrance":
                exits.append(item)
            elif tags.get("name") and tags.get("station") != "light_rail" and tags.get("usage") != "industrial":
                stations.append([tags["name"]] + item)
        else:
            if tags.get("access") in ("private", "no"):
                walk = False
            else:
                walk = tags.get("foot") != "no" and tags["highway"] not in NO_WALK
            ways.append([tags["highway"], 1 if walk else 0, el["nodes"],
                         [[round(p["lat"], 6), round(p["lon"], 6)] for p in el["geometry"]]])
    out = {"fetched": date.today().isoformat(), "ways": ways, "stations": stations, "exits": exits}
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  저장: {CACHE.relative_to(HERE)} (도로 {len(ways):,}개, 역 {len(stations)}개, 출입구 {len(exits)}개, "
          f"{CACHE.stat().st_size / 1e6:.1f}MB)")
    return out


# ── 공통 ─────────────────────────────────────────────────────────
def to_m(lat, lng):
    return (lat - 37.55) * M_LAT, (lng - 126.9) * M_LNG


def station_points(raw):
    """같은 이름의 역(노선별 노드)을 하나로 묶고, 가까운 출입구를 붙입니다.
    반환: [{"name", "lat", "lng", "pts": [(lat, lng), ...]}]  pts는 도보 거리의 도착점."""
    groups = {}
    for name, lat, lng in raw["stations"]:
        key = name.strip().removesuffix("역")
        groups.setdefault(key, []).append((lat, lng))
    result = []
    for key, pts in sorted(groups.items()):
        lat = sum(p[0] for p in pts) / len(pts)
        lng = sum(p[1] for p in pts) / len(pts)
        result.append({"name": key, "lat": lat, "lng": lng, "own": pts, "exits": []})
    for lat, lng in raw["exits"]:
        y, x = to_m(lat, lng)
        best, gap = None, 450.0                 # 450m 안의 가장 가까운 역에 출입구를 붙입니다
        for st in result:
            sy, sx = to_m(st["lat"], st["lng"])
            d = math.hypot(y - sy, x - sx)
            if d < gap:
                best, gap = st, d
        if best:
            best["exits"].append((lat, lng))
    for st in result:
        st["pts"] = st["exits"] or st["own"]    # 출입구 자료가 없는 역은 역 중심점으로 계산합니다
    return result


def walk_graph(raw):
    """걸을 수 있는 도로로 그래프를 만듭니다. 반환: (좌표 {노드: (y, x)}, 인접 {노드: [(이웃, 거리)]})"""
    pos, adj = {}, {}
    for _, walk, ids, geom in raw["ways"]:
        if not walk:
            continue
        for nid, (lat, lng) in zip(ids, geom):
            pos[nid] = to_m(lat, lng)
        for a, b in zip(ids, ids[1:]):
            ya, xa = pos[a]; yb, xb = pos[b]
            d = math.hypot(ya - yb, xa - xb)
            adj.setdefault(a, []).append((b, d))
            adj.setdefault(b, []).append((a, d))
    return pos, adj


def nearest_nodes(tree_xy, query):
    import numpy as np
    try:
        from scipy.spatial import cKDTree
        d, i = cKDTree(tree_xy).query(query)
        return d, i
    except ImportError:
        ds, ids = [], []
        for q in query:
            dist = np.hypot(tree_xy[:, 0] - q[0], tree_xy[:, 1] - q[1])
            j = int(dist.argmin())
            ds.append(float(dist[j])); ids.append(j)
        return np.array(ds), np.array(ids)


def walk_distances(raw, stations, addr):
    """모든 역 출입구에서 동시에 퍼져 나가는 최단 경로로 주소별 가장 가까운 역과 도보 거리를 구합니다.
    addr 행의 [6]=역 번호, [7]=도보 m (채워 넣음). 도로 자료가 없으면 -1."""
    import numpy as np
    pos, adj = walk_graph(raw)
    all_ids = list(pos)
    all_xy = np.array([pos[n] for n in all_ids])

    pts, owners = [], []
    for i, st in enumerate(stations):
        for lat, lng in st["pts"]:
            pts.append(to_m(lat, lng)); owners.append(i)
    d0, near = nearest_nodes(all_xy, np.array(pts))

    best, label, heap = {}, {}, []
    for snap, j, owner in zip(d0, near, owners):
        node = all_ids[int(j)]
        if snap < best.get(node, 1e18):
            best[node] = float(snap); label[node] = owner
            heapq.heappush(heap, (float(snap), node))
    while heap:
        d, u = heapq.heappop(heap)
        if d > best[u]:
            continue
        for v, w in adj.get(u, ()):
            nd = d + w
            if nd < best.get(v, 1e18):
                best[v] = nd; label[v] = label[u]
                heapq.heappush(heap, (nd, v))

    reached = list(best)                        # 끊어진 작은 길 조각은 빼고, 역과 이어진 길에만 붙입니다
    reached_xy = np.array([pos[n] for n in reached])
    query = np.array([to_m(37.5 + a[2] / 1e5, 126.85 + a[3] / 1e5) for a in addr])
    snap, idx = nearest_nodes(reached_xy, query)
    for a, s, j in zip(addr, snap, idx):
        node = reached[int(j)]
        del a[6:]
        a += [label[node], round(best[node] + float(s))]
    return addr


# ── 지도용 도로 (좌표 압축 + 단순화) ─────────────────────────────
def simplify(pts, tol=2.0):
    """Douglas-Peucker. pts는 압축 정수 좌표(1 단위 약 1m), tol 단위도 같습니다."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ya, xa = pts[a]; yb, xb = pts[b]
        dy, dx = yb - ya, xb - xa
        norm = math.hypot(dy, dx) or 1.0
        far, at = 0.0, -1
        for i in range(a + 1, b):
            d = abs(dy * (pts[i][1] - xa) - dx * (pts[i][0] - ya)) / norm
            if d > far:
                far, at = d, i
        if far > tol:
            keep[at] = True
            stack += [(a, at), (at, b)]
    return [p for p, k in zip(pts, keep) if k]


def road_lines(raw, compress):
    """{"maj": [[y0,x0,dy,dx,...],...], "min": [...], "svc": [...]}  첫 점은 절대값, 다음부터는 앞 점과의 차이."""
    out = {"maj": [], "min": [], "svc": []}          # svc: 한국 골목은 OSM에서 service로 많이 분류됩니다
    for kind, _, _, geom in raw["ways"]:
        group = "maj" if kind in DRAW_MAJOR else "min" if kind in DRAW_MINOR else "svc" if kind == "service" else None
        if not group:
            continue
        pts = simplify([compress(lat, lng) for lat, lng in geom])
        flat = list(pts[0])
        for (y0, x0), (y1, x1) in zip(pts, pts[1:]):
            flat += [y1 - y0, x1 - x0]
        out[group].append(flat)
    return out


def build(raw, compress, addr):
    """반환: (stations, roads). stations = [[이름, 위도압축, 경도압축], ...]. addr 행에 역 번호·도보 m를 채웁니다."""
    stations = station_points(raw)
    walk_distances(raw, stations, addr)
    table = [[st["name"], *compress(st["lat"], st["lng"])] for st in stations]
    return table, road_lines(raw, compress)


if __name__ == "__main__":
    fetch()
