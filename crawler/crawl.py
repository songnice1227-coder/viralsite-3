"""
Oppor 플랫폼 자동 크롤러
- 위비티(wevity.com) 대학생 디자인 공모전/인턴 공고 수집
- 콘테스트코리아(contestkorea.com) 디자인 공모전 수집
- 링커리어(linkareer.com) 대외활동/공모전 수집
- Claude Haiku API로 정형화
- Firebase Firestore (config/opps) 자동 업데이트
"""

import os
import re
import json
import time
import requests
import anthropic
import firebase_admin
from firebase_admin import credentials, firestore
from bs4 import BeautifulSoup
from datetime import datetime, date

# ── 설정 ──────────────────────────────────────────────────────────────────────

WEVITY_BASE = "https://www.wevity.com"
CONTESTKOREA_BASE = "https://www.contestkorea.com"
LINKAREER_API = "https://api.linkareer.com/graphql"
LINKAREER_BASE = "https://linkareer.com"
# 링커리어 persisted query hash (ActivityList_Activities)
LINKAREER_HASH = "2c08975fee8ab40c8a099c9a78adf8c4a9da63ce605f2cce4e49d4b26c99eba4"

# 위비티 카테고리별 URL (대학생 공모전 / 인턴·대외활동)
WEVITY_PAGES = [
    # 대학생 공모전 (디자인 분야 위주)
    f"{WEVITY_BASE}/index_university.php?c=find&s=_university&gub=1&cidx=25",
    # 대학생 대외활동·인턴
    f"{WEVITY_BASE}/index_university.php?c=find&s=_university&gub=3&cidx=25",
]

