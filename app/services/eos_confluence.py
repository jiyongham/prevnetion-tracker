# app/services/eos_confluence.py
"""
EoS 차주 계획을 JIRA 대신 Confluence 주간 작업계획 페이지에서 파악.
JIRA 변경계획일이 아직 안 잡힌(티켓 미생성) 작업도 이 페이지엔 미리 올라오는 경우가 많아,
JIRA 기반 집계로는 놓치는 차주 계획을 보완한다.

페이지 구조 (실측):
- 부모 페이지 밑에 주(週) 단위 형제 페이지가 있고, 제목이 'YYYY.MM.DD. ~ MM.DD.' 형식으로
  그 주의 월~금 날짜 범위를 나타낸다.
- 각 페이지 안 표의 '작업 계획' 컬럼(4번째 셀)에 "[작업구분][회사구분] 시스템명 ... 작업 (날짜)"
  형태로 작업이 한 줄씩 적혀 있다.
"""
import difflib
import io
import re
from collections import Counter
from datetime import date

import pdfplumber
from bs4 import BeautifulSoup

from app.core.confluence_client import confluence

# 작업행 텍스트 <-> 첨부파일(작업계획서 PDF) 제목 매칭 유사도 임계값.
# 둘 다 같은 작업을 가리키는 텍스트라 공백과 날짜 표기 정도만 다르고 거의 일치하므로,
# 이 정도로 높게 잡아도 실제 매칭엔 문제없고 엉뚱한 첨부는 걸러진다.
ATTACHMENT_MATCH_MIN_RATIO = 0.85

_DATE_RANGE_RE = re.compile(
    r"(\d{4})\.(\d{1,2})\.(\d{1,2})\.?\s*~\s*(?:\d{4}\.)?(\d{1,2})\.(\d{1,2})\.?"
)
_LEADING_TAG_RE = re.compile(r"^\[[^\]]*\]\s*")

MIN_TOKEN_LEN = 2
# 'DB'/'개발'/'서버'/'WEB'처럼 여러 시스템명에 공통으로 들어가는 단어는 매칭 근거로 쓰면
# 이름이 비슷한 다른 시스템에 잘못 붙는다. 대상 목록 안에서 이 값 이하로만 등장하는
# (= 이름을 특정할 수 있는) 토큰만 매칭에 쓴다.
MAX_TOKEN_DOC_FREQ = 3
MIN_MATCH_TOKENS = 1


def _parse_title_range(title: str) -> tuple[date, date] | None:
    m = _DATE_RANGE_RE.search(title or "")
    if not m:
        return None
    y, m1, d1, m2, d2 = m.groups()
    year = int(y)
    start = date(year, int(m1), int(d1))
    end = date(year, int(m2), int(d2))
    if end < start:  # 연말~연초 걸치는 주 대비
        end = date(year + 1, int(m2), int(d2))
    return start, end


def find_week_page(parent_page_id: str, week_start: date, week_end: date) -> dict | None:
    """parent_page_id 밑의 주간 페이지들 중 제목의 날짜범위가 week_start~week_end와 정확히 일치하는 페이지.
    아직 그 주 페이지가 안 만들어졌으면 None."""
    for child in confluence.get_children(parent_page_id):
        rng = _parse_title_range(child.get("title", ""))
        if rng == (week_start, week_end):
            return child
    return None


def extract_eos_rows(page_id: str) -> list[dict]:
    """
    페이지 안 작업표에서 IP전환 작업 행 추출 ('[예방1]' 태그는 요구하지 않음 - 태그 없이
    등록된 IP전환 작업도 있어서, 실제 EoS 대상 여부는 이후 호스트명 매칭 단계에서 우리
    대상 목록과 겹치는지로 가려낸다).
    """
    body = confluence.get_content(page_id, expand="body.storage")
    storage = body.get("body", {}).get("storage", {}).get("value", "")
    soup = BeautifulSoup(storage, "html.parser")

    rows = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 4:
                continue
            plan = cells[3].get_text(" ", strip=True)
            if "IP전환" not in plan and "IP 전환" not in plan:
                continue
            worker = cells[2].get_text(" ", strip=True)
            rows.append({"worker": worker, "text": plan})
    return rows


def _name_tokens(system_name: str) -> list[str]:
    """'[DR,A][관계사,인프라] 서비스명 개발 #2' -> ['서비스명','개발']  (대괄호 접두어 전부/번호·짧은 토큰 제거)"""
    name = system_name or ""
    while True:
        stripped = _LEADING_TAG_RE.sub("", name)
        if stripped == name:
            break
        name = stripped
    return [t for t in re.split(r"\s+", name) if len(t) >= MIN_TOKEN_LEN and not t.startswith("#")]


