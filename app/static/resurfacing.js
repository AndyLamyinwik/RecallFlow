(() => {
  const form = document.querySelector("[data-resurfacing-mount]");
  const actionPanel = document.querySelector("[data-resurfacing-actions]");

  document.querySelectorAll("[data-client-action-key]").forEach((input) => {
    if (input.value) return;
    input.value = window.crypto?.randomUUID
      ? window.crypto.randomUUID()
      : `${Date.now()}_${Math.random().toString(36).slice(2)}_${Math.random().toString(36).slice(2)}`;
  });

  const enableActions = () => {
    if (!actionPanel) return;
    actionPanel.disabled = false;
    actionPanel.setAttribute("aria-busy", "false");
  };

  if (!form) {
    enableActions();
    return;
  }
  if (form.dataset.sent === "true") return;

  form.dataset.sent = "true";
  window.requestAnimationFrame(() => {
    const body = new URLSearchParams(new FormData(form));
    fetch(form.action, {
      method: "POST",
      body,
      credentials: "same-origin",
      keepalive: true,
    })
      .then((response) => {
        if (!response.ok) throw new Error("mount acknowledgement failed");
        enableActions();
      })
      .catch(() => {
        // A failed acknowledgement deliberately leaves the reservation Pending;
        // the next page visit can safely retry it.
        const status = document.querySelector("[data-resurfacing-status]");
        if (status) status.hidden = false;
      });
  });
})();
