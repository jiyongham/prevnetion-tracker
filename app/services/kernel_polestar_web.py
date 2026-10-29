"""Read OS patch levels from the authenticated Polestar configuration grid."""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from app.config import settings

logger = logging.getLogger(__name__)

_PAGE_RE = re.compile(r"configuration\.grid\.pager/(\d+)")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_NULLS = {"", "-", "null", "none", "nan", "n/a"}
# 파드 메모리 제한 안에서 돌도록 Chromium 부가 프로세스와 그리드 판독에 필요 없는 리소스를 끈다.
# 스타일시트는 :visible 판정에 쓰이므로 막지 않는다.
_BROWSER_ARGS = [
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--mute-audio",
    "--no-first-run",
    "--renderer-process-limit=1",
]
_BLOCKED_RESOURCES = {"image", "media", "font"}


def _text(value: object) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in _NULLS else text


def parse_configuration_grid(html: str) -> list[dict[str, str]]:
    """Parse rendered Wicket grid HTML into name/IP/patchlevel rows."""
    soup = BeautifulSoup(html, "html.parser")
    output: list[dict[str, str]] = []
    for tr in soup.select("table tbody tr:has(td[data-grid-property='patchlevel'])"):
        cells = {
            str(td.get("data-grid-property", "")).strip().lower(): _text(td.get_text(" ", strip=True))
            for td in tr.select("td[data-grid-property]")
        }
        if cells:
            output.append({
                "name": cells.get("name", ""),
                "ipaddress": cells.get("ipaddress", ""),
                "patchlevel": cells.get("patchlevel", ""),
            })
    return output


