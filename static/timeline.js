// "Average days per stage" graph of the Hiring Timeline and Buffer Time section. The data comes from
// the section itself (a JSON script tag), so the same code draws it on the overall dashboard
// (all positions) and on a position's own screen (that position only).
(function () {
  function init() {
    var holder = document.getElementById("avg-days-data");
    var canvas = document.getElementById("avg-days-stage-chart");
    if (!holder || !canvas || typeof Chart === "undefined") return;
    var rows;
    try { rows = JSON.parse(holder.textContent); } catch (e) { return; }
    if (!rows || !rows.length) return;
    new Chart(canvas, {
      type: "bar",
      data: {
        labels: rows.map(function (r) { return r.phase; }),
        datasets: [{ data: rows.map(function (r) { return r.avg_days; }), backgroundColor: "#0096AF", borderRadius: 4 }],
      },
      options: {
        indexAxis: "y",
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: function (ctx) { return ctx.parsed.x + " days on average (" + rows[ctx.dataIndex].count + " stay(s))"; } } },
        },
        scales: { x: { beginAtZero: true, ticks: { font: { size: 9 } } }, y: { ticks: { font: { size: 9 } } } },
      },
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
