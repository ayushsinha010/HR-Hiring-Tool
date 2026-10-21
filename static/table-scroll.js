// Shows a fade on the right edge of a wide table while there's more to
// scroll to, and hides it once you've scrolled to the end - a quiet hint
// that the table continues sideways instead of just cutting off.
function initTableScrollFade() {
  document.querySelectorAll(".table-scroll-shell").forEach((shell) => {
    const scrollEl = shell.querySelector(".tracker-table-wrap");
    if (!scrollEl) return;
    const update = () => {
      const hasMore = scrollEl.scrollWidth - scrollEl.clientWidth - scrollEl.scrollLeft > 4;
      shell.classList.toggle("is-scrollable", hasMore);
    };
    scrollEl.addEventListener("scroll", update);
    window.addEventListener("resize", update);
    update();
  });
}

document.addEventListener("DOMContentLoaded", initTableScrollFade);