def _max_page(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    pages = [int(match.group(1)) for link in soup.select("ul.pagination a[href]")
             if (match := _PAGE_RE.search(str(link.get("href", ""))))]
    return max(pages, default=1)


def _source_from_records(records: list[dict[str, str]], page_count: int = 0) -> dict:
    levels: dict[str, str] = {}
    conflicts: set[str] = set()
    rows_with_level = 0
    rows_with_ip = 0
    for record in records:
        level = _text(record.get("patchlevel", ""))
        if not level:
            continue
        rows_with_level += 1
        ips = set(_IP_RE.findall(_text(record.get("ipaddress", ""))))
        if not ips:
            continue
        rows_with_ip += 1
        for ip in ips:
            if ip in conflicts:
                continue
            previous = levels.get(ip)
            if previous is None:
                levels[ip] = level
            elif previous != level:
                levels.pop(ip, None)
                conflicts.add(ip)

    return {
        "hosts": set(),
        "ips": set(levels),
        "levels": levels,
        "rows": len(levels),
        "grid_rows": len(records),
        "grid_pages": page_count,
        "rows_with_level": rows_with_level,
        "rows_with_ip": rows_with_ip,
        "ip_conflicts": len(conflicts),
        "source": "Polestar configuration web grid",
        "has_level": True,
        "source_kind": "web",
    }


def _choose_username_input(page):
    candidates = page.locator("input:not([type='hidden']):not([type='password']):not([type='submit']):not([type='button'])")
    preferred = re.compile(r"user|login|account|^id$", re.I)
    visible = []
    for index in range(candidates.count()):
        field = candidates.nth(index)
        if not field.is_visible():
            continue
        attrs = " ".join(str(field.get_attribute(name) or "") for name in ("name", "id", "autocomplete", "placeholder"))
        visible.append((bool(preferred.search(attrs)), field))
    if not visible:
        raise RuntimeError("Polestar 로그인 화면에서 사용자 입력란을 찾지 못했습니다")
    return next((field for is_preferred, field in visible if is_preferred), visible[0][1])


def _login(page, base_url: str, username: str, password: str) -> None:
    page.goto(urljoin(base_url, settings.kernel_polestar_web_login_path), wait_until="domcontentloaded")
    password_input = page.locator("input[type='password']:visible").first
    if password_input.count() == 0:
        raise RuntimeError("Polestar 로그인 화면에서 비밀번호 입력란을 찾지 못했습니다")
    _choose_username_input(page).fill(username)
    password_input.fill(password)

    submit = page.locator("button[type='submit']:visible, input[type='submit']:visible").first
    if submit.count():
        submit.click()
    else:
        password_input.press("Enter")
    page.wait_for_timeout(1200)


def read_patched_web_grid() -> dict:
    """Login with a service account and scrape every page of the Wicket grid.

    No credentials or browser storage state are persisted. Returns the standard
    patched-source shape; IP is used as the stable join key because the grid
    name is a display name rather than a guaranteed hostname.
    """
    username = settings.kernel_polestar_web_user
    password = settings.kernel_polestar_web_password
    if not username or not password:
        return {"hosts": set(), "ips": set(), "levels": {}, "rows": 0,
                "source": "", "has_level": False, "source_kind": ""}

    from playwright.sync_api import sync_playwright

    base = settings.polestar_url.rstrip("/")
    if not base:
        raise ValueError("polestar_url not configured")
    parsed = urlsplit(base)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("polestar_url must be an absolute URL")

    records: list[dict[str, str]] = []
    page_count = 0
    with sync_playwright() as playwright:
        if settings.kernel_polestar_browser_executable:
            browser = playwright.chromium.launch(
                headless=True,
                args=_BROWSER_ARGS,
                executable_path=settings.kernel_polestar_browser_executable,
            )
        else:
            browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            context = browser.new_context(ignore_https_errors=True)
            context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in _BLOCKED_RESOURCES
                else route.continue_(),
            )
            page = context.new_page()
            page.set_default_timeout(30000)
            _login(page, base, username, password)
            page.goto(urljoin(base, settings.kernel_polestar_web_grid_path), wait_until="domcontentloaded")
            if urlsplit(page.url).path.rstrip("/").endswith(settings.kernel_polestar_web_login_path.rstrip("/")):
                raise RuntimeError("Polestar 웹 로그인에 실패했거나 계정에 구성정보 권한이 없습니다")

            # Configuration starts with an empty grid. Open its resource-type
            # search, select servers, and submit the Wicket form before paging.
            page.get_by_role("button", name="PQL 검색").click()
            page.locator("#searchForm select[name='searchResourceType']").select_option("server.Server")
            page.locator("#searchForm button[type='submit']").click()
            page.locator("th[data-grid-property='patchlevel']").wait_for()
            page.locator("table tbody tr[data-grid-row]").first.wait_for()

            first_html = page.content()
            first_page_records = parse_configuration_grid(first_html)
            if not first_page_records:
                raise RuntimeError("Polestar 구성 그리드 첫 페이지에 행이 없습니다")
            records.extend(first_page_records)
            last_page = _max_page(first_html)
            page_count = 1
            current_page = 1
            while current_page < last_page:
                page_number = current_page + 1
                link = page.locator(f"a[href*='configuration.grid.pager/{page_number}?']").first
                link.wait_for()
                with page.expect_response(
                    lambda response: (
                        f"configuration.grid.pager/{page_number}" in response.url
                        and response.request.method == "POST"
                    )
                ):
                    link.click()
                page.wait_for_function(
                    "n => [...document.querySelectorAll('ul.pagination li.active a')].some(a => a.textContent.trim() === String(n))",
                    arg=page_number,
                )
                page_records = parse_configuration_grid(page.content())
                if not page_records:
                    raise RuntimeError(f"Polestar 구성 그리드 {page_number}페이지 응답에 행이 없습니다")
                records.extend(page_records)
                page_count += 1
                current_page = page_number
            if page_count != last_page:
                raise RuntimeError("Polestar 구성 그리드 전체 페이지 수집이 완료되지 않았습니다")
            context.close()
        finally:
            browser.close()

    return _source_from_records(records, page_count=page_count)
