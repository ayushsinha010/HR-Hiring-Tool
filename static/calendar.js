function initCalendarModal({ triggerId, modalId, closeId, monthUrl, dayUrl }) {
  const trigger = document.getElementById(triggerId);
  const modal = document.getElementById(modalId);
  const closeBtn = document.getElementById(closeId);
  if (!trigger || !modal) return;

  const current = new Date();
  current.setDate(1);

  const MILESTONE_LABELS = {
    sourced: "Sourced",
    reached: "Reached",
    l1_cleared: "L1 Cleared",
    l2_cleared: "L2 Cleared",
    l3_cleared: "L3 Cleared",
    offer_made: "Offer Made",
    joined: "Joined",
    rejected: "Rejected",
    on_hold: "On Hold",
  };

  function pad(n) {
    return String(n).padStart(2, "0");
  }
  function toDateStr(y, m, d) {
    return `${y}-${pad(m)}-${pad(d)}`;
  }

  function showDay(dateStr) {
    modal.querySelectorAll(".calendar-cell[data-date]").forEach((c) => {
      c.classList.toggle("calendar-cell-selected", c.dataset.date === dateStr);
    });
    const summary = modal.querySelector(".calendar-day-summary");
    summary.innerHTML = "<p>Loading...</p>";
    fetch(`${dayUrl}?date=${dateStr}`)
      .then((r) => r.json())
      .then((counts) => {
        const d = new Date(dateStr + "T00:00:00");
        const label = d.toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
        const rows = Object.keys(MILESTONE_LABELS)
          .map(
            (k) =>
              `<div class="calendar-stat"><span class="calendar-stat-value">${counts[k] || 0}</span><span class="calendar-stat-label">${MILESTONE_LABELS[k]}</span></div>`
          )
          .join("");
        summary.innerHTML = `<h3>${label}</h3><div class="calendar-stats-grid">${rows}</div>`;
      });
  }

  function render() {
    const year = current.getFullYear();
    const month = current.getMonth() + 1;
    const grid = modal.querySelector(".calendar-grid");
    const title = modal.querySelector(".calendar-month-title");
    title.textContent = current.toLocaleDateString(undefined, { month: "long", year: "numeric" });

    fetch(`${monthUrl}?year=${year}&month=${month}`)
      .then((r) => r.json())
      .then((data) => {
        const activeDates = new Set(data.active_dates || []);
        const daysInMonth = new Date(year, month, 0).getDate();
        const startWeekday = (new Date(year, month - 1, 1).getDay() + 6) % 7; // Monday-first
        const now = new Date();
        const todayStr = toDateStr(now.getFullYear(), now.getMonth() + 1, now.getDate());

        let html =
          '<div class="calendar-weekdays">' +
          ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => `<span>${d}</span>`).join("") +
          '</div><div class="calendar-cells">';
        for (let i = 0; i < startWeekday; i++) html += '<span class="calendar-cell calendar-cell-empty"></span>';
        for (let d = 1; d <= daysInMonth; d++) {
          const dateStr = toDateStr(year, month, d);
          const classes = ["calendar-cell"];
          if (activeDates.has(dateStr)) classes.push("calendar-cell-active");
          if (dateStr === todayStr) classes.push("calendar-cell-today");
          html += `<button type="button" class="${classes.join(" ")}" data-date="${dateStr}">${d}</button>`;
        }
        html += "</div>";
        grid.innerHTML = html;
        grid.querySelectorAll(".calendar-cell[data-date]").forEach((cell) => {
          cell.addEventListener("click", () => showDay(cell.dataset.date));
        });
      });
  }

  trigger.addEventListener("click", () => {
    modal.hidden = false;
    modal.querySelector(".calendar-day-summary").innerHTML = "<p>Click a date to see that day's activity.</p>";
    render();
  });
  if (closeBtn) closeBtn.addEventListener("click", () => { modal.hidden = true; });
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.hidden = true; });
  modal.querySelector(".calendar-prev").addEventListener("click", () => {
    current.setMonth(current.getMonth() - 1);
    render();
  });
  modal.querySelector(".calendar-next").addEventListener("click", () => {
    current.setMonth(current.getMonth() + 1);
    render();
  });
}
