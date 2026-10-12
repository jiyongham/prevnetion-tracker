// 메뉴 표시만 제어한다. API 권한 검사에는 사용하지 않는다.
(() => {
  const nav = document.querySelector(".dr-subnav");
  const input = document.getElementById("updatedBy");
  if (!nav || !input) return;
  const admins = JSON.parse(nav.dataset.admins || "[]");
  try {
    if (!input.value) input.value = localStorage.getItem("updatedBy") || "";
  } catch (error) {
    console.warn("입력자 저장값을 불러오지 못했습니다.", error);
  }
  const update = () => nav.classList.toggle("dr-nav-authorized", admins.includes(input.value.trim()));
  input.addEventListener("input", update);
  input.addEventListener("change", update);
  update();
})();
