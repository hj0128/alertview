"""일회성: 활성 방문형 캠페인의 정밀 좌표를 한꺼번에 채운다(동시 요청으로 빠르게).
poller._place_backfill 과 동일 로직이나, 세마포어 동시성으로 대량 처리.
  docker compose exec app python tools/bulk_place_geo.py
"""
import asyncio
import httpx

from app import db, config
from app.poller import _venue_name, _geocode_campaign

CONC = 8            # 동시 요청 수(카카오 rate limit 여유)
BATCH = 100000      # 남은 대상 전부


async def _one(client, sem, headers, r):
    if not _venue_name(r["title"]):
        db.place_geo_set(r["site"], r["cid"], None, None)
        return 0
    async with sem:
        res = await _geocode_campaign(client, headers, r["title"], r["region"] or "")
    if res in ("AUTH", "QUOTA"):
        print("중단:", res); return -1
    if res == "ERR":
        return 0                       # 일시 오류 → 캐시 안 함(다음에 재시도)
    lat, lng, place, addr = res
    db.place_geo_set(r["site"], r["cid"], lat, lng, place, addr)
    return 1 if lat is not None else 0


async def main():
    db.init(config.DATABASE_URL or config.DB_PATH)
    rows = db.places_missing_geo(BATCH)
    print(f"대상 {len(rows)}건 지오코딩 시작(동시 {CONC})...")
    headers = {"Authorization": f"KakaoAK {config.KAKAO_REST_API_KEY}"}
    sem = asyncio.Semaphore(CONC)
    hit = 0
    async with httpx.AsyncClient(trust_env=False, timeout=15.0) as client:
        for i in range(0, len(rows), 500):
            chunk = rows[i:i + 500]
            res = await asyncio.gather(*[_one(client, sem, headers, r) for r in chunk])
            if -1 in res:
                print("인증 문제로 중단"); break
            hit += sum(x for x in res if x > 0)
            print(f"  진행 {min(i + 500, len(rows))}/{len(rows)} · 누적 좌표 {hit}건", flush=True)
    print(f"완료: 좌표 {hit}건 / 표시 가능 {len(db.active_place_points())}건")


if __name__ == "__main__":
    asyncio.run(main())
