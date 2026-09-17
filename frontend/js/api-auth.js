(function () {
  const nativeFetch = window.fetch.bind(window);
  const proxyPrefix = window.location.pathname.match(/^(.*\/proxy\/\d+)\/?/)?.[1] || '';
  const storageKey = 'seagent_api_token:' + proxyPrefix;
  let token = '';
  try { token = window.sessionStorage.getItem(storageKey) || ''; } catch (error) {}
  let prompted = false;
  const button = document.getElementById('apiCredentialsButton');
  const dialog = document.getElementById('apiCredentialsDialog');
  const input = document.getElementById('apiTokenInput');

  function openCredentials() {
    if (!dialog || !input) return;
    button.hidden = false;
    input.value = token;
    if (!dialog.open) dialog.showModal();
    input.focus();
  }

  function setToken(value) {
    token = value.trim();
    prompted = false;
    try {
      if (token) window.sessionStorage.setItem(storageKey, token);
      else window.sessionStorage.removeItem(storageKey);
    } catch (error) {}
    button.hidden = false;
    dialog.close();
    input.value = '';
    window.dispatchEvent(new Event('seagent-auth-change'));
  }

  async function authenticatedFetch(url, options = {}) {
    const target = new URL(url, window.location.href);
    const applicationRequest = target.origin === window.location.origin &&
      target.pathname.startsWith(proxyPrefix + '/api/');
    const requestToken = token;
    const headers = new Headers(options.headers);
    if (applicationRequest && requestToken) headers.set('Authorization', 'Bearer ' + requestToken);
    const response = await nativeFetch(url, {...options, headers});
    if (applicationRequest && response.status === 401 && requestToken === token && !prompted) {
      prompted = true;
      openCredentials();
    }
    // A rejected mutation is never automatically replayed when credentials change.
    return response;
  }

  window.SEAgentAuth = {fetch: authenticatedFetch, hasToken: () => !!token};
  if (button) {
    button.hidden = !token;
    button.addEventListener('click', openCredentials);
    document.getElementById('apiCredentialsForm').addEventListener('submit', event => {
      event.preventDefault();
      setToken(input.value);
    });
    document.getElementById('apiCredentialsClear').addEventListener('click', () => setToken(''));
    document.getElementById('apiCredentialsCancel').addEventListener('click', () => dialog.close());
  }
})();
