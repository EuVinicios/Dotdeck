// Dotdeck website: a "Copiar" button on each terminal command. Without JavaScript
// the commands are still there to select by hand.
(function () {
  "use strict";
  if (!navigator.clipboard || !window.isSecureContext) return;
  document.querySelectorAll(".command").forEach(function (block) {
    var code = block.querySelector("code");
    if (!code) return;
    var button = document.createElement("button");
    button.type = "button";
    button.className = "copy";
    button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/>' +
      '<path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg><span>Copiar</span>';
    var label = button.querySelector("span");
    button.setAttribute("aria-label", "Copiar o comando " + code.textContent);
    button.title = "Copiar";
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(code.textContent).then(function () {
        label.textContent = "Copiado";
        button.title = "Copiado";
        setTimeout(function () { label.textContent = "Copiar"; button.title = "Copiar"; }, 1800);
      }, function () {
        label.textContent = "Selecione";
      });
    });
    block.appendChild(button);
    block.classList.add("has-copy");
  });
})();
