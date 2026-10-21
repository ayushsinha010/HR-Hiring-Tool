// Score breakdown popup: click a resume's score (anything marked data-score-breakdown="<candidate id>")
// to see HOW it scored - the parameters, their weights, the score on each and the evidence - in a
// small card anchored next to the score. The data is fetched lazily from
// /candidates/<id>/score-breakdown.json when the score is clicked. Only one popup exists at a time;
// it closes on click-outside, Esc, or the close button. All text is inserted with textContent
// (never innerHTML), so nothing the model wrote can inject markup.
(function () {
  const BUCKET_LABELS = { green: "Best fit", yellow: "Needs review", red: "Not a fit", unscored: "Not scored yet" };
  let pop = null;
  let anchorEl = null;
  let requestToken = 0;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function bucketForScore(score) {
    if (score == null) return "unscored";
    if (score > 6) return "green";
    if (score >= 4) return "yellow";
    return "red";
  }

  function closePopup(restoreFocus) {
    requestToken += 1;
    if (pop) {
      pop.remove();
      pop = null;
    }
    if (restoreFocus === true && anchorEl && document.contains(anchorEl)) {
      try { anchorEl.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
    }
    anchorEl = null;
  }

  function place() {
    if (!pop || !anchorEl || !document.contains(anchorEl)) return;
    const margin = 8;
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    const rect = anchorEl.getBoundingClientRect();
    const popW = pop.offsetWidth;
    const popH = pop.offsetHeight;
    let left = rect.left;
    if (left + popW > vw - margin) left = vw - popW - margin;
    left = Math.max(margin, left);
    let top = rect.bottom + 6;
    if (top + popH > vh - margin) {
      const above = rect.top - popH - 6;
      top = above >= margin ? above : Math.max(margin, vh - popH - margin);
    }
    pop.style.left = left + window.scrollX + "px";
    pop.style.top = top + window.scrollY + "px";
  }

  function buildHead(name) {
    const head = el("div", "score-pop-head");
    head.appendChild(el("div", "score-pop-name", name || "Candidate"));
    const close = el("button", "score-pop-close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Close score breakdown");
    close.addEventListener("click", () => closePopup(true));
    head.appendChild(close);
    return head;
  }

  function listSection(title, items) {
    const wrap = document.createDocumentFragment();
    wrap.appendChild(el("h4", null, title));
    const ul = el("ul");
    if (!items || !items.length) {
      ul.appendChild(el("li", null, "None noted."));
    } else {
      items.forEach((t) => ul.appendChild(el("li", null, t)));
    }
    wrap.appendChild(ul);
    return wrap;
  }

  function render(data) {
    pop.replaceChildren();
    pop.appendChild(buildHead(data.candidate_name));

    const bucket = data.bucket || bucketForScore(data.score);
    const top = el("div", "score-pop-top");
    const big = el("div", "score-pop-big bucket-" + bucket);
    big.textContent = data.score != null ? Number(data.score).toFixed(1) : "--";
    big.appendChild(el("span", null, "/10"));
    top.appendChild(big);
    top.appendChild(el("span", "detail-bucket-label bucket-label-" + bucket, BUCKET_LABELS[bucket] || bucket));
    pop.appendChild(top);

    if (data.has_breakdown && data.parameters && data.parameters.length) {
      const head = el("div", "score-pop-grid-head");
      head.appendChild(el("span", null, "Parameter"));
      head.appendChild(el("span", null, "Weight"));
      head.appendChild(el("span", null, "Score /10"));
      pop.appendChild(head);
      data.parameters.forEach((p) => {
        const row = el("div", "score-pop-row");
        row.appendChild(el("div", "score-pop-pname", p.name));
        row.appendChild(el("div", "score-pop-weight", Math.round(p.weight) + "%"));
        const score = Math.max(0, Math.min(10, Number(p.score) || 0));
        const bar = el("div", "score-pop-bar bucket-" + bucketForScore(score));
        const fill = el("i");
        fill.style.width = score * 10 + "%";
        bar.appendChild(fill);
        bar.appendChild(el("em", null, score.toFixed(1)));
        row.appendChild(bar);
        if (p.evidence) row.appendChild(el("div", "score-pop-evidence", p.evidence));
        pop.appendChild(row);
      });
    } else if (data.scored) {
      pop.appendChild(el("div", "score-pop-note", "Breakdown not available for this older score - re-score to generate it."));
    } else {
      pop.appendChild(el("div", "score-pop-note", "This resume has not been scored yet."));
    }

    if (data.scored) {
      const more = el("details", "score-pop-more");
      more.appendChild(el("summary", null, "Summary, strengths and gaps"));
      more.appendChild(el("p", null, data.summary || "No summary saved."));
      more.appendChild(listSection("Strengths", data.positives));
      more.appendChild(listSection("Gaps", data.negatives));
      more.addEventListener("toggle", place);
      pop.appendChild(more);
    }

    if (data.scored_at) {
      const when = new Date(String(data.scored_at).replace(" ", "T") + "Z");
      const label = isNaN(when.getTime()) ? data.scored_at : when.toLocaleString();
      pop.appendChild(el("div", "score-pop-foot", "Scored " + label));
    }
    place();
  }

  function openFor(trigger) {
    const id = trigger.dataset.scoreBreakdown;
    if (!id) return;
    if (pop && anchorEl === trigger) {
      closePopup(true);
      return;
    }
    closePopup(false);
    anchorEl = trigger;
    pop = el("div", "score-pop");
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", "Score breakdown");
    pop.appendChild(el("div", "field-hint-small", "Loading score breakdown..."));
    document.body.appendChild(pop);
    place();
    const token = ++requestToken;
    fetch("/candidates/" + encodeURIComponent(id) + "/score-breakdown.json", { headers: { Accept: "application/json" } })
      .then((r) => {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then((data) => {
        if (token !== requestToken || !pop) return; // closed or replaced meanwhile
        render(data);
        const close = pop.querySelector(".score-pop-close");
        if (close) close.focus({ preventScroll: true });
      })
      .catch(() => {
        if (token !== requestToken || !pop) return;
        pop.replaceChildren(buildHead("Score breakdown"), el("div", "score-pop-note", "Could not load the breakdown. Please try again."));
        place();
      });
  }

  // Capture phase, so a click on a score inside a candidate row (which is itself a link) opens the
  // popup instead of navigating to that candidate.
  document.addEventListener(
    "click",
    (e) => {
      const trigger = e.target.closest && e.target.closest("[data-score-breakdown]");
      if (trigger) {
        e.preventDefault();
        e.stopPropagation();
        openFor(trigger);
        return;
      }
      if (pop && !pop.contains(e.target)) closePopup(false);
    },
    true
  );

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && pop) {
      e.preventDefault();
      closePopup(true);
      return;
    }
    if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches("[data-score-breakdown]")) {
      e.preventDefault();
      openFor(e.target);
    }
  });

  window.addEventListener("resize", place);
  window.addEventListener("scroll", place, true);
})();
