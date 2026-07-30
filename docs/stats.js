/* Adds a "By the numbers" panel to the page.

   Kept in its own file on purpose: index.html only needs one extra line,
   so nothing in the existing page can be broken by this. If this script
   fails to load, the page carries on exactly as before.

   Reads docs/stats.json, written every hour by stats.py. */

(function () {
  var CSS = `
    .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:14px}
    .st{background:var(--panel-2);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
    .st .k{font-family:"IBM Plex Mono",monospace;font-size:10.5px;letter-spacing:.1em;
           text-transform:uppercase;color:var(--muted)}
    .st .v{font-family:"IBM Plex Mono",monospace;font-size:24px;margin-top:4px;color:var(--brass)}
    .st .r{font-size:12px;color:var(--muted);margin-top:4px;line-height:1.45}`;

  var style = document.createElement("style");
  style.textContent = CSS;
  document.head.appendChild(style);

  // slot the panel in just above "Past notes"
  var target = null;
  document.querySelectorAll("section").forEach(function (sec) {
    var h = sec.querySelector("h2");
    if (h && h.textContent.trim() === "Past notes") target = sec;
  });

  var box = document.createElement("section");
  box.innerHTML =
    '<h2>By the numbers</h2>' +
    '<p class="hint">What this thing has actually done since it started. Updated every hour.</p>' +
    '<div class="stats" id="stats"><p class="hint">Counting…</p></div>';

  if (target) target.parentNode.insertBefore(box, target);
  else document.querySelector(".wrap").appendChild(box);

  var card = function (k, v, sub) {
    return '<div class="st"><div class="k">' + k + '</div>' +
           '<div class="v">' + v + '</div>' +
           '<div class="r">' + (sub || "&nbsp;") + '</div></div>';
  };

  var plural = function (n, w) { return n + " " + w + (n === 1 ? "" : "s"); };

  fetch("stats.json?" + Date.now())
    .then(function (r) { return r.json(); })
    .then(function (s) {
      var since = new Date(s.started).toLocaleDateString("en-GB",
        { day: "2-digit", month: "short", year: "numeric" });

      var hit = s.forecasts_scored
        ? Math.round(s.forecasts_hit / s.forecasts_scored * 100) + "% landed in range"
        : "the first ones mature after a week";

      var lab = s.lab_tests_scored
        ? "across " + plural(s.lab_coins, "coin") + ", scored every hour"
        : "warming up — needs 12 hours per coin";

      var notes = s.notes_written === s.days_running
        ? "one every day so far"
        : (s.days_running - s.notes_written) + " day(s) missed";

      document.getElementById("stats").innerHTML =
          card("Days running", s.days_running, "since " + since)
        + card("Notes written", s.notes_written, notes)
        + card("Forecasts scored", s.forecasts_scored, hit)
        + card("Lab tests scored", (s.lab_tests_scored || 0).toLocaleString(), lab);
    })
    .catch(function () {
      document.getElementById("stats").innerHTML =
        '<p class="hint">Counts will appear after the next hourly run.</p>';
    });
})();