def _row_tokens(text: str) -> set[str]:
    """
    작업계획 텍스트 -> 매칭용 토큰 집합. 대괄호 태그(작업 구분·회사 구분 등)는 전부 제거하고
    (그대로 두면 회사 구분 한 단어만으로 같은 회사의 다른 시스템끼리 오매칭됨) 공백/구두점으로 분리.
    부분 문자열이 아니라 정확히 일치하는 토큰만 매칭 근거로 쓰기 위해 item 토큰과 세트로 비교한다
    (안 그러면 짧은 일반명사가 더 긴 시스템명 안에 우연히 포함돼 오매칭됨).
    """
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = text.replace("■", " ")
    return {t for t in re.split(r"[\s()~,./:]+", text) if t}


def match_eos_rows(rows: list[dict], items: list[dict]) -> dict:
    """
    Confluence EoS 작업 행을 EoS 대상 항목(system_name)과 매칭.
    'DB'/'개발'/'서버'처럼 여러 항목에 공통으로 나오는 토큰은 제외하고, 이 항목군 안에서
    드물게(<= MAX_TOKEN_DOC_FREQ번) 등장하는 - 즉 이름을 특정할 수 있는 - 토큰만 근거로 쓴다.
    반환: {"matched": {item_no: row}, "unmatched_rows": [row, ...]}
    """
    item_tokens = {item["item_no"]: set(_name_tokens(item.get("system_name", ""))) for item in items}
    doc_freq = Counter(tok for tokens in item_tokens.values() for tok in tokens)

    matched: dict[str, dict] = {}
    unmatched: list[dict] = []

    for row in rows:
        row_tokens = _row_tokens(row["text"])
        scored = []
        for item in items:
            distinctive = {t for t in item_tokens[item["item_no"]] if doc_freq[t] <= MAX_TOKEN_DOC_FREQ}
            score = len(distinctive & row_tokens)
            if score >= MIN_MATCH_TOKENS:
                scored.append((score, item["item_no"]))
        scored.sort(key=lambda x: -x[0])

        # 1등 점수가 유일해야(2등과 동점이 아니어야) 확정 매칭 - 애매하면 억지로 찍지 않고 미매칭 처리
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            matched[scored[0][1]] = row
        else:
            unmatched.append(row)

    return {"matched": matched, "unmatched_rows": unmatched}


def _normalize_title(text: str) -> str:
    """공백/날짜 구분자(/,공백 등) 표기 차이를 무시하고 비교하기 위해 문자/한글만 남긴다."""
    text = re.sub(r"\.pdf$", "", text or "", flags=re.I)
    text = text.lstrip("■").strip()
    return re.sub(r"[^\w가-힣]", "", text)


def find_attachment_for_row(page_id: str, row_text: str) -> dict | None:
    """작업행 텍스트와 제목이 가장 비슷한 첨부파일(작업계획서 PDF) 찾기."""
    target = _normalize_title(row_text)
    best = None
    best_ratio = 0.0
    for a in confluence.get_attachments(page_id):
        ratio = difflib.SequenceMatcher(None, target, _normalize_title(a.get("title", ""))).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = a
    return best if best_ratio >= ATTACHMENT_MATCH_MIN_RATIO else None


_HOSTNAME_HEADERS = {"호스트명", "hostname", "host name", "host"}


def extract_hostnames_from_pdf(pdf_bytes: bytes) -> set[str]:
    """
    작업계획서 PDF의 '변경작업 대상' 표(호스트명 컬럼이 있는 표)에서 호스트명만 추출.
    작성자마다 헤더를 '호스트명'(국문) 또는 'Hostname'(영문)으로 다르게 쓰는 걸 확인해서
    둘 다 인식한다.
    """
    hostnames = set()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table or not table[0]:
                    continue
                header = [(h or "").strip().lower() for h in table[0]]
                if not any(h in _HOSTNAME_HEADERS for h in header):
                    continue
                idx = next(i for i, h in enumerate(header) if h in _HOSTNAME_HEADERS)
                for row in table[1:]:
                    if idx < len(row) and row[idx]:
                        # 셀 안에서 줄바꿈된 긴 호스트명은 pdfplumber가 '\n'을 그대로 남긴다
                        # (예: 'hostname-svc-\ndev2' -> 'hostname-svc-dev2')
                        cleaned = re.sub(r"\s+", "", row[idx])
                        if cleaned:
                            hostnames.add(cleaned)
    return hostnames


