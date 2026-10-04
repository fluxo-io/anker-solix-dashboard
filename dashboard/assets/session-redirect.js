(() => {
  const nativeFetch = window.fetch.bind(window);

  window.fetch = async (...args) => {
    const response = await nativeFetch(...args);
    if (response.status !== 401) {
      return response;
    }

    const requestTarget =
      typeof args[0] === "string" ? args[0] : args[0]?.url || "";
    const requestUrl = new URL(requestTarget, window.location.href);
    if (
      requestUrl.origin === window.location.origin &&
      requestUrl.pathname.startsWith("/_dash-")
    ) {
      try {
        window.sessionStorage.setItem("solarbank.sessionExpired", "1");
      } catch (_error) {
        // Storage can be disabled; the redirect still has to work.
      }
      window.location.replace("/login");
    }
    return response;
  };
})();
