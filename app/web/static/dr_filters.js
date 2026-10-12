// 검색어와 사용자 이름은 저장하지 않는다. 명시적인 URL이 저장된 필터보다 우선한다.
(() => {
  const key = "prevention-tracker.dr.filters.v1";
  const allowed = ["half", "mode", "team", "company", "service", "status", "action", "compare"];
  const checkbox = document.getElementById("rememberDrFilters");
  const reset = document.getElementById("resetDrFilters");
  if (!checkbox || !reset) return;
  const url = new URL(window.location.href);
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(key) || "null");
  } catch (error) {
    console.warn("DR 필터 저장값을 읽을 수 없습니다.", error);
  }
  checkbox.checked = Boolean(saved && saved.enabled);
  function store() {
    const filters = {};
    for (const name of allowed) {
      if (url.searchParams.has(name)) filters[name] = url.searchParams.get(name);
    }
    try {
      if (checkbox.checked) localStorage.setItem(key, JSON.stringify({enabled: true, filters}));
      else localStorage.removeItem(key);
    } catch (error) {
      checkbox.checked = false;
      console.warn("브라우저가 필터 저장을 허용하지 않습니다.", error);
    }
  }
  // 쿼리가 전혀 없는 최초 진입에서만 복원한다. 탭의 의도적인 필터 해제를 존중한다.
  if (checkbox.checked && !url.search && saved.filters && typeof saved.filters === "object") {
    for (const name of allowed) {
      const value = saved.filters[name];
      if (typeof value === "string" && value.length <= 200 && value) url.searchParams.set(name, value);
    }
    if (url.search) {
      window.location.replace(url.pathname + url.search);
      return;
    }
  }
  checkbox.addEventListener("change", store);
  reset.addEventListener("click", () => {
    checkbox.checked = false;
    store();
    // 저장 접근이 차단되어도 명시적 URL로 이동하므로 복원 루프가 발생하지 않는다.
    window.location.assign("/dr?half=" + encodeURIComponent(document.getElementById("half").value));
  });
  if (checkbox.checked) store();
})();
