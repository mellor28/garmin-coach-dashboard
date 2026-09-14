(function () {
  const form = document.getElementById("unlock-form");
  const passwordInput = document.getElementById("unlock-password");
  const submitButton = document.getElementById("unlock-submit");
  const errorHost = document.getElementById("unlock-error");

  function decodeBase64(value) {
    const binary = atob(value);
    return Uint8Array.from(binary, character => character.charCodeAt(0));
  }

  async function decryptDashboard(passphrase) {
    const response = await fetch(`dashboard-data.enc.json?v=${Date.now()}`, {
      cache: "no-store",
    });
    if (!response.ok) {
      throw new Error("Encrypted dashboard data could not be downloaded.");
    }

    const envelope = await response.json();
    if (envelope.version !== 1 || envelope.cipher !== "AES-256-GCM") {
      throw new Error("This dashboard uses an unsupported encryption format.");
    }

    const keyMaterial = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(passphrase),
      "PBKDF2",
      false,
      ["deriveKey"],
    );
    const key = await crypto.subtle.deriveKey(
      {
        name: "PBKDF2",
        salt: decodeBase64(envelope.salt),
        iterations: envelope.iterations,
        hash: "SHA-256",
      },
      keyMaterial,
      { name: "AES-GCM", length: 256 },
      false,
      ["decrypt"],
    );
    const plaintext = await crypto.subtle.decrypt(
      {
        name: "AES-GCM",
        iv: decodeBase64(envelope.iv),
      },
      key,
      decodeBase64(envelope.ciphertext),
    );
    return JSON.parse(new TextDecoder().decode(plaintext));
  }

  form.addEventListener("submit", async event => {
    event.preventDefault();
    errorHost.textContent = "";
    submitButton.disabled = true;
    submitButton.textContent = "Unlocking…";

    try {
      const dashboardData = await decryptDashboard(passwordInput.value);
      passwordInput.value = "";
      window.startDashboard(dashboardData);
      document.body.classList.remove("locked");
      document.getElementById("unlock-screen").remove();
    } catch (error) {
      console.error(error);
      errorHost.textContent = error.name === "OperationError"
        ? "That password did not unlock the dashboard."
        : error.message;
      passwordInput.focus();
      passwordInput.select();
    } finally {
      submitButton.disabled = false;
      submitButton.textContent = "Unlock";
    }
  });

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("./sw.js").catch(error => {
        console.warn("Offline support could not be enabled:", error);
      });
    });
  }

  passwordInput.focus();
})();
