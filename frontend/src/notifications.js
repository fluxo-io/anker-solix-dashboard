import { notify, initNotifications } from '@fluxo/web-ui';
import '@fluxo/web-ui/styles.css';
function start() {
  initNotifications();
  // React owns Dash callback nodes: leave them mounted, and only forward events.
  const seen = new WeakMap();
  const scan = () => {
    document.querySelectorAll('[data-dashboard-notice]').forEach((node) => {
      const message = node.textContent.trim();
      const signature = JSON.stringify([node.dataset.eventId, node.dataset.level, message]);
      if (!message || seen.get(node) === signature) return;
      seen.set(node, signature);
      notify({level: node.dataset.level, message, eventId: node.dataset.eventId});
    });
  };
  document.documentElement.classList.add('notifications-ready');
  scan();
  new MutationObserver(scan).observe(document.body, {subtree:true, childList:true, characterData:true, attributes:true, attributeFilter:['data-event-id','data-level']});
  try {
    if (sessionStorage.getItem('solarbank.sessionExpired')) {
      sessionStorage.removeItem('solarbank.sessionExpired');
      notify({level: 'warning', message: 'Bitte erneut anmelden.'});
    }
  } catch (_) { /* Session feedback remains optional when storage is disabled. */ }
  let invalidPending = false;
  document.addEventListener('invalid', () => {
    if (invalidPending) return;
    invalidPending = true;
    queueMicrotask(() => { invalidPending = false; notify({level: 'error', message: 'Bitte prüfe die markierten Felder.'}); });
  }, true);
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once:true});
else start();
