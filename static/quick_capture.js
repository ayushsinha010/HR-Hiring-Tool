// Quick Capture: press Q anywhere (except while typing in a field) to open a
// small box, type a free sentence, and it's parsed into a candidate match +
// an optional date/time before being filed as a note - see capture.py for
// the actual parsing logic (no LLM, just a date library + fuzzy name match).
(function () {
  var overlay = document.getElementById("quick-capture-overlay");
  var input = document.getElementById("quick-capture-input");
  var highlight = document.getElementById("quick-capture-highlight");
  var resultBox = document.getElementById("quick-capture-result");
  var closeBtn = document.getElementById("quick-capture-close");
  if (!overlay || !input) return;

  var currentParse = null;
  var highlightTimer = null;
  var highlightRequestId = 0;

  function isTypingElsewhere() {
    var el = document.activeElement;
    if (!el) return false;
    var tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
  }

  function openModal() {
    overlay.hidden = false;
    resultBox.hidden = true;
    resultBox.innerHTML = "";
    input.value = "";
    currentParse = null;
    renderPlainHighlight();
    input.focus();
  }

  function closeModal() {
    overlay.hidden = true;
    currentParse = null;
    if (highlightTimer) clearTimeout(highlightTimer);
  }

  document.addEventListener("keydown", function (e) {
    if (!overlay.hidden) {
      if (e.key === "Escape") closeModal();
      return;
    }
    if ((e.key === "q" || e.key === "Q") && !e.ctrlKey && !e.metaKey && !e.altKey && !isTypingElsewhere()) {
      e.preventDefault();
      openModal();
    }
  });

  if (closeBtn) closeBtn.addEventListener("click", closeModal);
  overlay.addEventListener("click", function (e) {
    if (e.target === overlay) closeModal();
  });

  function escapeHtml(s) {
    var div = document.createElement("div");
    div.textContent = s || "";
    return div.innerHTML;
  }

  // Shows the exact typed text with no marks yet - called on every keystroke
  // so the backdrop never lags behind what's actually typed, even while the
  // debounced parse call for the highlight spans is still in flight.
  function renderPlainHighlight() {
    if (highlight) highlight.innerHTML = escapeHtml(input.value);
  }

  // Wraps the two spans (if present) in <mark> tags at their exact character
  // offsets. Spans come from capture.py's find_date_span/find_name_span,
  // which is the same parsing capture actually saves against - the highlight
  // is never a separate guess from what Confirm & Save would file.
  function applyHighlightSpans(text, dateSpan, nameSpan, stageSpan) {
    if (!highlight) return;
    var marks = [];
    if (dateSpan) marks.push({ start: dateSpan.start, end: dateSpan.end, cls: "hl-date" });
    if (nameSpan) marks.push({ start: nameSpan.start, end: nameSpan.end, cls: "hl-name" });
    if (stageSpan) marks.push({ start: stageSpan.start, end: stageSpan.end, cls: "hl-stage" });
    marks.sort(function (a, b) { return a.start - b.start; });

    var html = "";
    var pos = 0;
    marks.forEach(function (m) {
      if (m.start < pos || m.start >= m.end) return; // skip overlapping/invalid spans
      html += escapeHtml(text.slice(pos, m.start));
      html += '<mark class="' + m.cls + '">' + escapeHtml(text.slice(m.start, m.end)) + "</mark>";
      pos = m.end;
    });
    html += escapeHtml(text.slice(pos));
    highlight.innerHTML = html;
  }

  function syncHighlightScroll() {
    if (highlight) highlight.scrollLeft = input.scrollLeft;
  }

  function scheduleHighlight() {
    var text = input.value.trim();
    if (highlightTimer) clearTimeout(highlightTimer);
    if (!text) return;
    var requestId = ++highlightRequestId;
    highlightTimer = setTimeout(function () {
      fetch("/capture/parse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          // A slower, older request finishing after a newer one would
          // otherwise flash a stale highlight onto the current text.
          if (requestId !== highlightRequestId) return;
          applyHighlightSpans(text, data.date_span, data.name_span, data.stage_signal && data.stage_signal.span);
        })
        .catch(function () {});
    }, 120);
  }

  input.addEventListener("input", function () {
    renderPlainHighlight();
    scheduleHighlight();
  });
  input.addEventListener("scroll", syncHighlightScroll);

  function renderResult(data) {
    currentParse = data;
    applyHighlightSpans(data.raw_text, data.date_span, data.name_span, data.stage_signal && data.stage_signal.span);
    resultBox.hidden = false;
    var dateLine = data.parsed_date_display
      ? "Date/time detected: <strong>" + escapeHtml(data.parsed_date_display) + "</strong>"
      : "No date/time detected.";
    var stageRow = data.stage_signal
      ? '<label class="quick-capture-stage-row"><input type="checkbox" id="quick-capture-stage-check" checked>Also mark <strong>' +
        escapeHtml(data.stage_signal.label) + "</strong> on this candidate</label>"
      : "";

    if (data.matched_candidate) {
      resultBox.innerHTML =
        '<p class="quick-capture-match">Logging for <strong>' + escapeHtml(data.matched_candidate.candidate_name) +
        "</strong>" + (data.matched_candidate.position_title ? " (" + escapeHtml(data.matched_candidate.position_title) + ")" : "") +
        "<br>" + dateLine + "</p>" +
        stageRow +
        '<div class="quick-capture-actions">' +
        '<button type="button" class="btn btn-primary btn-small" id="quick-capture-confirm">Confirm &amp; Save</button>' +
        '<button type="button" class="btn btn-link btn-small" id="quick-capture-cancel">Cancel</button>' +
        "</div>";
      document.getElementById("quick-capture-confirm").addEventListener("click", saveCapture);
      document.getElementById("quick-capture-cancel").addEventListener("click", function () {
        resultBox.hidden = true;
        input.focus();
      });
    } else if (data.suggestions && data.suggestions.length) {
      var items = data.suggestions
        .map(function (s) {
          return (
            '<button type="button" class="quick-capture-suggestion-btn" data-id="' + s.id +
            '" data-name="' + escapeHtml(s.candidate_name) + '" data-position="' + escapeHtml(s.position_title) + '">' +
            escapeHtml(s.candidate_name) +
            ' <span class="field-hint-small">(' + escapeHtml(s.position_title) + ", " + s.score + "% match)</span></button>"
          );
        })
        .join("");
      resultBox.innerHTML =
        '<p class="quick-capture-match">Couldn\'t confidently match a candidate. Did you mean:</p>' +
        '<div class="quick-capture-suggestions">' + items + "</div>" +
        '<p class="field-hint-small">' + dateLine + "</p>";
      resultBox.querySelectorAll(".quick-capture-suggestion-btn").forEach(function (btn) {
        btn.addEventListener("click", function () {
          currentParse.matched_candidate = {
            id: parseInt(btn.dataset.id, 10),
            candidate_name: btn.dataset.name,
            position_title: btn.dataset.position,
          };
          renderResult(currentParse);
        });
      });
    } else {
      resultBox.innerHTML = '<p class="quick-capture-match">No matching candidate found in the system. ' + dateLine + "</p>";
    }
  }

  function parseCapture() {
    var text = input.value.trim();
    if (!text) return;
    fetch("/capture/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text }),
    })
      .then(function (r) { return r.json(); })
      .then(renderResult)
      .catch(function () {
        resultBox.hidden = false;
        resultBox.innerHTML = '<p class="quick-capture-match">Something went wrong parsing that - try again.</p>';
      });
  }

  function saveCapture() {
    if (!currentParse || !currentParse.matched_candidate) return;
    var stageCheck = document.getElementById("quick-capture-stage-check");
    var applyField = (stageCheck && stageCheck.checked && currentParse.stage_signal)
      ? currentParse.stage_signal.field
      : null;
    fetch("/capture/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        candidate_id: currentParse.matched_candidate.id,
        text: currentParse.raw_text,
        parsed_date: currentParse.parsed_date,
        apply_stage_field: applyField,
      }),
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.ok) {
          var extra = res.applied_field ? " (" + escapeHtml(currentParse.stage_signal.label) + " marked)" : "";
          resultBox.innerHTML = '<p class="quick-capture-success">Logged for ' + escapeHtml(res.candidate_name) + "." + extra + "</p>";
          setTimeout(function () {
            input.value = "";
            renderPlainHighlight();
            resultBox.hidden = true;
            input.focus();
          }, 1200);
        }
      });
  }

  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      parseCapture();
    }
  });
})();
