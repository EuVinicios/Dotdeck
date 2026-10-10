// Dotdeck website: a "Copiar" button on each terminal command. Without JavaScript
// the commands are still there to select by hand.
(function () {
  "use strict";
  if (!navigator.clipboard || !window.isSecureContext) return;
  document.querySelectorAll(".command").forEach(function (block) {
    var code = block.querySelector("code");
    if (!code) return;
    var command = code.textContent;
    var button = document.createElement("button");
    button.type = "button";
    button.className = "copy";
    button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/>' +
      '<path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg><span>Copiar</span>';
    var label = button.querySelector("span");
    button.setAttribute("aria-label", "Copiar o comando " + command);
    button.title = "Copiar";
    // Screen readers hear the result here: the button keeps its own name.
    var status = document.createElement("span");
    status.className = "visually-hidden";
    status.setAttribute("role", "status");
    var timer;
    function show(text, spoken) {
      label.textContent = text;
      button.title = text;
      status.textContent = spoken;
      clearTimeout(timer);
      timer = setTimeout(function () {
        label.textContent = "Copiar";
        button.title = "Copiar";
        status.textContent = "";
      }, 1800);
    }
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(command).then(function () {
        show("Copiado", "Comando copiado.");
      }, function () {
        show("Selecione", "Não deu para copiar. Selecione o comando e copie.");
      });
    });
    block.appendChild(button);
    block.appendChild(status);
    block.classList.add("has-copy");
  });
})();
