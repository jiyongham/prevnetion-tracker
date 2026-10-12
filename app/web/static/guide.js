// 바로가기로 선택한 설명만 펼친다. 기본 진입에서는 모두 접어 둔다.
(() => {
  function reveal() {
    const id = window.location.hash.slice(1);
    const panel = document.getElementById(id);
    if (!panel || !panel.matches('details.guide-panel')) return;
    panel.open = true;
    panel.scrollIntoView({ block: 'start' });
  }
  window.addEventListener('hashchange', reveal);
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href^="#guide-"]');
    if (link && link.hash === window.location.hash) reveal();
  });
  reveal();
})();