def match_eos_rows_by_hostname(page_id: str, rows: list[dict], items: list[dict]) -> dict:
    """
    작업행 -> 첨부 작업계획서 PDF -> '변경작업 대상' 표의 호스트명 -> EoS 대상 hostname 완전일치.
    시스템명 토큰 매칭(match_eos_rows)보다 훨씬 정확하지만, 첨부 PDF가 없거나 표 형식이 다르면
    (no_pdf_rows) 매칭이 안 될 수 있다 - 그럴 땐 match_eos_rows로 보완한다.
    한 작업행(PDF)에 호스트가 여러 대(클러스터 등) 걸릴 수 있어 item_no가 여러 개 매칭될 수 있다.
    반환: {"matched": {item_no: row}, "unmatched_rows": [...], "no_pdf_rows": [...]}
    """
    host_index = {
        (item.get("hostname") or "").strip().lower(): item["item_no"]
        for item in items if item.get("hostname")
    }

    matched: dict[str, dict] = {}
    unmatched: list[dict] = []
    no_pdf_rows: list[dict] = []

    for row in rows:
        attachment = find_attachment_for_row(page_id, row["text"])
        if not attachment:
            no_pdf_rows.append(row)
            continue

        try:
            pdf_bytes = confluence.download_attachment(attachment)
            hostnames = extract_hostnames_from_pdf(pdf_bytes)
        except Exception:
            # 손상되었거나 표 형식이 파싱 불가한 PDF - 토큰 매칭으로 보완하도록 넘긴다
            no_pdf_rows.append(row)
            continue

        hit_any = False
        for h in hostnames:
            item_no = host_index.get(h.lower())
            if item_no:
                matched[item_no] = row
                hit_any = True
        if not hit_any:
            unmatched.append(row)

    return {"matched": matched, "unmatched_rows": unmatched, "no_pdf_rows": no_pdf_rows}


def match_eos_rows_combined(page_id: str, rows: list[dict], items: list[dict]) -> dict:
    """
    호스트명 매칭(정확, 작업계획서 PDF의 '변경작업 대상' 표 기반)을 우선 적용하고,
    첨부 PDF가 없거나 표에서 호스트명을 못 찾은 행만 시스템명 토큰 매칭으로 보완한다.
    반환: {"matched": {item_no: row}, "unmatched_rows": [row, ...]}
    """
    host_result = match_eos_rows_by_hostname(page_id, rows, items)
    matched = dict(host_result["matched"])

    leftover_rows = host_result["unmatched_rows"] + host_result["no_pdf_rows"]
    if not leftover_rows:
        return {"matched": matched, "unmatched_rows": []}

    token_result = match_eos_rows(leftover_rows, items)
    for item_no, row in token_result["matched"].items():
        matched.setdefault(item_no, row)

    return {"matched": matched, "unmatched_rows": token_result["unmatched_rows"]}


def get_week_plan_count(parent_page_id: str, week_start: date, week_end: date, items: list[dict]) -> dict:
    """
    특정 주(week_start~week_end)의 Confluence 계획을 EoS 대상과 매칭해 건수까지 계산.
    아직 그 주 페이지가 없으면 found=False로 표시 (아직 작성 전 - 정상적인 상황).

    extract_eos_rows가 '[예방1]' 태그를 요구하지 않게 되면서 IP전환 작업행에는 EoS와
    무관한 작업(DR훈련 등)도 섞여 들어온다 - 그래서 "count"(차주 계획 N대)는 행 개수가
    아니라 실제로 EoS 대상 호스트와 매칭된 항목 수로 센다. row_count는 참고용(스캔한
    IP전환 작업행 총수, EoS 외 포함)으로 같이 남긴다.
    """
    page = find_week_page(parent_page_id, week_start, week_end)
    if not page:
        return {"found": False, "matched": {}, "unmatched_rows": [], "count": 0, "row_count": 0}

    rows = extract_eos_rows(page["id"])
    result = match_eos_rows_combined(page["id"], rows, items)
    return {
        "found": True,
        "page_id": page["id"],
        "page_title": page["title"],
        "matched": result["matched"],
        "unmatched_rows": result["unmatched_rows"],
        "count": len(result["matched"]),
        "row_count": len(rows),
    }