# 콘테스트코리아 디자인 분야 URL
CONTESTKOREA_PAGES = [
    # 디자인 공모전
    f"{CONTESTKOREA_BASE}/sub/list.php?int_gbn=1&Txt_bcode=031210001",
    # 광고·마케팅 (브랜딩 등 겹침)
    f"{CONTESTKOREA_BASE}/sub/list.php?int_gbn=1&Txt_bcode=031210002",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# 디자인 관련 키워드 — 이 단어가 제목에 없으면 건너뜀
DESIGN_KEYWORDS = [
    "디자인", "design", "그래픽", "graphic",
    "영상", "모션", "일러스트", "타이포", "브랜딩",
    "ux", "ui", "포스터", "패키지", "시각",
    "미디어", "아트", "art", "캐릭터",
    "인턴", "intern", "서포터즈",
]

# ── Firebase 초기화 ────────────────────────────────────────────────────────────

def init_firebase():
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not sa_json:
        raise ValueError("FIREBASE_SERVICE_ACCOUNT 환경변수가 없습니다.")
    cred = credentials.Certificate(json.loads(sa_json))
    firebase_admin.initialize_app(cred)
    return firestore.client()

# ── 크롤링 ────────────────────────────────────────────────────────────────────

def is_design_related(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in DESIGN_KEYWORDS)

def parse_deadline(text: str) -> str:
    """
    다양한 형태의 날짜 문자열에서 YYYY-MM-DD 추출.
    예) '2026-04-05', '2026.04.05', '~2026-04-05 17:00'
    """
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if m:
        y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        return f"{y}-{mo}-{d}"
    return ""

def get_page_html(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.encoding = "utf-8"
        return resp.text
    except Exception as e:
        print(f"  [WARN] 페이지 로드 실패: {url} — {e}")
        return ""

def scrape_wevity_page(url: str) -> list[dict]:
    """위비티 목록 페이지에서 공고 기본 정보 수집"""
    html = get_page_html(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if "ix=" not in href:
            continue

        full_url = href if href.startswith("http") else WEVITY_BASE + "/" + href.lstrip("/")

        title_el = a_tag.select_one(".tit, h6, .title, strong")
        raw_title = title_el.get_text(" ", strip=True) if title_el else a_tag.get_text(" ", strip=True)

        if not raw_title or len(raw_title) < 4:
            continue
        if not is_design_related(raw_title):
            continue
        if any(r["link"] == full_url for r in results):
            continue

        results.append({"title": raw_title, "link": full_url})

    return results


def scrape_contestkorea_page(url: str) -> list[dict]:
    """콘테스트코리아 목록 페이지에서 공고 기본 정보 수집"""
    html = get_page_html(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        # 상세 페이지 패턴: str_no= 파라미터 포함
        if "str_no=" not in href:
            continue

        full_url = href if href.startswith("http") else CONTESTKOREA_BASE + "/sub/" + href.lstrip("/")

        raw_title = a_tag.get_text(" ", strip=True)
        if not raw_title or len(raw_title) < 4:
            continue
        if not is_design_related(raw_title):
            continue
        if any(r["link"] == full_url for r in results):
            continue

        results.append({"title": raw_title, "link": full_url})

    return results

def scrape_linkareer() -> list[dict]:
    """링커리어 GraphQL API로 공고 목록 수집"""
    import urllib.parse

    results = []

    # activityTypeID: 1=대외활동, 2=공모전 — 둘 다 수집
    for type_id in ["1", "2"]:
        variables = {
            "filterBy": {
                "status": "OPEN",
                "activityTypeID": type_id,
                "simpleApplyFilter": None
            },
            "pageSize": 30,
            "page": 1,
            "activityOrder": {"field": "CREATED_AT", "direction": "DESC"}
        }
        extensions = {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": LINKAREER_HASH
            }
        }
        params = {
            "operationName": "ActivityList_Activities",
            "variables": json.dumps(variables, ensure_ascii=False),
            "extensions": json.dumps(extensions, ensure_ascii=False),
        }

        try:
            resp = requests.get(
                LINKAREER_API,
                params=params,
                headers={**HEADERS, "Accept": "application/json"},
                timeout=15
            )
            data = resp.json()
            nodes = data.get("data", {}).get("activities", {}).get("nodes", [])
        except Exception as e:
            print(f"  [WARN] 링커리어 API 오류 (type={type_id}): {e}")
            continue

        for node in nodes:
            title = node.get("title", "").strip()
            node_id = node.get("id", "")
            if not title or not node_id:
                continue
            if not is_design_related(title):
                continue
            link = f"{LINKAREER_BASE}/activity/{node_id}"
            if any(r["link"] == link for r in results):
                continue
            results.append({"title": title, "link": link})

        time.sleep(2)

    print(f"    → 링커리어 {len(results)}개 공고 발견")
    return results


def scrape_detail_page(url: str) -> str:
    """상세 페이지의 텍스트 전체 반환 (Claude에 넘길 원문)"""
    html = get_page_html(url)
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")

    # 불필요한 태그 제거
    for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    # 본문 영역 추출 시도
    body = (
        soup.find(class_="content-wrap")
        or soup.find(id="content")
        or soup.find("main")
        or soup.body
    )
    text = (body or soup).get_text("\n", strip=True)

    # 너무 길면 앞 3000자만
    return text[:3000]

# ── Claude API로 데이터 정형화 ─────────────────────────────────────────────────

def enrich_with_claude(client: anthropic.Anthropic, raw_title: str, raw_text: str, link: str) -> dict | None:
    """Claude Haiku로 비정형 텍스트 → 정형 JSON 변환"""

    today = date.today().isoformat()

    prompt = f"""아래는 대학생 공모전/인턴십 상세 페이지에서 추출한 텍스트입니다.
이 정보를 바탕으로 Oppor 플랫폼용 JSON 데이터를 생성해주세요.
오늘 날짜: {today}

--- 공고 원문 ---
제목: {raw_title}
링크: {link}

{raw_text}
--- 끝 ---

아래 JSON 스키마를 **정확히** 따라서 JSON 객체 하나만 반환하세요.
다른 텍스트나 설명은 절대 포함하지 마세요.

{{
  "title": "공고 제목 (한국어)",
  "company": "주최/주관 기관명",
  "category": "contest | intern | startup | exp | parttime 중 하나",
  "field": "그래픽 | UI/UX | 브랜딩 | 타이포 | 제품 | 모션 | 기타 중 하나",
  "deadline": "YYYY-MM-DD (마감일, 불명확하면 오늘로부터 60일 후)",
  "difficulty": 1,
  "estimatedTime": "예상 작업 기간 (예: 2~3주 작업)",
  "portfolio": "none | optional | required 중 하나",
  "isNew": true,
  "recommend": false,
  "grade": [1, 2, 3, 4],
  "shortDesc": "한두 줄 핵심 요약 (50자 이내)",
  "description": "공고 설명 3~5문장",
  "why": "지원해야 하는 이유 (2~3문장)",
  "forWho": ["대상 설명 1", "대상 설명 2"],
  "prepare": ["준비물/제출물 1", "준비물/제출물 2"],
  "portfolioValue": "포트폴리오 활용 가치 설명 (1~2문장)",
  "skills": ["관련 스킬1", "관련 스킬2"],
  "applicationSteps": ["지원 단계1", "지원 단계2", "지원 단계3"],
  "difficultyDesc": "난이도 설명 (1문장)",
  "requiredSkills": ["필수 스킬1", "필수 스킬2"],
  "workScope": "작업 범위 설명 (1~2문장)",
  "fitGood": ["이런 사람에게 좋아요 1", "이런 사람에게 좋아요 2"],
  "fitBad": ["이런 사람에게 안 맞아요 1"],
  "fitBadge": "대상 뱃지 (예: 전학년 가능 / 2~3학년 추천 / 1~2학년 강추)",
  "tags": ["태그1", "태그2", "태그3"]
}}"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_json = message.content[0].text.strip()

        # 코드블록 제거
        raw_json = re.sub(r"^```[a-z]*\n?", "", raw_json)
        raw_json = re.sub(r"\n?```$", "", raw_json)

        return json.loads(raw_json)
    except json.JSONDecodeError as e:
        print(f"  [WARN] JSON 파싱 실패: {e}")
        return None
    except anthropic.APIError as e:
        print(f"  [WARN] Claude API 오류: {e}")
        return None

# ── Firestore 업데이트 ─────────────────────────────────────────────────────────

def load_current_opps(db) -> tuple[list[dict], set[str], int]:
    """Firestore에서 현재 opps 목록 불러오기"""
    doc = db.collection("config").document("opps").get()
    current = doc.to_dict().get("list", []) if doc.exists else []
    existing_links = {item.get("link", "") for item in current}
    max_id = max((item.get("id", 0) for item in current), default=100)
    return current, existing_links, max_id

def save_opps(db, opps_list: list[dict]):
    db.collection("config").document("opps").set({"list": opps_list})

# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().isoformat()}] 크롤러 시작")

    # 1. 초기화
    db = init_firebase()
    anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # 2. 현재 Firestore 데이터 로드
    current_list, existing_links, max_id = load_current_opps(db)
    print(f"  현재 등록 공고 수: {len(current_list)}")

    # 3. 크롤링
    all_raw = []

    # 위비티
    for page_url in WEVITY_PAGES:
        print(f"  [위비티] 크롤링 중: {page_url}")
        items = scrape_wevity_page(page_url)
        print(f"    → {len(items)}개 공고 발견")
        all_raw.extend(items)
        time.sleep(2)

    # 콘테스트코리아
    for page_url in CONTESTKOREA_PAGES:
        print(f"  [콘테스트코리아] 크롤링 중: {page_url}")
        items = scrape_contestkorea_page(page_url)
        print(f"    → {len(items)}개 공고 발견")
        all_raw.extend(items)
        time.sleep(2)

    # 링커리어
    print(f"  [링커리어] GraphQL API 크롤링 중...")
    items = scrape_linkareer()
    all_raw.extend(items)

    # 4. 새 항목만 필터링
    new_raw = [r for r in all_raw if r["link"] not in existing_links]
    print(f"  신규 공고 후보: {len(new_raw)}개")

    if not new_raw:
        print("  새로운 공고 없음. 종료.")
        return

    # 5. 상세 크롤링 + Claude 정형화
    new_items = []
    for raw in new_raw[:10]:  # 한 번에 최대 10개 (API 비용 절약)
        print(f"  처리 중: {raw['title'][:40]}...")
        detail_text = scrape_detail_page(raw["link"])
        time.sleep(1)

        enriched = enrich_with_claude(
            anthropic_client,
            raw["title"],
            detail_text,
            raw["link"],
        )

        if enriched is None:
            print("    → 정형화 실패, 건너뜀")
            continue

        # 필수 필드 보정
        enriched.setdefault("id", max_id + 1)
        enriched["id"] = max_id + 1
        enriched["link"] = raw["link"]
        enriched["isNew"] = True
        enriched["crawledAt"] = datetime.now().isoformat()

        # deadline 보정
        if not enriched.get("deadline") or not re.match(r"\d{4}-\d{2}-\d{2}", enriched.get("deadline", "")):
            enriched["deadline"] = ""

        new_items.append(enriched)
        max_id += 1
        print(f"    → 추가 완료: {enriched['title']}")
        time.sleep(1)  # API 레이트리밋 방지

    # 6. Firestore 업데이트
    if new_items:
        updated = current_list + new_items
        save_opps(db, updated)
        print(f"\n완료: {len(new_items)}개 공고 추가됨 (총 {len(updated)}개)")
    else:
        print("\n추가된 공고 없음.")

    print(f"[{datetime.now().isoformat()}] 크롤러 종료")

if __name__ == "__main__":
    main()
