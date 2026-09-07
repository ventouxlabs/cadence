/* The check-in steppers. Vanilla, no build step (D-012), and loaded only by /assess.

   Client-side only on purpose: the value lives in a real number input, so with this file blocked
   the two buttons do nothing and the box is still typed into and still posted. Nothing here
   writes to the server -- the form does that, once, on Save. */
(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    if (!event.target.closest) return;
    var button = event.target.closest("button.astep");
    if (!button) return;
    var box = button.parentNode.querySelector("input.avalue");
    if (!box) return;
    event.preventDefault();

    var step = parseInt(button.getAttribute("data-step"), 10);
    if (!step) return;
    var next = (parseFloat(box.value) || 0) + step;
    var low = parseFloat(box.getAttribute("min"));
    var high = parseFloat(box.getAttribute("max"));
    /* Clamped here as well as on the server: a number the form will refuse is not worth showing
       the user, and the seconds tests step by five past their own floor otherwise. */
    if (!isNaN(low)) next = Math.max(low, next);
    if (!isNaN(high)) next = Math.min(high, next);
    box.value = String(next);
  }, true);
})();
