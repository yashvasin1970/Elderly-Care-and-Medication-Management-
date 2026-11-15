/* Hearth: small progressive enhancements.
 *
 * Every page works without this file. It only adds conveniences: reading a
 * summary aloud, printing, a confirmation before removing things, and a
 * tidier medicine form. No data is sent anywhere from here.
 */
(function () {
  "use strict";

  /** Run `setup` for each element matching `selector`. */
  function each(selector, setup) {
    Array.prototype.forEach.call(document.querySelectorAll(selector), setup);
  }

  // --- Read aloud -----------------------------------------------------------
  // Uses the browser's built-in speech, so nothing leaves the device.
  var speech = window.speechSynthesis;
  if (speech && typeof window.SpeechSynthesisUtterance === "function") {
    each("[data-speak]", function (button) {
      var idleLabel = button.innerHTML;
      button.hidden = false;
      button.setAttribute("aria-pressed", "false");

      function reset() {
        button.innerHTML = idleLabel;
        button.setAttribute("aria-pressed", "false");
      }

      button.addEventListener("click", function () {
        if (speech.speaking) {
          speech.cancel();
          reset();
          return;
        }
        var utterance = new window.SpeechSynthesisUtterance(button.getAttribute("data-speak"));
        utterance.lang = document.documentElement.lang || "en";
        utterance.rate = 0.9; // a little slower than default is easier to follow
        utterance.onend = reset;
        utterance.onerror = reset;
        button.textContent = "Stop reading";
        button.setAttribute("aria-pressed", "true");
        speech.speak(utterance);
      });
    });
    window.addEventListener("pagehide", function () {
      speech.cancel();
    });
  }

  // --- Show the passcode while typing, if the person chooses to -----------
  each("[data-reveal]", function (box) {
    var input = document.getElementById(box.getAttribute("data-reveal"));
    var toggle = box.querySelector("input[type=checkbox]");
    if (!input || !toggle) {
      return;
    }
    box.hidden = false;
    toggle.addEventListener("change", function () {
      input.type = toggle.checked ? "text" : "password";
    });
  });

  // --- "More" menu: close on Escape or when clicking elsewhere --------------
  each("details.more-menu", function (menu) {
    document.addEventListener("click", function (event) {
      if (menu.open && !menu.contains(event.target)) {
        menu.open = false;
      }
    });
    menu.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && menu.open) {
        menu.open = false;
        menu.querySelector("summary").focus();
      }
    });
  });

  // --- Print ----------------------------------------------------------------
  each("[data-print]", function (button) {
    button.hidden = false;
    button.addEventListener("click", function () {
      window.print();
    });
  });

  // --- Ask before removing or stopping something ---------------------------
  each("form[data-confirm]", function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        event.preventDefault();
      }
    });
  });

  // --- Suggest the visitor's own timezone -----------------------------------
  each("select[data-detect-timezone]", function (select) {
    if (select.value !== "UTC" || !window.Intl || !Intl.DateTimeFormat) {
      return;
    }
    var zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    var match = Array.prototype.some.call(select.options, function (option) {
      return option.value === zone;
    });
    if (match) {
      select.value = zone;
    }
  });

  // --- Medicine form: show only the controls that apply to each dose -------
  each("[data-slot-row]", function (row) {
    var anchor = row.querySelector("[data-slot-anchor]");
    var offset = row.querySelector("[data-slot-offset]");
    var time = row.querySelector("[data-slot-time]");
    if (!anchor || !offset || !time) {
      return;
    }
    function update() {
      var value = anchor.value;
      offset.hidden = value === "" || value === "fixed";
      time.hidden = value !== "fixed";
    }
    anchor.addEventListener("change", update);
    update();
  });

  // --- Take keyboard and screen-reader users straight to form errors -------
  var summary = document.querySelector(".error-summary");
  if (summary) {
    summary.focus();
  }
})();
