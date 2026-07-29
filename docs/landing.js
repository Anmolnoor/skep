const terminalLines = Array.from(document.querySelectorAll("#terminal-lines li"));
let activeLine = 0;

function advanceTerminal() {
  for (const line of terminalLines) {
    line.classList.remove("active");
  }
  terminalLines[activeLine]?.classList.add("active");
  activeLine = (activeLine + 1) % terminalLines.length;
}

if (terminalLines.length > 0) {
  advanceTerminal();
  window.setInterval(advanceTerminal, 1100);
}

function fallbackCopy(text) {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

async function copyBlock(button) {
  const targetId = button.getAttribute("data-copy");
  const target = targetId ? document.getElementById(targetId) : null;
  const text = target?.textContent?.trim();
  if (!text) {
    return;
  }
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      fallbackCopy(text);
    }
  } else {
    fallbackCopy(text);
  }
  const original = button.textContent;
  button.textContent = "Copied";
  window.setTimeout(() => {
    button.textContent = original;
  }, 1400);
}

for (const button of document.querySelectorAll("[data-copy]")) {
  button.addEventListener("click", () => {
    copyBlock(button).catch(() => {
      button.textContent = "Copy failed";
    });
  });
}
